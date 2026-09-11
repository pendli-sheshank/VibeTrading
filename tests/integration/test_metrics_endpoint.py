from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import make_test_engine, reset_schema, seed_test_users
from vibetrading.api.app import app
from vibetrading.api.deps import get_db


@pytest_asyncio.fixture
async def metrics_client():
    engine = make_test_engine()
    await reset_schema(engine)
    await seed_test_users(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_metrics_endpoint_is_reachable_without_auth_and_exposes_custom_counters(metrics_client):
    response = await metrics_client.get("/metrics")
    assert response.status_code == 200

    body = response.text
    for metric_name in [
        "vibetrading_orders_placed_total",
        "vibetrading_risk_rejections_total",
        "vibetrading_job_duration_seconds",
        "vibetrading_lease_acquisition_failures_total",
        "vibetrading_fencing_aborts_total",
        "vibetrading_token_replay_rejections_total",
        "vibetrading_circuit_breaker_opens_total",
    ]:
        assert metric_name in body, f"{metric_name} not exposed on /metrics"
