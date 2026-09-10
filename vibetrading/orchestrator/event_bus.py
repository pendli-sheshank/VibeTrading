from __future__ import annotations

import asyncio


class EventBus:
    """Minimal in-process pub/sub (asyncio.Queue-based) — no external broker
    needed at this scale (see the project plan's scheduling rationale).
    The dashboard's WebSocket subscribes to forward every published message
    to connected browser clients; the orchestrator (agent runs, signals,
    orders, kill-switch changes) publishes to it.
    """

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def publish(self, message: dict) -> None:
        for queue in list(self._subscribers):
            await queue.put(message)


event_bus = EventBus()
