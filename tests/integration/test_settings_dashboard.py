from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from vibetrading.api.app import app
from vibetrading.api.deps import get_db, get_runtime
from vibetrading.auth.backend import current_active_user, current_dashboard_user
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import get_settings
from vibetrading.persistence.orm_models import Base, UserORM
from vibetrading.persistence.repositories import get_app_setting

FAKE_USER = UserORM(id=1, email="test@example.com", hashed_password="x", is_active=True)


class FakeRuntime:
    """Stands in for OrchestratorRuntime in dashboard-route tests -- these
    tests exercise ASGITransport directly (no real FastAPI lifespan), so
    app.state.runtime is never populated; get_runtime is overridden to
    return this instead, same pattern as the existing get_broker override
    in test_dashboard_and_api.py."""

    def __init__(self, broker, fail: bool = False):
        self._broker = broker
        self.fail = fail
        self.restart_calls = 0

    @property
    def broker(self):
        return self._broker

    async def restart(self) -> None:
        self.restart_calls += 1
        if self.fail:
            raise RuntimeError("simulated broker rebuild failure")


@pytest_asyncio.fixture
async def settings_client():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    broker = MockBrokerClient(seed=1)
    runtime = FakeRuntime(broker)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_runtime] = lambda: runtime
    app.dependency_overrides[current_active_user] = lambda: FAKE_USER
    app.dependency_overrides[current_dashboard_user] = lambda: FAKE_USER

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, session_factory, runtime

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_settings_page_renders_all_sections_and_masks_secrets(settings_client):
    client, _, _ = settings_client

    response = await client.get("/settings")
    assert response.status_code == 200
    body = response.text
    for heading in ["Execution &amp; Broker", "Watchlist", "LLM", "Risk Limits", "Data Sources"]:
        assert heading in body

    # risk_token_secret has a non-empty class default -> must show as
    # "configured", never the actual default string.
    assert "dev-insecure-secret-change-me" not in body
    assert "•••• (configured)" in body


async def test_live_without_confirmation_returns_400_and_leaves_mode_unchanged(settings_client):
    client, _, runtime = settings_client

    response = await client.post("/settings/execution-mode/live")
    assert response.status_code == 400
    assert get_settings().vibetrading_execution_mode.value == "paper"
    assert runtime.restart_calls == 0


async def test_live_with_confirmation_switches_mode_and_restarts(settings_client):
    client, _, runtime = settings_client

    response = await client.post("/settings/execution-mode/live", data={"confirm_live": "yes"})
    assert response.status_code == 200
    assert get_settings().vibetrading_execution_mode.value == "live"
    assert runtime.restart_calls == 1


async def test_switching_back_to_paper_needs_no_confirmation(settings_client):
    client, _, runtime = settings_client

    await client.post("/settings/execution-mode/live", data={"confirm_live": "yes"})
    response = await client.post("/settings/execution-mode/paper")

    assert response.status_code == 200
    assert get_settings().vibetrading_execution_mode.value == "paper"
    assert runtime.restart_calls == 2


async def test_saving_execution_broker_section_persists_and_restarts(settings_client):
    client, session_factory, runtime = settings_client

    response = await client.post(
        "/settings/save/execution_broker",
        data={
            "dhan_client_id": "test-client",
            "dhan_access_token": "test-token",
            "enable_scheduler": "on",
            "agent_interval_research_sec": "900",
            "agent_interval_strategy_sec": "300",
            "agent_interval_stop_loss_monitor_sec": "30",
        },
    )
    assert response.status_code == 200
    assert get_settings().dhan_client_id == "test-client"
    assert get_settings().dhan_access_token == "test-token"
    assert runtime.restart_calls == 1

    async with session_factory() as session:
        row = await get_app_setting(session, "dhan_access_token")
        assert row is not None
        assert row.is_secret is True
        assert row.value != "test-token"  # encrypted at rest


async def test_saving_risk_limits_section_does_not_restart(settings_client):
    client, _, runtime = settings_client

    response = await client.post(
        "/settings/save/risk_limits",
        data={
            "risk_max_position_size_inr": "75000",
            "risk_max_pct_capital_per_stock": "0.10",
            "risk_max_concurrent_positions": "5",
            "risk_max_daily_loss_inr": "10000",
            "risk_mandatory_stop_loss_pct": "0.03",
            "risk_min_signal_confidence": "0.65",
            "risk_max_total_exposure_pct": "0.50",
        },
    )
    assert response.status_code == 200
    assert get_settings().risk_max_position_size_inr == 75000.0
    assert runtime.restart_calls == 0  # the whole point of the exemption


async def test_clearing_a_secret_via_checkbox_reverts_to_default(settings_client):
    client, _, _ = settings_client

    await client.post("/settings/save/execution_broker", data={"dhan_access_token": "some-token"})
    assert get_settings().dhan_access_token == "some-token"

    response = await client.post("/settings/save/execution_broker", data={"clear__dhan_access_token": "on"})
    assert response.status_code == 200
    assert get_settings().dhan_access_token == ""


async def test_blank_secret_field_leaves_existing_credential_unchanged(settings_client):
    client, _, _ = settings_client

    await client.post("/settings/save/execution_broker", data={"dhan_access_token": "original-token"})
    await client.post("/settings/save/execution_broker", data={})

    assert get_settings().dhan_access_token == "original-token"


async def test_watchlist_add_update_delete_round_trip_and_each_restarts(settings_client):
    client, _, runtime = settings_client

    add_response = await client.post(
        "/settings/watchlist/add", data={"symbol": "hdfcbank", "exchange": "NSE", "dhan_security_id": "1333"}
    )
    assert add_response.status_code == 200
    assert "HDFCBANK" in add_response.text
    assert runtime.restart_calls == 1

    update_response = await client.post(
        "/settings/watchlist/update/HDFCBANK", data={"exchange": "NSE", "dhan_security_id": "9999"}
    )
    assert update_response.status_code == 200
    assert "9999" in update_response.text
    assert runtime.restart_calls == 2

    delete_response = await client.post("/settings/watchlist/delete/HDFCBANK")
    assert delete_response.status_code == 200
    assert "HDFCBANK" not in delete_response.text
    assert runtime.restart_calls == 3


async def test_watchlist_add_requires_a_symbol(settings_client):
    client, _, runtime = settings_client

    response = await client.post("/settings/watchlist/add", data={"symbol": "  "})
    assert response.status_code == 400
    assert runtime.restart_calls == 0


async def test_failed_restart_reports_502_but_keeps_the_already_saved_setting(settings_client):
    client, _, runtime = settings_client
    runtime.fail = True

    response = await client.post("/settings/save/execution_broker", data={"dhan_client_id": "test-client"})

    assert response.status_code == 502
    assert "couldn't be applied" in response.text
    # The save itself already committed -- only *applying* it failed.
    assert get_settings().dhan_client_id == "test-client"
