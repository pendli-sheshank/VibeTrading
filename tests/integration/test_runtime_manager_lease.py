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


async def test_manage_leases_false_never_builds_a_scheduler_or_competes_for_the_lease(patched_db_session):
    """The Web Service side of the plan's Web/Worker split (WORKER_ROLE=web,
    see api/app.py) -- get_or_start() still returns a runtime with a
    working broker (dashboard/API routes need that), but never attempts
    lease acquisition and never runs a scheduler, regardless of whether
    anyone else holds the lease."""
    web_manager = MultiTenantRuntimeManager(worker_id="web-replica", manage_leases=False)
    runtime = await web_manager.get_or_start(TENANT_ID)
    try:
        assert runtime.broker is not None
        assert runtime.scheduler is None

        from vibetrading.persistence.orm_models import TenantLeaseORM

        async with patched_db_session() as session:
            lease = await session.get(TenantLeaseORM, TENANT_ID)
            assert lease is None  # never even attempted acquisition
    finally:
        await web_manager.shutdown_all()


async def test_manage_leases_false_start_lease_loop_is_a_no_op(patched_db_session):
    web_manager = MultiTenantRuntimeManager(worker_id="web-replica", manage_leases=False)
    web_manager.start_lease_loop()
    assert web_manager._lease_loop_task is None
    await web_manager.shutdown_all()


async def test_a_newly_registered_tenant_gets_picked_up_by_the_next_renewal_tick(patched_db_session):
    """A standalone worker process (scripts/run_worker.py) never serves
    HTTP requests, so nothing calls get_or_start() for a user who
    registers after start_all_existing_tenants() already ran --
    _discover_new_tenants() (run every renewal tick) is what catches up."""
    from vibetrading.persistence.orm_models import UserORM

    manager = MultiTenantRuntimeManager(worker_id="worker-a")
    await manager.start_all_existing_tenants()
    assert manager.get(TENANT_ID) is not None
    assert manager.get(3) is None  # tenant 3 not registered yet (1 and 2 are seed_test_users() defaults)

    session_factory = patched_db_session
    async with session_factory() as session:
        session.add(UserORM(id=3, email="new-tenant@example.com", hashed_password="x", is_active=True))
        await session.commit()

    await manager._discover_new_tenants()

    assert manager.get(3) is not None
    await manager.shutdown_all()
