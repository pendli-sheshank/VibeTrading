from __future__ import annotations

import asyncio
import os

import pytest
import redis as sync_redis

from vibetrading.config import get_settings
from vibetrading.orchestrator.redis_bus import (
    channel_for_tenant,
    publish_event,
    reset_redis_client_cache,
    subscribe_tenant_channel,
)

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def _redis_reachable() -> bool:
    try:
        return sync_redis.Redis.from_url(REDIS_URL, socket_connect_timeout=0.5).ping()
    except Exception:  # noqa: BLE001 - reachability probe, any failure means "skip"
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_REDIS_TESTS") == "1" or not _redis_reachable(),
    reason=(
        "Redis-backed tests need a real Redis instance (see the CI redis "
        "service, or run redis-server locally) -- skip when unreachable "
        "rather than failing; set SKIP_REDIS_TESTS=1 to skip explicitly."
    ),
)


@pytest.fixture(autouse=True)
def _redis_configured(monkeypatch):
    """Points settings().redis_url at a real local Redis instance (a
    scratch DB index, 15, kept separate from anything else on this Redis
    server) for the duration of each test in this file, and clears the
    module-level client cache before and after so tests don't leak a
    stale client between each other or into unrelated tests."""
    monkeypatch.setattr(get_settings(), "redis_url", REDIS_URL)
    reset_redis_client_cache()
    yield
    reset_redis_client_cache()


def test_channel_for_tenant_is_namespaced_per_tenant():
    assert channel_for_tenant(1) == "vibetrading:events:1"
    assert channel_for_tenant(2) == "vibetrading:events:2"
    assert channel_for_tenant(1) != channel_for_tenant(2)


async def test_publish_event_is_a_no_op_without_configuration(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", None)
    reset_redis_client_cache()
    await publish_event(1, {"type": "signal"})  # must not raise


async def test_subscribe_without_configuration_yields_none(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", None)
    reset_redis_client_cache()
    async with subscribe_tenant_channel(1) as messages:
        assert messages is None


async def test_a_published_message_reaches_a_subscriber_on_the_same_tenant_channel():
    async with subscribe_tenant_channel(1) as messages:
        assert messages is not None

        async def publish_after_a_beat():
            await asyncio.sleep(0.1)
            await publish_event(1, {"type": "order", "stock_symbol": "TCS"})

        publisher = asyncio.create_task(publish_after_a_beat())
        try:
            message = await asyncio.wait_for(anext(messages), timeout=2.0)
        finally:
            await publisher

    assert message == {"type": "order", "stock_symbol": "TCS"}


async def test_a_published_message_does_not_reach_a_different_tenants_subscriber():
    async with subscribe_tenant_channel(2) as messages:
        assert messages is not None

        await publish_event(1, {"type": "order", "stock_symbol": "TCS"})

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(messages), timeout=0.3)
