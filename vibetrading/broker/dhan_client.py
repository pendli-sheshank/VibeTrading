from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import OrderSide, OrderStatus
from vibetrading.core.exceptions import BrokerError, OrderRejectedError
from vibetrading.core.models import (
    Candle,
    FundsSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Stock,
)
from vibetrading.risk.tokens import RiskApprovalToken

logger = logging.getLogger(__name__)

try:
    from dhanhq import dhanhq as _DhanSDKClient
except ImportError:  # the 'dhan' extra isn't installed; DhanBrokerClient stays unusable but importable
    _DhanSDKClient = None


class DhanBrokerClient(BrokerClient):
    """Wraps the official `dhanhq` SDK (install with `pip install .[dhan]`).

    Every method translates SDK exceptions into BrokerError/OrderRejectedError
    and normalizes responses into VibeTrading's core models, so the Risk Agent
    and orchestration code never touch the SDK directly — this is what keeps
    the Execution Agent broker-agnostic (see broker/base.py).

    The dhanhq SDK is synchronous (requests-based); every call here runs it
    in a worker thread via asyncio.to_thread so it doesn't block the event
    loop other agents are running on.

    IMPORTANT: the method/parameter names below reflect the dhanhq SDK's
    documented surface as commonly published, but SDK APIs do shift between
    releases. Before relying on this in production: `pip show dhanhq` to
    confirm the installed version, `python -c "import dhanhq; help(dhanhq)"`
    to check the exact method signatures, and run a paper/sandbox trade end
    to end before flipping VIBETRADING_EXECUTION_MODE=live.
    """

    def __init__(self, client_id: str, access_token: str):
        if _DhanSDKClient is None:
            raise BrokerError(
                "The 'dhanhq' package is not installed. Install it with `pip install .[dhan]` to use "
                "DhanBrokerClient, or leave DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN unset to fall back to "
                "MockBrokerClient for paper trading."
            )
        self._client = _DhanSDKClient(client_id, access_token)
        self._security_id_cache: dict[str, str] = {}

    def _security_id(self, stock: Stock) -> str:
        if stock.dhan_security_id:
            return stock.dhan_security_id
        cached = self._security_id_cache.get(stock.symbol)
        if cached:
            return cached
        raise BrokerError(
            f"No Dhan security_id known for {stock.symbol}. Populate Stock.dhan_security_id "
            "(e.g. from Dhan's published instrument master) before trading it."
        )

    async def place_order(self, order_request: OrderRequest, risk_token: RiskApprovalToken) -> OrderResult:
        self._require_valid_token(risk_token)

        security_id = self._security_id(Stock(symbol=order_request.stock_symbol))
        transaction_type = "BUY" if order_request.side == OrderSide.BUY else "SELL"

        def _call():
            return self._client.place_order(
                security_id=security_id,
                exchange_segment=self._client.NSE,
                transaction_type=transaction_type,
                quantity=order_request.quantity,
                order_type=self._client.MARKET if order_request.order_type == "MARKET" else self._client.LIMIT,
                product_type=self._client.INTRA,
                price=order_request.limit_price or 0,
            )

        try:
            response = await asyncio.to_thread(_call)
        except Exception as exc:
            raise BrokerError(f"Dhan place_order failed for {order_request.stock_symbol}: {exc}") from exc

        if isinstance(response, dict) and response.get("status") == "failure":
            raise OrderRejectedError(f"Dhan rejected order for {order_request.stock_symbol}: {response}")

        broker_order_id = _extract(response, "orderId") or _extract(response, "order_id")
        return OrderResult(
            order_id=str(broker_order_id or order_request.stock_symbol),
            broker_order_id=str(broker_order_id) if broker_order_id else None,
            status=OrderStatus.SUBMITTED,
            filled_quantity=0,
            filled_price=None,
            # Dhan doesn't cheaply report per-fill realized P&L; the daily-loss
            # circuit breaker for live mode relies on positions/funds polling
            # until a dedicated reconciliation job is added.
            realized_pnl=0.0,
            raw_response=response if isinstance(response, dict) else {"raw": str(response)},
        )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            response = await asyncio.to_thread(self._client.cancel_order, order_id)
        except Exception as exc:
            raise BrokerError(f"Dhan cancel_order failed for {order_id}: {exc}") from exc
        return bool(isinstance(response, dict) and response.get("status") != "failure")

    async def get_positions(self) -> list[Position]:
        try:
            response = await asyncio.to_thread(self._client.get_positions)
        except Exception as exc:
            raise BrokerError(f"Dhan get_positions failed: {exc}") from exc

        rows = response if isinstance(response, list) else response.get("data", []) if isinstance(response, dict) else []
        positions: list[Position] = []
        for row in rows:
            quantity = int(row.get("netQty", 0) or 0)
            if quantity == 0:
                continue
            positions.append(
                Position(
                    stock_symbol=row.get("tradingSymbol", ""),
                    quantity=quantity,
                    avg_price=float(row.get("costPrice", 0) or 0),
                    unrealized_pnl=float(row.get("unrealizedProfit", 0) or 0),
                    realized_pnl=float(row.get("realizedProfit", 0) or 0),
                    opened_at=datetime.now(UTC),
                )
            )
        return positions

    async def get_funds(self) -> FundsSnapshot:
        try:
            response = await asyncio.to_thread(self._client.get_fund_limits)
        except Exception as exc:
            raise BrokerError(f"Dhan get_fund_limits failed: {exc}") from exc

        data = response.get("data", response) if isinstance(response, dict) else {}
        return FundsSnapshot(
            available_balance=float(data.get("availabelBalance", data.get("availableBalance", 0)) or 0),
            used_margin=float(data.get("utilizedAmount", 0) or 0),
        )

    async def get_historical_candles(
        self, stock: Stock, interval: str, from_date: datetime, to_date: datetime
    ) -> list[Candle]:
        security_id = self._security_id(stock)

        def _call():
            return self._client.historical_daily_data(
                security_id=security_id,
                exchange_segment=self._client.NSE,
                instrument_type="EQUITY",
                from_date=from_date.strftime("%Y-%m-%d"),
                to_date=to_date.strftime("%Y-%m-%d"),
            )

        try:
            response = await asyncio.to_thread(_call)
        except Exception as exc:
            raise BrokerError(f"Dhan historical_daily_data failed for {stock.symbol}: {exc}") from exc

        data = response.get("data", response) if isinstance(response, dict) else {}
        opens = data.get("open", [])
        highs = data.get("high", [])
        lows = data.get("low", [])
        closes = data.get("close", [])
        volumes = data.get("volume", [])
        timestamps = data.get("timestamp", data.get("start_Time", []))

        candles: list[Candle] = []
        for i in range(len(closes)):
            candles.append(
                Candle(
                    timestamp=_parse_epoch_or_now(timestamps[i] if i < len(timestamps) else None),
                    open=float(opens[i]) if i < len(opens) else float(closes[i]),
                    high=float(highs[i]) if i < len(highs) else float(closes[i]),
                    low=float(lows[i]) if i < len(lows) else float(closes[i]),
                    close=float(closes[i]),
                    volume=int(volumes[i]) if i < len(volumes) else 0,
                )
            )
        return candles

    async def get_ltp(self, stock: Stock) -> float:
        now = datetime.now(UTC)
        candles = await self.get_historical_candles(stock, "1d", now, now)
        if candles:
            return candles[-1].close
        raise BrokerError(f"Could not determine LTP for {stock.symbol}: no candle data returned.")

    async def subscribe_market_feed(self, stocks: list[Stock], on_tick: Callable[[str, float], None]) -> None:
        raise NotImplementedError(
            "Live WebSocket market feed subscription (dhanhq.marketfeed) is a follow-up integration "
            "point — wire it here using the SDK's marketfeed module, translating each tick into "
            "on_tick(symbol, ltp). Until then, poll get_ltp() for each stock instead."
        )


def _extract(response, key: str):
    if isinstance(response, dict):
        if key in response:
            return response[key]
        data = response.get("data")
        if isinstance(data, dict):
            return data.get(key)
    return None


def _parse_epoch_or_now(value) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (TypeError, ValueError, OSError):
        return datetime.now(UTC)
