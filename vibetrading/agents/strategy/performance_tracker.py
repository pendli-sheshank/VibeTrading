from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.core.enums import ActionType
from vibetrading.persistence.orm_models import SignalORM
from vibetrading.persistence.repositories import list_signals_for_stock


async def reconcile_signal_performance(
    session: AsyncSession, signal: SignalORM, current_price: float, quantity: int | None = None
) -> SignalORM:
    """Mark a past Signal's realized outcome against a later price.

    This is a lightweight mark-to-market check of the *call itself*
    (reference_price at signal time vs. current_price), independent of
    whether a real trade was ever matched to it — giving the Strategy tab's
    "how did this suggestion do" view a number even before/without full
    order-matching. A hold signal is neutral by definition (0 P&L).
    """
    if signal.reference_price is None:
        return signal

    qty = quantity or signal.suggested_quantity or 1

    if signal.action == ActionType.BUY.value:
        pnl_per_share = current_price - signal.reference_price
    elif signal.action == ActionType.SELL.value:
        pnl_per_share = signal.reference_price - current_price
    else:
        pnl_per_share = 0.0

    signal.realized_pnl = round(pnl_per_share * qty, 2)
    signal.realized_at = datetime.now(UTC)
    return signal


async def get_performance_summary(session: AsyncSession, tenant_id: int, stock_symbol: str) -> dict:
    signals = await list_signals_for_stock(session, tenant_id, stock_symbol, only_realized=True)
    if not signals:
        return {"stock_symbol": stock_symbol, "total_signals": 0, "win_rate": None, "total_pnl": 0.0}

    directional = [s for s in signals if s.action != ActionType.HOLD.value]
    wins = sum(1 for s in directional if (s.realized_pnl or 0.0) > 0)

    return {
        "stock_symbol": stock_symbol,
        "total_signals": len(signals),
        "win_rate": round(wins / len(directional), 4) if directional else None,
        "total_pnl": round(sum(s.realized_pnl or 0.0 for s in signals), 2),
    }
