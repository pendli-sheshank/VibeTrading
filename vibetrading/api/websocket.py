from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import Depends, WebSocket, WebSocketDisconnect

from vibetrading.auth.backend import current_active_user
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.persistence.orm_models import UserORM

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks connected dashboard clients and broadcasts event_bus messages
    to all of them. One instance per process (see api/app.py's lifespan,
    which pumps event_bus -> this manager)."""

    def __init__(self):
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for websocket in self._connections:
            try:
                await websocket.send_json(message)
            except Exception:  # noqa: BLE001 - a dead socket shouldn't break the broadcast
                dead.append(websocket)
        for websocket in dead:
            self._connections.discard(websocket)


manager = ConnectionManager()


async def websocket_endpoint(websocket: WebSocket, user: UserORM = Depends(current_active_user)) -> None:
    await manager.connect(websocket)
    try:
        while True:
            # The dashboard client doesn't send anything meaningful; this
            # just keeps the connection open and detects disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def forward_event_bus_to_websockets() -> None:
    """Background task (started in api/app.py's lifespan): pumps every
    event_bus message to every connected dashboard client."""
    queue = event_bus.subscribe()
    try:
        while True:
            message = await queue.get()
            await manager.broadcast(message)
    except asyncio.CancelledError:
        pass
    finally:
        event_bus.unsubscribe(queue)


@contextlib.asynccontextmanager
async def run_event_forwarder():
    task = asyncio.create_task(forward_event_bus_to_websockets())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
