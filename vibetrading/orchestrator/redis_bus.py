from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

import redis.asyncio as redis

from vibetrading.config import get_settings

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "vibetrading:events:"


def channel_for_tenant(tenant_id: int) -> str:
    return f"{CHANNEL_PREFIX}{tenant_id}"


@lru_cache
def _get_redis_client() -> redis.Redis | None:
    """None (cached) when redis_url isn't configured -- the single-process,
    zero-Redis-dependency dev/test path. lru_cache means the client (and
    its connection pool) is built once per process, same pattern as
    persistence.db.get_engine()."""
    url = get_settings().redis_url
    if not url:
        return None
    return redis.from_url(url)


async def publish_event(tenant_id: int, message: dict) -> None:
    """Best-effort cross-worker fan-out -- a Redis outage or missing
    configuration degrades to "no live push," never an error the caller
    has to handle. Nothing durable (orders, risk state) depends on this;
    it's read fresh from Postgres on a page reload regardless."""
    client = _get_redis_client()
    if client is None:
        return
    try:
        await client.publish(channel_for_tenant(tenant_id), json.dumps(message))
    except Exception:
        logger.warning("Failed to publish event to Redis for tenant_id=%s", tenant_id, exc_info=True)


@asynccontextmanager
async def subscribe_tenant_channel(tenant_id: int) -> AsyncIterator[AsyncIterator[dict] | None]:
    """Yields an async iterator of decoded messages for this tenant's
    channel, or None if Redis isn't configured/reachable -- the caller
    (websocket.py) treats None as "local fan-out only, same as
    single-process mode" rather than failing the connection."""
    client = _get_redis_client()
    if client is None:
        yield None
        return

    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(channel_for_tenant(tenant_id))
    except Exception:
        logger.warning("Failed to subscribe to Redis for tenant_id=%s; falling back to local-only.", tenant_id, exc_info=True)
        yield None
        return

    async def _messages() -> AsyncIterator[dict]:
        try:
            async for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue
                try:
                    yield json.loads(raw["data"])
                except (TypeError, ValueError):
                    continue
        except Exception:
            logger.warning("Redis subscription for tenant_id=%s dropped.", tenant_id, exc_info=True)

    try:
        yield _messages()
    finally:
        await pubsub.unsubscribe(channel_for_tenant(tenant_id))
        await pubsub.aclose()


def reset_redis_client_cache() -> None:
    """Test-only: clears the lru_cache'd client so a test that changes
    settings().redis_url mid-run gets a fresh (or fresh-None) client."""
    _get_redis_client.cache_clear()
