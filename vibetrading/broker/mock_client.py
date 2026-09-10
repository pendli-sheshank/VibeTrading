from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import ExecutionMode, OrderSide, OrderStatus
from vibetrading.core.exceptions import OrderRejectedError
from vibetrading.core.models import (
    Candle,
    FundsSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Stock,
)


class MockBrokerClient(BrokerClient):
    """In-memory paper-trading broker.

    Generates a deterministic (seeded) synthetic price series per symbol so
    local dev/tests/paper mode work with zero real credentials or network
    access. Fills every order immediately at the current synthetic LTP (or
    the requested limit price), and tracks positions/funds in memory only —
    nothing here is persisted or real.
    """

    def __init__(self, seed: int = 42, initial_funds: float = 1_000_000.0):
        self._seed = seed
        self._positions: dict[str, Position] = {}
        self._funds = FundsSnapshot(available_balance=initial_funds)
        self._orders: dict[str, OrderResult] = {}

    def _rng_for(self, symbol: str) -> random.Random:
        return random.Random(f"{self._seed}:{symbol}")

    async def get_historical_candles(
        self, stock: Stock, interval: str, from_date: datetime, to_date: datetime
    ) -> list[Candle]:
        rng = self._rng_for(stock.symbol)
        base_price = 100.0 + (rng.random() * 2000.0)

        candles: list[Candle] = []
        price = base_price
        current = from_date
        step = timedelta(days=1) if interval in ("1d", "day", "daily") else timedelta(minutes=1)

        while current <= to_date:
            pct_change = rng.gauss(0, 0.012)
            open_price = price
            close_price = max(0.05, open_price * (1 + pct_change))
            high_price = max(open_price, close_price) * (1 + abs(rng.gauss(0, 0.004)))
            low_price = min(open_price, close_price) * (1 - abs(rng.gauss(0, 0.004)))
            volume = rng.randint(50_000, 500_000)

            candles.append(
                Candle(
                    timestamp=current,
                    open=round(open_price, 2),
                    high=round(high_price, 2),
                    low=round(low_price, 2),
                    close=round(close_price, 2),
                    volume=volume,
                )
            )
            price = close_price
            current += step

        return candles

    async def get_ltp(self, stock: Stock) -> float:
        now = datetime.now(UTC)
        candles = await self.get_historical_candles(stock, "1d", now - timedelta(days=2), now)
        return candles[-1].close if candles else 0.0

    async def place_order(self, order_request: OrderRequest) -> OrderResult:
        if order_request.quantity <= 0:
            raise OrderRejectedError("Order quantity must be positive")

        ltp = await self.get_ltp(Stock(symbol=order_request.stock_symbol))
        fill_price = order_request.limit_price or ltp

        order_id = str(uuid.uuid4())
        result = OrderResult(
            order_id=order_id,
            broker_order_id=f"MOCK-{order_id[:8]}",
            status=OrderStatus.FILLED,
            filled_quantity=order_request.quantity,
            filled_price=fill_price,
            raw_response={"mode": ExecutionMode.PAPER.value},
        )
        self._orders[order_id] = result
        self._apply_fill(order_request, fill_price)
        return result

    def _apply_fill(self, order_request: OrderRequest, fill_price: float) -> None:
        symbol = order_request.stock_symbol
        signed_qty = order_request.quantity if order_request.side == OrderSide.BUY else -order_request.quantity
        cost = fill_price * order_request.quantity
        self._funds.available_balance -= cost if order_request.side == OrderSide.BUY else -cost

        existing = self._positions.get(symbol)
        if existing is None:
            if signed_qty == 0:
                return
            self._positions[symbol] = Position(
                stock_symbol=symbol,
                quantity=signed_qty,
                avg_price=fill_price,
                stop_loss_price=order_request.stop_loss_price,
                opened_at=datetime.now(UTC),
            )
            return

        new_quantity = existing.quantity + signed_qty
        if new_quantity == 0:
            realized = (fill_price - existing.avg_price) * min(existing.quantity, order_request.quantity)
            existing.realized_pnl += realized
            del self._positions[symbol]
            return

        if (existing.quantity > 0 and signed_qty > 0) or (existing.quantity < 0 and signed_qty < 0):
            total_cost = existing.avg_price * existing.quantity + fill_price * signed_qty
            existing.avg_price = total_cost / new_quantity
        existing.quantity = new_quantity

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order and order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            order.status = OrderStatus.CANCELLED
            return True
        return False

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_funds(self) -> FundsSnapshot:
        return self._funds

    async def subscribe_market_feed(
        self, stocks: list[Stock], on_tick: Callable[[str, float], None]
    ) -> None:
        for stock in stocks:
            on_tick(stock.symbol, await self.get_ltp(stock))
