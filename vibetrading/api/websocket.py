from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import Depends, WebSocket, WebSocketDisconnect

from vibetrading.auth.backend import current_websocket_user
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.orchestrator.redis_bus import subscribe_tenant_channel
from vibetrading.persistence.orm_models import UserORM

logger = logging.getLogger(__name__)


async def websocket_endpoint(websocket: WebSocket, user: UserORM = Depends(current_websocket_user)) -> None:
    """One connection per authenticated tenant. Subscribes only to this
    tenant's events.EventBus queue (in-process fan-out, e.g. from a
    scheduler running in THIS process) and, if Redis is configured, this
    tenant's Redis channel too (fan-out from a scheduler running in a
    DIFFERENT worker process) -- never a shared, all-tenants broadcast.
    Two independent background tasks forward each source to the browser;
    the main loop just waits on receive_text() to detect a disconnect,
    same as before this per-tenant split.
    """
    await websocket.accept()
    queue = event_bus.subscribe(user.id)

    async def pump_local() -> None:
        while True:
            message = await queue.get()
            try:
                await websocket.send_json(message)
            except Exception:  # noqa: BLE001 - a closing socket shouldn't crash this task
                return

    local_task = asyncio.create_task(pump_local())

    async with subscribe_tenant_channel(user.id) as redis_messages:
        redis_task: asyncio.Task | None = None
        if redis_messages is not None:

            async def pump_redis() -> None:
                async for message in redis_messages:
                    try:
                        await websocket.send_json(message)
                    except Exception:  # noqa: BLE001 - a closing socket shouldn't crash this task
                        return

            redis_task = asyncio.create_task(pump_redis())

        try:
            while True:
                # The dashboard client doesn't send anything meaningful;
                # this just keeps the connection open and detects
                # disconnects.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            event_bus.unsubscribe(user.id, queue)
            for task in (local_task, redis_task):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
