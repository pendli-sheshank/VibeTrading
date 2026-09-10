from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from vibetrading.api.app import app
from vibetrading.api.deps import get_broker, get_db
from vibetrading.auth.backend import current_active_user, current_dashboard_user
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.core.enums import ActionType, SignalSource
from vibetrading.core.models import Signal, Stock
from vibetrading.persistence.orm_models import Base, UserORM
from vibetrading.risk.engine import RiskEngine

FAKE_USER = UserORM(id=1, email="test@example.com", hashed_password="x", is_active=True)


@pytest_asyncio.fixture
async def api_client():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    broker = MockBrokerClient(seed=11)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[current_active_user] = lambda: FAKE_USER
    app.dependency_overrides[current_dashboard_user] = lambda: FAKE_USER

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, session_factory, broker

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_kill_switch_endpoint_blocks_next_risk_engine_call(api_client):
    client, session_factory, broker = api_client

    response = await client.post("/api/risk/kill-switch", json={"active": True})
    assert response.status_code == 200
    assert response.json()["kill_switch_active"] is True

    engine = RiskEngine(broker=broker, tenant_id=FAKE_USER.id)
    signal = Signal(
        stock_symbol="TCS",
        timestamp=datetime.now(UTC),
        source=SignalSource.STRATEGY_AGENT,
        action=ActionType.BUY,
        confidence=0.9,
        reasoning="test",
        reference_price=100.0,
    )

    async with session_factory() as session:
        result = await engine.approve_and_execute(session, signal, Stock(symbol="TCS"))
        await session.commit()

    assert result.approved is False
    assert result.risk_check.rule_results["kill_switch"] is False


async def test_dashboard_kill_switch_toggle_flips_state_and_next_call_rejected(api_client):
    client, session_factory, broker = api_client

    response = await client.post("/risk/kill-switch", data={"active": "true"})
    assert response.status_code == 200
    assert "ACTIVE" in response.text

    engine = RiskEngine(broker=broker, tenant_id=FAKE_USER.id)
    signal = Signal(
        stock_symbol="INFY",
        timestamp=datetime.now(UTC),
        source=SignalSource.STRATEGY_AGENT,
        action=ActionType.BUY,
        confidence=0.9,
        reasoning="test",
        reference_price=100.0,
    )

    async with session_factory() as session:
        result = await engine.approve_and_execute(session, signal, Stock(symbol="INFY"))
        await session.commit()

    assert result.approved is False


async def test_risk_state_endpoint_returns_config_and_defaults(api_client):
    client, _, _ = api_client
    response = await client.get("/api/risk/state")
    assert response.status_code == 200
    body = response.json()
    assert body["kill_switch_active"] is False
    assert "config" in body


async def test_backtest_run_endpoint_persists_and_returns_result(api_client):
    client, _, _ = api_client
    response = await client.post(
        "/api/backtest/run", json={"symbol": "RELIANCE", "days": 60, "warmup_days": 90}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stock_symbol"] == "RELIANCE"
    assert "total_trades" in body
    assert "win_rate" in body


async def test_dashboard_backtest_page_loads(api_client):
    client, _, _ = api_client
    response = await client.get("/backtest")
    assert response.status_code == 200
    assert "Run backtest" in response.text


async def test_dashboard_backtest_run_form_returns_result_html(api_client):
    client, _, _ = api_client
    response = await client.post("/backtest/run", data={"symbol": "TCS", "days": "60"})
    assert response.status_code == 200
    assert "TCS" in response.text
    assert "Total trades" in response.text


async def test_watchlist_page_loads(api_client):
    client, _, _ = api_client
    response = await client.get("/")
    assert response.status_code == 200
    assert "Strategy" in response.text


async def test_monitor_page_loads(api_client):
    client, _, _ = api_client
    response = await client.get("/monitor")
    assert response.status_code == 200
    assert "Monitor" in response.text


async def test_system_mode_endpoint(api_client):
    client, _, _ = api_client
    response = await client.get("/api/system/mode")
    assert response.status_code == 200
    assert response.json()["execution_mode"] == "paper"


@pytest.mark.parametrize("path", ["/api/watchlist", "/api/monitor/positions", "/api/monitor/orders"])
async def test_read_only_api_endpoints_are_reachable(api_client, path):
    client, _, _ = api_client
    response = await client.get(path)
    assert response.status_code == 200
