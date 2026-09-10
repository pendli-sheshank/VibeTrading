from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import reset_schema, seed_test_users
from vibetrading.api.app import app
from vibetrading.api.deps import get_db
from vibetrading.auth.backend import get_jwt_strategy
from vibetrading.persistence.orm_models import UserORM

TENANT_ID = 1


@pytest.fixture
async def db_override():
    """Same override-the-get_db-dependency pattern as every other
    ASGITransport-based integration test in this suite, but for
    TestClient's websocket_connect() (which needs a real, synchronous ASGI
    app to drive from its background thread rather than httpx's async
    transport). Deliberately always SQLite here, ignoring TEST_DATABASE_URL
    -- TestClient's websocket_connect() drives the app from a background
    thread with its OWN event loop (anyio's BlockingPortal), and asyncpg's
    connections are bound to the loop that created them, so a Postgres
    engine built in this test's loop fails cross-thread with a "different
    loop" RuntimeError the moment the WS route touches the DB. That's a
    TestClient/asyncpg test-harness limitation, not a real app constraint
    (a real deployment has exactly one event loop) -- this test exists to
    prove the auth-wiring code path (current_websocket_user), which is
    dialect-independent, so it isn't worth chasing onto Postgres.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    await reset_schema(engine)
    await seed_test_users(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield session_factory
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def _session_cookie(session_factory) -> str:
    """Mints a real session JWT the same way /login does (JWTStrategy.
    write_token()) without needing a full HTTP round trip through
    fastapi-users' password-hash + form-submission machinery."""
    async with session_factory() as session:
        user = await session.get(UserORM, TENANT_ID)
    return await get_jwt_strategy().write_token(user)


async def test_connecting_without_a_session_cookie_is_rejected(db_override):
    """The regression test for the pre-Phase-21 bug: current_active_user
    (built for HTTP Request-based auth) crashed with an unhandled
    TypeError on a WebSocket connection instead of cleanly rejecting it --
    see current_websocket_user in auth/backend.py."""
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
        pass


async def test_connecting_with_a_valid_session_cookie_is_accepted(db_override):
    session_factory = db_override
    token = await _session_cookie(session_factory)
    client = TestClient(app, cookies={"vibetrading_session": token})

    with client.websocket_connect("/ws"):
        pass  # connecting and cleanly disconnecting must not raise
