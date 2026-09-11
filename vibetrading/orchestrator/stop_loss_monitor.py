from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import ActionType, SignalSource
from vibetrading.core.models import Signal, Stock
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.risk.engine import ExecutionResult, RiskEngine

logger = logging.getLogger(__name__)


class StopLossMonitor:
    """Watches every open position against the latest tick and submits a
    protective exit (source=SYSTEM_STOP_LOSS) through the same risk-gated
    path as any other order the moment a position's stop-loss is breached.
    Runs far more frequently than the Strategy cycle — see
    AGENT_INTERVAL_STOP_LOSS_MONITOR_SEC.
    """

    def __init__(self, broker: BrokerClient, risk_engine: RiskEngine):
        self.broker = broker
        self.risk_engine = risk_engine

    async def check_all(self, session: AsyncSession) -> list[ExecutionResult]:
        positions = await self.broker.get_positions()
        results: list[ExecutionResult] = []

        for position in positions:
            if position.stop_loss_price is None or position.quantity == 0:
                continue

            ltp = await self.broker.get_ltp(Stock(symbol=position.stock_symbol))
            is_long = position.quantity > 0
            breached = (is_long and ltp <= position.stop_loss_price) or (
                not is_long and ltp >= position.stop_loss_price
            )
            if not breached:
                continue

            logger.warning(
                "Stop-loss breached for %s: LTP %.2f vs stop %.2f — submitting protective exit.",
                position.stock_symbol,
                ltp,
                position.stop_loss_price,
            )

            exit_signal = Signal(
                stock_symbol=position.stock_symbol,
                timestamp=datetime.now(UTC),
                source=SignalSource.SYSTEM_STOP_LOSS,
                action=ActionType.SELL if is_long else ActionType.BUY,
                confidence=1.0,
                reasoning=f"Stop-loss triggered: LTP {ltp:.2f} breached stop {position.stop_loss_price:.2f}.",
                suggested_quantity=abs(position.quantity),
                reference_price=ltp,
            )

            result = await self.risk_engine.approve_and_execute(session, exit_signal, Stock(symbol=position.stock_symbol))
            await session.commit()

            await event_bus.publish(
                {
                    "type": "stop_loss_exit",
                    "stock_symbol": position.stock_symbol,
                    "approved": result.approved,
                    "ltp": ltp,
                    "stop_loss_price": position.stop_loss_price,
                },
                tenant_id=self.risk_engine.tenant_id,
            )
            results.append(result)

        return results
