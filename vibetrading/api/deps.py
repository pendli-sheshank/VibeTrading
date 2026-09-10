from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.broker.base import BrokerClient
from vibetrading.orchestrator.runtime import OrchestratorRuntime
from vibetrading.persistence.db import get_session
from vibetrading.risk.engine import RiskEngine


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session() as session:
        yield session


def get_runtime(request: Request) -> OrchestratorRuntime:
    """The single place every route reaches OrchestratorRuntime through —
    same override-friendly dependency pattern as get_db/get_broker, so
    tests can inject a fake runtime instead of needing FastAPI's real
    lifespan (which ASGITransport-based test clients don't run) to have
    populated app.state.runtime."""
    return request.app.state.runtime


def get_broker(runtime: OrchestratorRuntime = Depends(get_runtime)) -> BrokerClient:
    """Reads the current broker from OrchestratorRuntime rather than caching
    its own instance — so a settings change that rebuilds the broker (e.g.
    flipping to live, or new Dhan credentials) is immediately visible to
    every request, not stuck on a stale cached one.
    """
    return runtime.broker


def get_risk_engine(broker: BrokerClient = Depends(get_broker)) -> RiskEngine:
    return RiskEngine(broker=broker)
