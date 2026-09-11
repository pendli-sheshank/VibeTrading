from __future__ import annotations

from fastapi import Depends, Request

from vibetrading.api.db_dep import get_db
from vibetrading.auth.backend import current_active_user
from vibetrading.broker.base import BrokerClient
from vibetrading.orchestrator.manager import MultiTenantRuntimeManager
from vibetrading.orchestrator.runtime import OrchestratorRuntime
from vibetrading.persistence.orm_models import UserORM
from vibetrading.risk.engine import RiskEngine

__all__ = ["get_broker", "get_db", "get_risk_engine", "get_runtime"]


async def get_runtime(
    request: Request, user: UserORM = Depends(current_active_user)
) -> OrchestratorRuntime:
    """The single place every route reaches an OrchestratorRuntime through —
    same override-friendly dependency pattern as get_db, so tests can
    inject a fake runtime instead of needing FastAPI's real lifespan (which
    ASGITransport-based test clients don't run) to have populated
    app.state.runtime_manager. Resolves to the CALLING USER's own runtime —
    there is one per tenant (see orchestrator/manager.py), never a shared
    one, and a router-level auth dependency has already guaranteed `user`
    is the real authenticated caller by the time this runs."""
    manager: MultiTenantRuntimeManager = request.app.state.runtime_manager
    return await manager.get_or_start(user.id)


async def get_broker(runtime: OrchestratorRuntime = Depends(get_runtime)) -> BrokerClient:
    """Reads the current broker from the caller's OrchestratorRuntime rather
    than caching its own instance — so a settings change that rebuilds the
    broker (e.g. flipping to live, or new Dhan credentials) is immediately
    visible to every request, not stuck on a stale cached one.
    """
    return runtime.broker


def get_risk_engine(
    user: UserORM = Depends(current_active_user), broker: BrokerClient = Depends(get_broker)
) -> RiskEngine:
    return RiskEngine(broker=broker, tenant_id=user.id)
