from __future__ import annotations

from contextlib import asynccontextmanager

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import make_test_engine, reset_schema, seed_test_users
from vibetrading.api.app import app
from vibetrading.api.deps import get_db
from vibetrading.api.routers.health import _migrations_head_revision


@pytest_asyncio.fixture
async def health_client(monkeypatch):
    engine = make_test_engine()
    await reset_schema(engine)
    await seed_test_users(engine)

    # reset_schema() only drops/creates Base.metadata's tables -- it has no
    # idea alembic_version exists, since Alembic manages that table itself,
    # not the ORM. In CI's Postgres job, the "alembic upgrade head against
    # clean Postgres" step runs first against this same TEST_DATABASE_URL
    # database, leaving a real alembic_version table (at the real head
    # revision) behind for every test that follows, including this one.
    # Without this, both tests below silently assume a table that may or
    # may not already be there depending on which CI job/local setup is
    # running.
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # health.py's readiness() calls persistence.db.get_session() directly
    # (it isn't behind a request -- there's no user/tenant to scope a
    # get_db() Depends() to), same as the orchestrator modules -- point it
    # at this test's isolated engine too, same pattern as
    # patch_orchestrator_db_session in test_orchestrator_runtime.py.
    @asynccontextmanager
    async def _test_get_session():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr("vibetrading.api.routers.health.get_session", _test_get_session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, session_factory

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_healthz_is_always_ok_and_requires_no_auth(health_client):
    client, _ = health_client
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_not_ready_when_migration_state_is_unknown(health_client):
    """The reset_schema()-built test schema (Base.metadata.create_all(),
    not a real `alembic upgrade`) has no alembic_version table at all --
    /readyz must report not_ready, not crash, and must not conflate that
    with the database itself being unreachable."""
    client, _ = health_client
    response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] is True
    assert body["checks"]["migration_current"] is None


async def test_readyz_reports_ok_when_at_the_migrations_head(health_client):
    client, session_factory = health_client
    head_revision = _migrations_head_revision()

    async with session_factory() as session:
        await session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        await session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": head_revision}
        )
        await session.commit()

    response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["migration_current"] == head_revision
    assert body["checks"]["migration_head"] == head_revision
