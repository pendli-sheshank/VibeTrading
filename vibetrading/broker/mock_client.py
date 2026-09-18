from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime

from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import ExecutionMode, OrderSide, OrderStatus
from vibetrading.core.exceptions import OrderRejectedError
from vibetrading.core.models import (
    FundsSnapshot,
    OrderRequest,
    OrderResult,
    Position,
)
from vibetrading.risk.tokens import RiskApprovalToken


class MockBrokerClient(BrokerClient):
    """In-memory paper-trading broker.

    Fills every order immediately at the requested limit price (or a
    deterministic, seeded synthetic price) and tracks positions/funds in
    memory only — nothing here is persisted or real, and no credentials or
    network access are needed.

    It serves no market data: analysis prices come from a market-data
    provider, so paper mode shows the same real quotes as live mode and only
    the *execution* is simulated.
    """

    def __init__(self, seed: int = 42, initial_funds: float = 1_000_000.0):
        self._seed = seed
        self._positions: dict[str, Position] = {}
        self._funds = FundsSnapshot(available_balance=initial_funds)
        self._orders: dict[str, OrderResult] = {}

    def _rng_for(self, symbol: str) -> random.Random:
        return random.Random(f"{self._seed}:{symbol}")

    def _fill_price_for(self, symbol: str) -> float:
        """A deterministic synthetic price for filling paper orders.

        Private to the mock: it is a fill simulator, not market data. Anything
        that needs real prices goes to a market-data provider instead.
        """
        rng = self._rng_for(symbol)
        price = 100.0 + (rng.random() * 2000.0)
        for _ in range(5):
            price = max(0.05, price * (1 + rng.gauss(0, 0.012)))
        return round(price, 2)

    async def place_order(self, order_request: OrderRequest, risk_token: RiskApprovalToken) -> OrderResult:
        self._require_valid_token(risk_token)

        if order_request.quantity <= 0:
            raise OrderRejectedError("Order quantity must be positive")

        fill_price = order_request.limit_price or self._fill_price_for(order_request.stock_symbol)
        realized_pnl = self._apply_fill(order_request, fill_price)

        order_id = str(uuid.uuid4())
        result = OrderResult(
            order_id=order_id,
            broker_order_id=f"MOCK-{order_id[:8]}",
            status=OrderStatus.FILLED,
            filled_quantity=order_request.quantity,
            filled_price=fill_price,
            realized_pnl=realized_pnl,
            raw_response={"mode": ExecutionMode.PAPER.value},
        )
        self._orders[order_id] = result
        return result

    def _apply_fill(self, order_request: OrderRequest, fill_price: float) -> float:
        """Applies a fill to positions/funds and returns the realized P&L this
        specific fill booked (0.0 for an opening/increasing trade).
        """
        symbol = order_request.stock_symbol
        signed_qty = order_request.quantity if order_request.side == OrderSide.BUY else -order_request.quantity
        cost = fill_price * order_request.quantity
        self._funds.available_balance -= cost if order_request.side == OrderSide.BUY else -cost

        existing = self._positions.get(symbol)
        if existing is None or existing.quantity == 0:
            if signed_qty == 0:
                return 0.0
            self._positions[symbol] = Position(
                stock_symbol=symbol,
                quantity=signed_qty,
                avg_price=fill_price,
                stop_loss_price=order_request.stop_loss_price,
                opened_at=datetime.now(UTC),
            )
            return 0.0

        same_direction = (existing.quantity > 0 and signed_qty > 0) or (existing.quantity < 0 and signed_qty < 0)
        if same_direction:
            new_quantity = existing.quantity + signed_qty
            total_cost = existing.avg_price * existing.quantity + fill_price * signed_qty
            existing.avg_price = total_cost / new_quantity
            existing.quantity = new_quantity
            return 0.0

        # Opposite direction: this reduces, closes, or flips the position.
        closing_qty = min(abs(existing.quantity), abs(signed_qty))
        if existing.quantity > 0:
            realized_pnl = (fill_price - existing.avg_price) * closing_qty
        else:
            realized_pnl = (existing.avg_price - fill_price) * closing_qty
        existing.realized_pnl += realized_pnl

        new_quantity = existing.quantity + signed_qty
        if new_quantity == 0:
            del self._positions[symbol]
        elif (new_quantity > 0) != (existing.quantity > 0):
            # Flipped direction: the remainder is a fresh position at fill_price.
            existing.quantity = new_quantity
            existing.avg_price = fill_price
            existing.opened_at = datetime.now(UTC)
        else:
            existing.quantity = new_quantity

        return realized_pnl

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

