from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import make_test_engine, reset_schema, seed_test_users
from vibetrading.api.app import app
from vibetrading.api.deps import get_broker, get_db, get_market_data
from vibetrading.auth.backend import current_active_user, current_dashboard_user
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.core.enums import ActionType, AgentType, SignalSource
from vibetrading.core.models import Signal, Stock
from vibetrading.marketdata.providers import SimulatedMarketDataProvider
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import get_latest_agent_output, upsert_stock
from vibetrading.risk.engine import RiskEngine

FAKE_USER = UserORM(id=1, email="test@example.com", hashed_password="x", is_active=True)


@pytest_asyncio.fixture
async def api_client():
    engine = make_test_engine()
    await reset_schema(engine)
    await seed_test_users(engine)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    broker = MockBrokerClient(seed=11)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_broker] = lambda: broker
    app.dependency_overrides[get_market_data] = lambda: SimulatedMarketDataProvider(seed=11)
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


async def _seed_stock(session_factory, symbol: str, **kwargs) -> None:
    async with session_factory() as session:
        await upsert_stock(session, FAKE_USER.id, Stock(symbol=symbol, **kwargs))
        await session.commit()


async def test_backtest_run_endpoint_persists_and_returns_result(api_client):
    client, session_factory, _ = api_client
    # Backtesting resolves the stock from the watchlist first, so an unknown
    # symbol is a clear 404 rather than an attempted run.
    await _seed_stock(session_factory, "RELIANCE")

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
    client, session_factory, _ = api_client
    await _seed_stock(session_factory, "TCS")

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


async def test_stock_detail_page_loads_with_no_analysis_yet(api_client):
    client, session_factory, _ = api_client
    async with session_factory() as session:
        await upsert_stock(session, FAKE_USER.id, Stock(symbol="RELIANCE"))
        await session.commit()

    response = await client.get("/stock/RELIANCE")
    assert response.status_code == 200
    assert "Analyze now" in response.text
    assert "No technical read yet" in response.text
    assert "No research read yet" in response.text


async def test_stock_analyze_endpoint_runs_both_agents_and_persists(api_client):
    client, session_factory, _ = api_client
    async with session_factory() as session:
        await upsert_stock(session, FAKE_USER.id, Stock(symbol="RELIANCE"))
        await session.commit()

    response = await client.post("/stock/RELIANCE/analyze")
    assert response.status_code == 200
    assert "Technical Agent" in response.text
    assert "Research Agent" in response.text
    assert "No technical read yet" not in response.text
    assert "No research read yet" not in response.text

    async with session_factory() as session:
        technical = await get_latest_agent_output(session, FAKE_USER.id, "RELIANCE", AgentType.TECHNICAL.value)
        research = await get_latest_agent_output(session, FAKE_USER.id, "RELIANCE", AgentType.RESEARCH.value)
    assert technical is not None
    assert research is not None


async def test_stock_analyze_endpoint_404s_for_a_symbol_not_on_the_watchlist(api_client):
    client, _, _ = api_client
    response = await client.post("/stock/NOPE/analyze")
    assert response.status_code == 404


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
