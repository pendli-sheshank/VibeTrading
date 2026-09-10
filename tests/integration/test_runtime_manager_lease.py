from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import make_test_engine, reset_schema, seed_test_users
from vibetrading.orchestrator.manager import MultiTenantRuntimeManager

TENANT_ID = 1


@pytest.fixture
async def patched_db_session(monkeypatch):
    """Every module MultiTenantRuntimeManager's call chain touches
    (manager.py itself, plus OrchestratorRuntime/OrchestratorScheduler
    underneath get_or_start()) resolves the DB via persistence.db.
    get_session() -- point them all at one isolated test engine, same
    pattern as test_orchestrator_runtime.py's patch_orchestrator_db_session."""
    engine = make_test_engine()
    await reset_schema(engine)
    await seed_test_users(engine)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _test_get_session():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr("vibetrading.orchestrator.manager.get_session", _test_get_session)
    monkeypatch.setattr("vibetrading.orchestrator.runtime.get_session", _test_get_session)
    monkeypatch.setattr("vibetrading.orchestrator.scheduler.get_session", _test_get_session)
    return session_factory


async def test_a_second_worker_does_not_get_a_scheduler_while_the_first_holds_the_lease(patched_db_session):
    manager_a = MultiTenantRuntimeManager(worker_id="worker-a")
    runtime_a = await manager_a.get_or_start(TENANT_ID)
    try:
        assert runtime_a.scheduler is not None
        assert runtime_a.scheduler.risk_engine.fencing_token == 1

        manager_b = MultiTenantRuntimeManager(worker_id="worker-b")
        runtime_b = await manager_b.get_or_start(TENANT_ID)
        try:
            # worker-b still gets a runtime (dashboard/broker access always
            # works) -- but no scheduler, since worker-a's lease is live.
            assert runtime_b.scheduler is None
        finally:
            await runtime_b.shutdown()
    finally:
        await runtime_a.shutdown()


async def test_releasing_the_lease_lets_a_waiting_worker_take_over_on_its_next_renewal(patched_db_session):
    manager_a = MultiTenantRuntimeManager(worker_id="worker-a")
    runtime_a = await manager_a.get_or_start(TENANT_ID)
    assert runtime_a.scheduler is not None

    manager_b = MultiTenantRuntimeManager(worker_id="worker-b")
    runtime_b = await manager_b.get_or_start(TENANT_ID)
    assert runtime_b.scheduler is None

    # worker-a shuts down gracefully -- releases the lease rather than
    # making worker-b wait out the full TTL.
    await manager_a.shutdown_all()

    # worker-b's next renewal tick (simulated directly, rather than
    # sleeping out the real interval) now finds the lease free.
    await manager_b._renew_one_lease(TENANT_ID)

    assert runtime_b.scheduler is not None
    assert runtime_b.scheduler.risk_engine.fencing_token == 2  # takeover bumped the token

    await manager_b.shutdown_all()
