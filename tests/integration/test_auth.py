from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import make_test_engine, reset_schema
from vibetrading.api.app import app
from vibetrading.api.deps import get_broker, get_db, get_runtime
from vibetrading.broker.mock_client import MockBrokerClient


class _FakeRuntime:
    def __init__(self, broker):
        self._broker = broker

    @property
    def broker(self):
        return self._broker

    async def restart(self) -> None:  # pragma: no cover - not exercised here
        pass


@pytest_asyncio.fixture
async def auth_client():
    """Unlike the other dashboard-route fixtures, this one does NOT override
    current_active_user/current_dashboard_user -- these tests exercise the
    real register/login/logout flow end-to-end."""
    engine = make_test_engine()
    await reset_schema(engine)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    broker = MockBrokerClient(seed=1)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_runtime] = lambda: _FakeRuntime(broker)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_unauthenticated_dashboard_request_redirects_to_login(auth_client):
    response = await auth_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_unauthenticated_htmx_request_gets_hx_redirect_header(auth_client):
    response = await auth_client.post(
        "/settings/execution-mode/paper", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/login"


async def test_unauthenticated_api_request_gets_json_401(auth_client):
    response = await auth_client.get("/api/watchlist")
    assert response.status_code == 401


async def test_register_login_access_round_trip(auth_client):
    register = await auth_client.post(
        "/register", data={"email": "new@example.com", "password": "correct-horse"}
    )
    assert register.status_code == 303
    assert register.headers["location"] == "/login?registered=1"

    login = await auth_client.post(
        "/login", data={"username": "new@example.com", "password": "correct-horse"}
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/"
    assert "vibetrading_session" in login.cookies

    dashboard = await auth_client.get("/")
    assert dashboard.status_code == 200
    assert "Log out" in dashboard.text

    logout = await auth_client.post("/logout")
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"

    after_logout = await auth_client.get("/", follow_redirects=False)
    assert after_logout.status_code == 303


async def test_login_with_wrong_password_shows_error_without_redirecting(auth_client):
    await auth_client.post("/register", data={"email": "someone@example.com", "password": "correct-horse"})

    response = await auth_client.post(
        "/login", data={"username": "someone@example.com", "password": "wrong-password"}
    )
    assert response.status_code == 400
    assert "Incorrect email or password" in response.text


async def test_registering_the_same_email_twice_shows_error(auth_client):
    await auth_client.post("/register", data={"email": "dupe@example.com", "password": "correct-horse"})
    response = await auth_client.post("/register", data={"email": "dupe@example.com", "password": "correct-horse"})

    assert response.status_code == 400
    assert "already exists" in response.text


async def test_registering_with_a_short_password_shows_error(auth_client):
    response = await auth_client.post("/register", data={"email": "short@example.com", "password": "abc"})

    assert response.status_code == 400
    assert "at least 8 characters" in response.text
