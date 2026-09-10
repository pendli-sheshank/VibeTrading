from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.broker.base import BrokerClient
from vibetrading.config import Settings
from vibetrading.core.enums import ActionType, OrderSide, OrderStatus
from vibetrading.core.models import OrderRequest, OrderResult, RiskCheckResult, Signal, Stock
from vibetrading.orchestrator.lease import verify_lease
from vibetrading.persistence.orm_models import AuditLogORM, OrderORM, RiskEventORM, UsedRiskTokenORM
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.rules import DEFAULT_RULES, RiskContext, RiskRule
from vibetrading.risk.state import get_or_create_risk_state, record_realized_pnl
from vibetrading.risk.tokens import mint_token
from vibetrading.settings.cache import get_tenant_settings

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
        tenant_id: int,
        rules: list[RiskRule] | None = None,
        config: RiskConfig | None = None,
        settings: Settings | None = None,
        fencing_token: int | None = None,
    ):
        self.broker = broker
        self.tenant_id = tenant_id
        self.rules = rules if rules is not None else DEFAULT_RULES
        self._config_override = config
        self._settings = settings or get_tenant_settings(tenant_id)
        # Set only by the distributed scheduler path (see
        # orchestrator/manager.py) -- a worker's proof, as of when it last
        # acquired/renewed this tenant's lease, that it's the sole active
        # owner. None (the default, and every direct/manual/test
        # construction of RiskEngine) means "no distributed enforcement,"
        # matching this class's single-process behavior before Phase 20.
        self.fencing_token = fencing_token

    async def approve_and_execute(
        self, session: AsyncSession, signal: Signal, stock: Stock, signal_id: int | None = None
    ) -> ExecutionResult:
        if self.fencing_token is not None and not await verify_lease(session, self.tenant_id, self.fencing_token):
            # Never mint a token or touch risk state on a lease this worker
            # no longer (verifiably) owns -- worst case is a skipped cycle,
            # never a duplicate order from two workers racing.
            return ExecutionResult(
                approved=False,
                risk_check=RiskCheckResult(
                    approved=False,
                    rule_results={"lease_fencing": False},
                    reasons=[
                        (
                            "lease_fencing: this worker's tenant lease is stale or lost; cycle skipped to "
                            "avoid a duplicate order from another worker."
                        )
                    ],
                ),
            )

        config = self._config_override or RiskConfig.from_settings(self._settings)
        risk_state = await get_or_create_risk_state(session, self.tenant_id)

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
            tenant_id=self.tenant_id,
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
                        tenant_id=self.tenant_id,
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

        # Durable replay-protection record, reserved before any broker call
        # -- token_id is a fresh 128-bit random value each mint, so this is
        # practically always None; a hit here (defense-in-depth alongside
        # the lease fencing check above) means this exact token was
        # somehow already spent, surviving a process restart unlike
        # risk/tokens.py's in-memory _used_token_ids set.
        if await session.get(UsedRiskTokenORM, token.token_id) is not None:
            audit.status = "rejected"
            return ExecutionResult(
                approved=False,
                risk_check=RiskCheckResult(
                    approved=False,
                    rule_results={"token_replay": False},
                    reasons=["token_replay: a RiskApprovalToken with this id was already consumed."],
                ),
                audit_log_id=audit.id,
            )
        session.add(UsedRiskTokenORM(token_id=token.token_id, tenant_id=self.tenant_id, consumed_at=datetime.now(UTC)))

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
                tenant_id=self.tenant_id,
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
            await record_realized_pnl(session, self.tenant_id, order_result.realized_pnl)

        return ExecutionResult(approved=True, risk_check=risk_check, order_result=order_result, audit_log_id=audit.id)
