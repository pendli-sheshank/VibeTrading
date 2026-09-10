from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.broker.base import BrokerClient
from vibetrading.persistence.db import get_session
from vibetrading.risk.engine import RiskEngine


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session() as session:
        yield session


def get_broker(request: Request) -> BrokerClient:
    """Reads the current broker from OrchestratorRuntime (app.state.runtime)
    rather than caching its own instance — so a settings change that
    rebuilds the broker (e.g. flipping to live, or new Dhan credentials) is
    immediately visible to every request, not stuck on a stale cached one.
    """
    return request.app.state.runtime.broker


def get_risk_engine(broker: BrokerClient = Depends(get_broker)) -> RiskEngine:
    return RiskEngine(broker=broker)
