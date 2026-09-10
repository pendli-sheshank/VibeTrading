from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.broker.base import BrokerClient
from vibetrading.config import Settings, get_settings
from vibetrading.core.enums import ActionType, OrderSide, OrderStatus
from vibetrading.core.models import OrderRequest, OrderResult, RiskCheckResult, Signal, Stock
from vibetrading.persistence.orm_models import AuditLogORM, OrderORM, RiskEventORM
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.rules import DEFAULT_RULES, RiskContext, RiskRule
from vibetrading.risk.state import get_or_create_risk_state, record_realized_pnl
from vibetrading.risk.tokens import mint_token

_ACTION_TO_SIDE = {ActionType.BUY: OrderSide.BUY, ActionType.SELL: OrderSide.SELL}


@dataclass
class ExecutionResult:
    approved: bool
    risk_check: RiskCheckResult
    order_result: OrderResult | None = None
    audit_log_id: int | None = None


class RiskEngine:
    """The Risk Agent / Gatekeeper. approve_and_execute() is the ONLY
    sanctioned path from a Signal to a real broker order — see
    broker.base.BrokerClient.place_order, which structurally refuses to run
    without the RiskApprovalToken this method mints on approval.
    """

    def __init__(
        self,
        broker: BrokerClient,
        rules: list[RiskRule] | None = None,
        config: RiskConfig | None = None,
        settings: Settings | None = None,
    ):
        self.broker = broker
        self.rules = rules if rules is not None else DEFAULT_RULES
        self._config_override = config
        self._settings = settings or get_settings()

    async def approve_and_execute(
        self, session: AsyncSession, signal: Signal, stock: Stock, signal_id: int | None = None
    ) -> ExecutionResult:
        config = self._config_override or RiskConfig.from_settings(self._settings)
        risk_state = await get_or_create_risk_state(session)

        positions = await self.broker.get_positions()
        funds = await self.broker.get_funds()
        has_existing_position = any(p.stock_symbol == stock.symbol for p in positions)
        total_exposure = sum(p.quantity * p.avg_price for p in positions)

        ctx = RiskContext(
            signal=signal,
            stock=stock,
            config=config,
            risk_state=risk_state,
            open_position_count=len(positions),
            has_existing_position=has_existing_position,
            available_funds=funds.available_balance,
            total_exposure_inr=total_exposure,
        )

        outcomes = [rule.check(ctx) for rule in self.rules]
        rule_results = {outcome.rule_name: outcome.passed for outcome in outcomes}
        reasons = [f"{outcome.rule_name}: {outcome.reason}" for outcome in outcomes]
        approved = all(outcome.passed for outcome in outcomes)

        risk_check = RiskCheckResult(
            approved=approved,
            rule_results=rule_results,
            reasons=reasons,
            adjusted_quantity=ctx.quantity or None,
            adjusted_stop_loss=ctx.stop_loss_price,
        )

        audit = AuditLogORM(
            order_id=None,
            signal_id=signal_id,
            contributing_agent_output_ids=signal.contributing_output_ids,
            risk_checks_passed=rule_results,
            mode=self._settings.vibetrading_execution_mode.value,
            status="pending",
            timestamp=datetime.now(UTC),
        )
        session.add(audit)
        await session.flush()

        for outcome in outcomes:
            if not outcome.passed:
                session.add(
                    RiskEventORM(
                        stock_symbol=stock.symbol,
                        rule_name=outcome.rule_name,
                        passed=False,
                        reason=outcome.reason,
                        timestamp=datetime.now(UTC),
                    )
                )

        if signal.action == ActionType.HOLD:
            audit.status = "skipped_hold"
            return ExecutionResult(approved=True, risk_check=risk_check, order_result=None, audit_log_id=audit.id)

        if not approved:
            audit.status = "rejected"
            return ExecutionResult(approved=False, risk_check=risk_check, order_result=None, audit_log_id=audit.id)

        token = mint_token(signal_id=signal_id, stock_symbol=stock.symbol, quantity=ctx.quantity)
        order_request = OrderRequest(
            stock_symbol=stock.symbol,
            side=_ACTION_TO_SIDE[signal.action],
            quantity=ctx.quantity,
            stop_loss_price=ctx.stop_loss_price,
            mode=self._settings.vibetrading_execution_mode,
        )

        try:
            order_result = await self.broker.place_order(order_request, token)
        except Exception:
            audit.status = "failed"
            raise

        audit.order_id = order_result.order_id
        audit.status = order_result.status.value if order_result.status != OrderStatus.FILLED else "filled"

        session.add(
            OrderORM(
                order_id=order_result.order_id,
                broker_order_id=order_result.broker_order_id,
                signal_id=signal_id,
                stock_symbol=stock.symbol,
                side=_ACTION_TO_SIDE[signal.action].value,
                quantity=ctx.quantity,
                status=order_result.status.value,
                mode=self._settings.vibetrading_execution_mode.value,
                filled_quantity=order_result.filled_quantity,
                filled_price=order_result.filled_price,
                timestamp=datetime.now(UTC),
                raw_response=order_result.raw_response,
            )
        )

        if order_result.realized_pnl:
            await record_realized_pnl(session, order_result.realized_pnl)

        return ExecutionResult(approved=True, risk_check=risk_check, order_result=order_result, audit_log_id=audit.id)
