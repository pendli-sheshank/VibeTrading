"""End-to-end coverage of the two flows that were broken in the UI:

  Analyze  -> market data -> agents -> response -> rendered fragment
  Backtest -> data -> engine -> results (or a stated reason, never a 500)

Every test here drives the real ASGI app over HTTP, so it exercises the same
request/response path the browser does.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import make_test_engine, reset_schema, seed_test_users
from tests.unit.test_market_snapshot import StubBroker, make_candles
from vibetrading.api.app import app
from vibetrading.api.deps import get_broker, get_db
from vibetrading.auth.backend import current_active_user, current_dashboard_user
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.core.enums import AgentType
from vibetrading.core.exceptions import BrokerError
from vibetrading.core.models import Stock
from vibetrading.core.reliability import CircuitBreakerOpenError
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import get_latest_agent_output, upsert_stock

FAKE_USER = UserORM(id=1, email="test@example.com", hashed_password="x", is_active=True)
WATCHLIST = ["RELIANCE", "TCS", "INFY"]


@pytest_asyncio.fixture
async def flow_client():
    engine = make_test_engine()
    await reset_schema(engine)
    await seed_test_users(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    broker_box = {"broker": MockBrokerClient(seed=21)}

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_broker] = lambda: broker_box["broker"]
    app.dependency_overrides[current_active_user] = lambda: FAKE_USER
    app.dependency_overrides[current_dashboard_user] = lambda: FAKE_USER

    async with session_factory() as session:
        for symbol in WATCHLIST:
            await upsert_stock(session, FAKE_USER.id, Stock(symbol=symbol, dhan_security_id="1234"))
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, session_factory, broker_box

    app.dependency_overrides.clear()
    await engine.dispose()


# --------------------------------------------------------------------------
# Analyze
# --------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", WATCHLIST)
async def test_analyze_returns_a_real_confidence_for_each_stock(flow_client, symbol):
    """Multiple stocks, not just one -- each must analyze on its own data."""
    client, session_factory, _ = flow_client

    response = await client.post(f"/stock/{symbol}/analyze")

    assert response.status_code == 200
    body = response.text
    assert "Technical Agent" in body
    assert "confidence" in body
    assert "DATA_INSUFFICIENT" not in body
    # A real reading, not the 0% placeholder the old code produced. The word
    # boundary matters: "90% confidence" contains "0% confidence".
    assert re.search(r"\b0% confidence", body) is None

    async with session_factory() as session:
        stored = await get_latest_agent_output(session, FAKE_USER.id, symbol, AgentType.TECHNICAL.value)
    assert stored is not None
    assert stored.confidence > 0.0


async def test_analyze_json_api_returns_structured_data_not_html(flow_client):
    """The API surface answers JSON, with the market data the analysis used."""
    client, _, _ = flow_client

    response = await client.post("/api/strategy/RELIANCE/analyze")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["analyzed"] is True
    assert 0.0 < body["confidence"] <= 1.0
    assert body["direction"] in ("bullish", "bearish", "neutral", "unknown")
    assert body["technical"]["indicators"]["rsi_14"] is not None
    assert body["market_data"]["quote"]["status"] == "simulated"


async def test_analyze_with_insufficient_data_renders_data_insufficient_not_a_zero_score(flow_client):
    client, _, broker_box = flow_client
    broker_box["broker"] = StubBroker(candles=make_candles(4))

    response = await client.post("/stock/RELIANCE/analyze")

    assert response.status_code == 200
    assert "DATA_INSUFFICIENT" in response.text
    assert "no analysis run" in response.text
    assert "0%" not in response.text


async def test_analyze_with_a_broker_error_shows_the_reason_instead_of_failing_silently(flow_client):
    client, _, broker_box = flow_client
    broker_box["broker"] = StubBroker(
        candle_error=BrokerError("Dhan rejected historical_daily_data: DH-905 : Invalid security id")
    )

    response = await client.post("/stock/RELIANCE/analyze")

    # 200 with an explained state card: htmx swaps it in and the user reads
    # the actual cause, rather than a 500 whose body htmx discards.
    assert response.status_code == 200
    assert "DH-905" in response.text
    assert "state-card" in response.text


async def test_analyze_unknown_symbol_is_a_404_not_a_crash(flow_client):
    client, _, _ = flow_client
    response = await client.post("/stock/NOTREAL/analyze")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


async def test_market_data_panel_shows_prices_indicators_and_a_status(flow_client):
    client, _, _ = flow_client

    response = await client.get("/stock/RELIANCE/market-data")

    assert response.status_code == 200
    body = response.text
    for label in ["Last price", "Volume", "RSI (14)", "MACD line", "ATR (14)", "Bollinger upper", "Support"]:
        assert label in body
    assert "SIMULATED" in body  # labelled, never passed off as live
    assert "Option chain" in body


async def test_market_data_json_reports_per_section_status(flow_client):
    client, _, _ = flow_client

    response = await client.get("/api/strategy/RELIANCE/market-data")

    assert response.status_code == 200
    body = response.json()
    assert body["quote"]["status"] == "simulated"
    assert body["indicator_status"] == "simulated"
    assert body["option_chain"]["status"] == "unavailable"
    assert body["option_chain"]["strikes"] == []
    assert body["indicators"]["atr_14"] is not None


async def test_stock_page_requests_market_data_before_any_analysis(flow_client):
    client, _, _ = flow_client

    response = await client.get("/stock/RELIANCE")

    assert response.status_code == 200
    assert '/stock/RELIANCE/market-data' in response.text
    assert 'hx-trigger="load"' in response.text
    assert "Loading live market data" in response.text


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", WATCHLIST)
async def test_backtest_completes_for_each_stock(flow_client, symbol):
    client, _, _ = flow_client

    response = await client.post("/backtest/run", data={"symbol": symbol, "days": "120"})

    assert response.status_code == 200
    assert "Total trades" in response.text
    assert "could not run" not in response.text


async def test_backtest_rejects_an_out_of_range_day_count_with_a_readable_message(flow_client):
    client, _, _ = flow_client

    response = await client.post("/backtest/run", data={"symbol": "RELIANCE", "days": "5"})

    assert response.status_code == 400
    assert "Days must be between" in response.text
    assert "state-card" in response.text


async def test_backtest_for_a_stock_not_on_the_watchlist_is_a_clear_404(flow_client):
    client, _, _ = flow_client

    response = await client.post("/backtest/run", data={"symbol": "NOTREAL", "days": "120"})

    assert response.status_code == 404
    assert "not on your watchlist" in response.text


async def test_backtest_broker_failure_renders_502_card_instead_of_a_500(flow_client):
    """The reported bug: this path used to be an unhandled AttributeError
    from the Dhan response parser, i.e. a bare 500."""
    client, _, broker_box = flow_client
    broker_box["broker"] = StubBroker(
        candle_error=BrokerError("Dhan rejected historical_daily_data: DH-905 : Invalid security id")
    )

    response = await client.post("/backtest/run", data={"symbol": "RELIANCE", "days": "120"})

    assert response.status_code == 502
    assert "DH-905" in response.text
    assert "could not run" in response.text


async def test_backtest_with_too_little_history_says_data_insufficient(flow_client):
    client, _, broker_box = flow_client
    broker_box["broker"] = StubBroker(candles=make_candles(5))

    response = await client.post("/backtest/run", data={"symbol": "RELIANCE", "days": "120"})

    assert response.status_code == 200
    assert "DATA_INSUFFICIENT" in response.text


async def test_backtest_json_api_validates_parameters(flow_client):
    client, _, _ = flow_client

    too_short = await client.post("/api/backtest/run", json={"symbol": "RELIANCE", "days": 1})
    assert too_short.status_code == 422

    unknown = await client.post("/api/backtest/run", json={"symbol": "NOTREAL", "days": 120})
    assert unknown.status_code == 404

    bad_range = await client.post(
        "/api/backtest/run",
        json={"symbol": "RELIANCE", "start_date": "2026-01-01T00:00:00Z", "end_date": "2025-01-01T00:00:00Z"},
    )
    assert bad_range.status_code == 422


async def test_backtest_json_api_maps_broker_failure_to_502_with_the_reason(flow_client):
    client, _, broker_box = flow_client
    broker_box["broker"] = StubBroker(candle_error=BrokerError("DH-905 : Invalid security id"))

    response = await client.post("/api/backtest/run", json={"symbol": "RELIANCE", "days": 120})

    assert response.status_code == 502
    assert "DH-905" in response.json()["detail"]


async def test_backtest_with_an_open_circuit_breaker_renders_503_not_a_500(flow_client):
    client, _, broker_box = flow_client

    class TrippedBroker(StubBroker):
        async def get_historical_candles(self, stock, interval, from_date, to_date):
            raise CircuitBreakerOpenError("Circuit 'dhan:123' is open (>= 5 consecutive failures)")

    broker_box["broker"] = TrippedBroker()

    response = await client.post("/backtest/run", data={"symbol": "RELIANCE", "days": "120"})

    assert response.status_code == 503
    assert "BROKER_UNAVAILABLE" in response.text
    assert "is open" in response.text


async def test_market_data_with_an_open_circuit_breaker_still_renders_a_panel(flow_client):
    client, _, broker_box = flow_client

    class TrippedBroker(StubBroker):
        async def get_historical_candles(self, stock, interval, from_date, to_date):
            raise CircuitBreakerOpenError("Circuit 'dhan:123' is open")

        async def get_quote(self, stock):
            raise CircuitBreakerOpenError("Circuit 'dhan:123' is open")

    broker_box["broker"] = TrippedBroker()

    response = await client.get("/stock/RELIANCE/market-data")

    assert response.status_code == 200
    assert "is open" in response.text
    assert 'id="market-data"' in response.text  # panel survives, so Refresh still works
