from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.broker.base import BrokerClient
from vibetrading.broker.factory import get_broker_client
from vibetrading.persistence.db import get_session
from vibetrading.risk.engine import RiskEngine


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session() as session:
        yield session


@lru_cache
def get_broker() -> BrokerClient:
    """One broker instance per process — MockBrokerClient's in-memory
    positions/funds need to be shared across requests to behave sensibly.
    """
    return get_broker_client()


def get_risk_engine() -> RiskEngine:
    return RiskEngine(broker=get_broker())
