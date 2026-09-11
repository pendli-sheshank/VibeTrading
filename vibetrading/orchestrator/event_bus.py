from __future__ import annotations

import asyncio

from vibetrading.orchestrator.redis_bus import publish_event


class EventBus:
    """Per-tenant in-process pub/sub (asyncio.Queue-based) — the dashboard's
    WebSocket subscribes to its own tenant's queue to forward messages to
    that one connected browser; the orchestrator (agent runs, signals,
    orders, kill-switch changes) publishes tagged with the tenant_id the
    event belongs to. Never broadcasts across tenants -- a subscriber only
    ever sees messages for the tenant_id it subscribed to.

    publish() also fans out to Redis (see redis_bus.py) so a WebSocket
    connected to one API replica sees an event published by a scheduler
    running in a different worker process. Redis is optional (None when
    unconfigured) and best-effort -- this in-process path is what keeps
    tests and single-process dev mode working with zero Redis dependency.
    """

    def __init__(self):
        self._subscribers: dict[int, list[asyncio.Queue]] = {}

    def subscribe(self, tenant_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(tenant_id, []).append(queue)
        return queue

    def unsubscribe(self, tenant_id: int, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(tenant_id)
        if not queues or queue not in queues:
            return
        queues.remove(queue)
        if not queues:
            del self._subscribers[tenant_id]

    async def publish(self, message: dict, tenant_id: int) -> None:
        for queue in list(self._subscribers.get(tenant_id, ())):
            await queue.put(message)
        await publish_event(tenant_id, message)


event_bus = EventBus()
