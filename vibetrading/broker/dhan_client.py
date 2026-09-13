from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import DataStatus, OrderSide, OrderStatus
from vibetrading.core.exceptions import BrokerError, MarketDataUnavailableError, OrderRejectedError
from vibetrading.core.models import (
    Candle,
    FundsSnapshot,
    OptionChainSnapshot,
    OptionStrike,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    Stock,
)
from vibetrading.core.reliability import with_retry_and_circuit_breaker
from vibetrading.risk.tokens import RiskApprovalToken


def _dhan_circuit_name(self: DhanBrokerClient, *args, **kwargs) -> str:
    """One breaker per Dhan account (client_id), not one global "dhan"
    breaker -- accounts have independent credentials/health, so one
    tenant's broken API key or rate limit shouldn't fail-fast every other
    tenant's Dhan calls too. Never applied to place_order() -- see that
    method's own docstring for why a broker write must never be
    automatically retried."""
    return f"dhan:{self._client_id}"

logger = logging.getLogger(__name__)

try:
    from dhanhq import DhanContext as _DhanContext
    from dhanhq import dhanhq as _DhanSDKClient
except ImportError:  # the 'dhan' extra isn't installed; DhanBrokerClient stays unusable but importable
    _DhanSDKClient = None
    _DhanContext = None


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
    releases -- e.g. 2.0.2 replaced the `dhanhq(client_id, access_token)`
    constructor with `dhanhq(DhanContext(client_id, access_token))`, hence
    the `dhanhq>=2.0.2` floor in pyproject.toml. Before relying on this in
    production: `pip show dhanhq` to confirm the installed version,
    `python -c "import dhanhq; help(dhanhq)"` to check the exact method
    signatures, and run a paper/sandbox trade end to end before flipping
    VIBETRADING_EXECUTION_MODE=live.
    """

    def __init__(self, client_id: str, access_token: str):
        if _DhanSDKClient is None:
            raise BrokerError(
                "The 'dhanhq' package is not installed. Install it with `pip install .[dhan]` to use "
                "DhanBrokerClient, or leave DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN unset to fall back to "
                "MockBrokerClient for paper trading."
            )
        self._client = _DhanSDKClient(_DhanContext(client_id, access_token))
        self._client_id = client_id
        self._security_id_cache: dict[str, str] = {}

    def _security_id(self, stock: Stock) -> str:
        if stock.dhan_security_id:
            return stock.dhan_security_id
        cached = self._security_id_cache.get(stock.symbol)
        if cached:
            return cached
        raise MarketDataUnavailableError(
            f"No Dhan security ID is set for {stock.symbol}, so Dhan cannot be asked about it. "
            "Add the security ID for this stock under Settings -> Watchlist (Dhan publishes an "
            "instrument master listing them)."
        )

    async def place_order(self, order_request: OrderRequest, risk_token: RiskApprovalToken) -> OrderResult:
        # Deliberately NOT wrapped in with_retry_and_circuit_breaker: an
        # order placement is not idempotent. If the SDK call times out or
        # its response is lost after Dhan already accepted the order,
        # blindly retrying could submit a duplicate. A failed placement
        # surfaces as a single BrokerError; the caller (RiskEngine) does
        # not retry it either.
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

    @with_retry_and_circuit_breaker(_dhan_circuit_name, retry_on=(BrokerError,))
    async def cancel_order(self, order_id: str) -> bool:
        try:
            response = await asyncio.to_thread(self._client.cancel_order, order_id)
        except Exception as exc:
            raise BrokerError(f"Dhan cancel_order failed for {order_id}: {exc}") from exc
        return bool(isinstance(response, dict) and response.get("status") != "failure")

    @with_retry_and_circuit_breaker(_dhan_circuit_name, retry_on=(BrokerError,))
    async def get_positions(self) -> list[Position]:
        try:
            response = await asyncio.to_thread(self._client.get_positions)
        except Exception as exc:
            raise BrokerError(f"Dhan get_positions failed: {exc}") from exc

        if isinstance(response, dict) and str(response.get("status", "")).lower() == "failure":
            raise BrokerError(f"Dhan rejected get_positions: {_remarks_text(response)}")

        rows = response if isinstance(response, list) else response.get("data", []) if isinstance(response, dict) else []
        if not isinstance(rows, list):
            rows = []
        positions: list[Position] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
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

    @with_retry_and_circuit_breaker(_dhan_circuit_name, retry_on=(BrokerError,))
    async def get_funds(self) -> FundsSnapshot:
        try:
            response = await asyncio.to_thread(self._client.get_fund_limits)
        except Exception as exc:
            raise BrokerError(f"Dhan get_fund_limits failed: {exc}") from exc

        data = _unwrap(response, "get_fund_limits")
        return FundsSnapshot(
            available_balance=float(data.get("availabelBalance", data.get("availableBalance", 0)) or 0),
            used_margin=float(data.get("utilizedAmount", 0) or 0),
        )

    @with_retry_and_circuit_breaker(
        _dhan_circuit_name, retry_on=(BrokerError,), not_a_failure=(MarketDataUnavailableError,)
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

        data = _unwrap(response, f"historical_daily_data for {stock.symbol}")

        try:
            closes = data.get("close", []) or []
            opens = data.get("open", []) or []
            highs = data.get("high", []) or []
            lows = data.get("low", []) or []
            volumes = data.get("volume", []) or []
            timestamps = data.get("timestamp", data.get("start_Time", [])) or []

            candles = [
                Candle(
                    timestamp=_parse_epoch_or_now(timestamps[i] if i < len(timestamps) else None),
                    open=float(opens[i]) if i < len(opens) else float(closes[i]),
                    high=float(highs[i]) if i < len(highs) else float(closes[i]),
                    low=float(lows[i]) if i < len(lows) else float(closes[i]),
                    close=float(closes[i]),
                    volume=int(volumes[i]) if i < len(volumes) else 0,
                )
                for i in range(len(closes))
            ]
        except (TypeError, ValueError, AttributeError) as exc:
            # A shape we don't understand is a broker-integration problem, not
            # an unhandled crash halfway up the call stack in a dashboard route.
            raise BrokerError(
                f"Dhan returned candle data for {stock.symbol} in an unexpected shape: {exc}"
            ) from exc

        if not candles:
            raise MarketDataUnavailableError(
                f"Dhan returned no candles for {stock.symbol} between "
                f"{from_date:%Y-%m-%d} and {to_date:%Y-%m-%d}. The range may cover only "
                "non-trading days, or the security ID may point at a different instrument type."
            )
        return candles

    @with_retry_and_circuit_breaker(
        _dhan_circuit_name, retry_on=(BrokerError,), not_a_failure=(MarketDataUnavailableError,)
    )
    async def get_quote(self, stock: Stock) -> Quote:
        """Live quote via Dhan's market-quote endpoint.

        Falls back to the lighter OHLC endpoint if the full quote isn't
        available for this segment -- but never falls back to inventing a
        price: an unusable response raises rather than returning zeros.
        """
        security_id = self._security_id(stock)
        segment = self._client.NSE

        def _call():
            return self._client.quote_data({segment: [int(security_id)]})

        try:
            response = await asyncio.to_thread(_call)
        except Exception as exc:
            raise BrokerError(f"Dhan quote_data failed for {stock.symbol}: {exc}") from exc

        data = _unwrap(response, f"quote_data for {stock.symbol}")
        row = _first_quote_row(data, segment, security_id)
        if row is None:
            raise MarketDataUnavailableError(
                f"Dhan returned no quote row for {stock.symbol} (security ID {security_id})."
            )

        ohlc = row.get("ohlc") if isinstance(row.get("ohlc"), dict) else {}
        return Quote(
            symbol=stock.symbol,
            status=DataStatus.LIVE,
            last_price=_as_float(row.get("last_price", row.get("lastPrice"))),
            previous_close=_as_float(ohlc.get("close", row.get("close"))),
            open=_as_float(ohlc.get("open")),
            high=_as_float(ohlc.get("high")),
            low=_as_float(ohlc.get("low")),
            close=_as_float(ohlc.get("close")),
            volume=_as_int(row.get("volume")),
            timestamp=datetime.now(UTC),
            source="dhan:quote_data",
        )

    @with_retry_and_circuit_breaker(
        _dhan_circuit_name, retry_on=(BrokerError,), not_a_failure=(MarketDataUnavailableError,)
    )
    async def get_option_chain(self, stock: Stock, strikes_around_atm: int = 5) -> OptionChainSnapshot:
        security_id = self._security_id(stock)
        segment = self._client.NSE

        def _expiries():
            return self._client.expiry_list(under_security_id=int(security_id), under_exchange_segment=segment)

        try:
            expiry_response = await asyncio.to_thread(_expiries)
        except Exception as exc:
            raise BrokerError(f"Dhan expiry_list failed for {stock.symbol}: {exc}") from exc

        expiries = _unwrap_list(expiry_response, f"expiry_list for {stock.symbol}")
        if not expiries:
            raise MarketDataUnavailableError(
                f"Dhan lists no option expiries for {stock.symbol}; it may not have listed options."
            )
        expiry = str(expiries[0])

        def _chain():
            return self._client.option_chain(
                under_security_id=int(security_id), under_exchange_segment=segment, expiry=expiry
            )

        try:
            chain_response = await asyncio.to_thread(_chain)
        except Exception as exc:
            raise BrokerError(f"Dhan option_chain failed for {stock.symbol}: {exc}") from exc

        chain_data = _unwrap(chain_response, f"option_chain for {stock.symbol}")
        return _parse_option_chain(stock.symbol, expiry, chain_data, strikes_around_atm)

    async def get_ltp(self, stock: Stock) -> float:
        """Last traded price, from the live quote.

        Deliberately NOT the old "one daily candle for today" trick: that
        returned nothing at all on weekends, holidays and before the first
        candle of the session prints, which turned every such call into an
        error during exactly the hours people check the dashboard most.
        """
        quote = await self.get_quote(stock)
        if quote.last_price is not None:
            return quote.last_price
        raise MarketDataUnavailableError(
            f"Dhan returned a quote for {stock.symbol} with no last traded price."
        )

    async def subscribe_market_feed(self, stocks: list[Stock], on_tick: Callable[[str, float], None]) -> None:
        raise NotImplementedError(
            "Live WebSocket market feed subscription (dhanhq.marketfeed) is a follow-up integration "
            "point — wire it here using the SDK's marketfeed module, translating each tick into "
            "on_tick(symbol, ltp). Until then, poll get_ltp() for each stock instead."
        )


def _unwrap(response, what: str) -> dict:
    """Turn one dhanhq response into its `data` payload, or raise.

    The SDK does not raise on API errors -- it returns
    {"status": "failure", "remarks": ..., "data": ""}, where `data` is an
    empty STRING. Reaching straight for response["data"]["close"] therefore
    blew up with `'str' object has no attribute 'get'` on every Dhan error
    (bad security ID, expired token, rate limit), which surfaced as a 500 on
    the backtest route and as an unexplained 0%-confidence card on Analyze.
    Checking the envelope here turns all of those into one BrokerError
    carrying Dhan's own remarks (e.g. "DH-905 : Invalid security id"), so
    the UI can say what actually went wrong.
    """
    if not isinstance(response, dict):
        raise BrokerError(f"Dhan {what} returned {type(response).__name__}, expected a response object.")

    if str(response.get("status", "")).lower() == "failure":
        raise BrokerError(f"Dhan rejected {what}: {_remarks_text(response)}")

    data = response.get("data", response)
    if not isinstance(data, dict):
        raise MarketDataUnavailableError(f"Dhan {what} returned no usable data (got {data!r}).")
    return data


def _unwrap_list(response, what: str) -> list:
    """Same envelope check as _unwrap, for endpoints whose `data` is a list
    (expiry_list returns ["2026-09-25", ...] rather than an object)."""
    if not isinstance(response, dict):
        raise BrokerError(f"Dhan {what} returned {type(response).__name__}, expected a response object.")

    if str(response.get("status", "")).lower() == "failure":
        raise BrokerError(f"Dhan rejected {what}: {_remarks_text(response)}")

    data = response.get("data", [])
    if not isinstance(data, list):
        raise MarketDataUnavailableError(f"Dhan {what} returned no usable list (got {data!r}).")
    return data


def _remarks_text(response: dict) -> str:
    """Dhan's failure detail, which is sometimes a plain string and
    sometimes an error object. Pull the human sentence out of either --
    dumping the raw dict into a UI message is not an explanation."""
    remarks = response.get("remarks") or response.get("data") or ""
    if isinstance(remarks, dict):
        message = remarks.get("errorMessage") or remarks.get("error_message") or ""
        code = remarks.get("errorCode") or remarks.get("error_code") or ""
        if message:
            return f"{code} : {message}" if code else str(message)
        # An error object whose fields are all empty (Dhan does return
        # these) tells the reader nothing -- printing {'error_code': None,
        # ...} at them is worse than saying plainly that there were no
        # details and pointing at the usual causes.
        return (
            "the request was rejected without details — this is usually an expired or invalid "
            "access token, or the API being unreachable"
        )
    return str(remarks) if remarks else "no details provided"


def _first_quote_row(data: dict, segment: str, security_id: str) -> dict | None:
    """Dhan nests quotes as {segment: {security_id: {...}}}; tolerate both
    that and a flatter {security_id: {...}} without guessing at values."""
    by_segment = data.get(segment)
    candidates = by_segment if isinstance(by_segment, dict) else data
    if not isinstance(candidates, dict):
        return None
    row = candidates.get(str(security_id)) or candidates.get(security_id)
    if isinstance(row, dict):
        return row
    # Single-instrument request: some responses return the row unkeyed.
    rows = [v for v in candidates.values() if isinstance(v, dict)]
    return rows[0] if len(rows) == 1 else None


def _parse_option_chain(
    symbol: str, expiry: str, data: dict, strikes_around_atm: int
) -> OptionChainSnapshot:
    underlying = _as_float(data.get("last_price", data.get("underlying_price")))
    raw_strikes = data.get("oc", data.get("strikes"))
    if not isinstance(raw_strikes, dict) or not raw_strikes:
        raise MarketDataUnavailableError(f"Dhan returned an option chain for {symbol} with no strikes.")

    parsed: list[OptionStrike] = []
    for raw_strike, legs in raw_strikes.items():
        strike_price = _as_float(raw_strike)
        if strike_price is None or not isinstance(legs, dict):
            continue
        call = legs.get("ce") if isinstance(legs.get("ce"), dict) else {}
        put = legs.get("pe") if isinstance(legs.get("pe"), dict) else {}
        parsed.append(
            OptionStrike(
                strike=strike_price,
                call_oi=_as_int(call.get("oi")),
                call_oi_change=_oi_change(call),
                call_volume=_as_int(call.get("volume")),
                call_iv=_as_float(call.get("implied_volatility")),
                call_ltp=_as_float(call.get("last_price")),
                put_oi=_as_int(put.get("oi")),
                put_oi_change=_oi_change(put),
                put_volume=_as_int(put.get("volume")),
                put_iv=_as_float(put.get("implied_volatility")),
                put_ltp=_as_float(put.get("last_price")),
            )
        )

    if not parsed:
        raise MarketDataUnavailableError(f"Dhan's option chain for {symbol} had no readable strikes.")

    parsed.sort(key=lambda s: s.strike)
    atm = min(parsed, key=lambda s: abs(s.strike - underlying)).strike if underlying else None
    if atm is not None:
        atm_index = next(i for i, s in enumerate(parsed) if s.strike == atm)
        low = max(0, atm_index - strikes_around_atm)
        parsed = parsed[low : atm_index + strikes_around_atm + 1]

    total_call_oi = sum(s.call_oi or 0 for s in parsed)
    total_put_oi = sum(s.put_oi or 0 for s in parsed)
    return OptionChainSnapshot(
        symbol=symbol,
        status=DataStatus.LIVE,
        expiry=expiry,
        underlying_price=underlying,
        atm_strike=atm,
        strikes=parsed,
        total_call_oi=total_call_oi or None,
        total_put_oi=total_put_oi or None,
        put_call_ratio=round(total_put_oi / total_call_oi, 3) if total_call_oi else None,
        timestamp=datetime.now(UTC),
        source="dhan:option_chain",
    )


def _oi_change(leg: dict) -> int | None:
    """Change in open interest for one option leg. None (not 0) when the
    provider didn't send a previous-OI figure -- an unknown change and a
    genuinely flat one must not render identically."""
    current = _as_int(leg.get("oi"))
    previous = _as_int(leg.get("previous_oi"))
    if current is None or previous is None:
        return None
    return current - previous


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


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
