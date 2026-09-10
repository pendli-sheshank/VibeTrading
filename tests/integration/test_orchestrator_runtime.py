from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import Settings
from vibetrading.orchestrator.runtime import OrchestratorRuntime
from vibetrading.persistence.orm_models import Base
from vibetrading.settings.cache import get_tenant_settings
from vibetrading.settings.service import save_settings

TENANT_ID = 1


@pytest.fixture
async def test_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def patch_orchestrator_db_session(monkeypatch, test_engine):
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def _test_get_session():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr("vibetrading.orchestrator.scheduler.get_session", _test_get_session)
    monkeypatch.setattr("vibetrading.orchestrator.runtime.get_session", _test_get_session)
    return session_factory


async def test_start_builds_broker_and_skips_scheduler_when_disabled(patch_orchestrator_db_session):
    session_factory = patch_orchestrator_db_session
    async with session_factory() as session:
        await save_settings(session, TENANT_ID, {"enable_scheduler": False})
        await session.commit()

    runtime = OrchestratorRuntime(tenant_id=TENANT_ID)
    await runtime.start()
    try:
        assert isinstance(runtime.broker, MockBrokerClient)
        assert runtime.scheduler is None
    finally:
        await runtime.shutdown()


async def test_start_builds_scheduler_when_enabled(patch_orchestrator_db_session):
    settings = get_tenant_settings(TENANT_ID)
    settings.enable_scheduler = True

    runtime = OrchestratorRuntime(tenant_id=TENANT_ID, settings=settings)
    await runtime.start()
    try:
        assert runtime.scheduler is not None
        assert runtime.scheduler.scheduler.running is True
    finally:
        await runtime.shutdown()


async def test_restart_produces_a_new_broker_instance(patch_orchestrator_db_session):
    settings = get_tenant_settings(TENANT_ID)
    settings.enable_scheduler = False

    runtime = OrchestratorRuntime(tenant_id=TENANT_ID, settings=settings)
    await runtime.start()
    try:
        first_broker = runtime.broker
        await runtime.restart()
        assert runtime.broker is not first_broker
    finally:
        await runtime.shutdown()


async def test_broker_property_raises_before_start():
    runtime = OrchestratorRuntime(tenant_id=TENANT_ID, settings=Settings(_env_file=None))
    with pytest.raises(RuntimeError):
        _ = runtime.broker


async def test_saving_dhan_credentials_via_save_settings_and_restarting_swaps_the_broker(
    db_session, patch_orchestrator_db_session
):
    """The Phase 12 proof: writing Dhan credentials + flipping to live
    through save_settings(), then calling runtime.restart(), changes what
    get_broker() (i.e. runtime.broker) returns -- get_broker_client() picks
    DhanBrokerClient once settings.has_dhan_credentials is true. The dhanhq
    SDK isn't installed in this environment, so DhanBrokerClient itself
    raises BrokerError on construction -- which is exactly the observable
    proof that get_broker_client() re-read the live (mutated) per-tenant
    settings object and took the Dhan branch rather than reusing a stale
    MockBrokerClient.
    """
    from vibetrading.core.exceptions import BrokerError

    settings = get_tenant_settings(TENANT_ID)
    settings.enable_scheduler = False

    runtime = OrchestratorRuntime(tenant_id=TENANT_ID, settings=settings)
    await runtime.start()
    try:
        assert isinstance(runtime.broker, MockBrokerClient)

        await save_settings(
            db_session,
            TENANT_ID,
            {
                "dhan_client_id": "test-client-id",
                "dhan_access_token": "test-access-token",
                "vibetrading_execution_mode": "live",
            },
        )
        await db_session.commit()

        with pytest.raises(BrokerError, match="dhanhq"):
            await runtime.restart()
    finally:
        await runtime.shutdown()


async def test_restart_waits_for_an_in_flight_job_before_tearing_down_the_scheduler(
    patch_orchestrator_db_session,
):
    """Proves OrchestratorRuntime.restart() never discards an in-flight job
    -- it drains the old scheduler via shutdown_gracefully() before
    building the new one."""
    settings = get_tenant_settings(TENANT_ID)
    settings.enable_scheduler = True

    runtime = OrchestratorRuntime(tenant_id=TENANT_ID, settings=settings)
    await runtime.start()
    try:
        old_scheduler = runtime.scheduler
        old_scheduler._job_started()
        finished = False

        async def finish_after_delay():
            nonlocal finished
            await asyncio.sleep(0.05)
            finished = True
            old_scheduler._job_finished()

        task = asyncio.create_task(finish_after_delay())
        await runtime.restart()

        assert finished is True
        assert runtime.scheduler is not old_scheduler
        await task
    finally:
        await runtime.shutdown()
