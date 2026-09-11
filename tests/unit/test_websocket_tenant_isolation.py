from __future__ import annotations

import asyncio

from fastapi import WebSocketDisconnect

from vibetrading.api.websocket import websocket_endpoint
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.persistence.orm_models import UserORM


class FakeWebSocket:
    """Minimal stand-in for FastAPI's WebSocket -- just the three methods
    websocket_endpoint() actually calls. Avoids driving a real ASGI/HTTP
    transport (which, for a genuinely concurrent multi-connection test,
    would need two separate authenticated connections open at once --
    awkward with this codebase's sync TestClient/thread-based websocket
    support) while still exercising the real websocket_endpoint() code
    path end to end, in-process, in this test's own event loop.
    """

    def __init__(self):
        self.sent: list[dict] = []
        self._disconnect_event = asyncio.Event()

    async def accept(self) -> None:
        pass

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def receive_text(self) -> str:
        await self._disconnect_event.wait()
        raise WebSocketDisconnect()

    def disconnect(self) -> None:
        self._disconnect_event.set()


async def test_an_event_published_for_one_tenant_never_reaches_another_tenants_socket():
    """The regression test for the pre-Phase-21 leak: ConnectionManager.
    broadcast() used to send every event to every connected socket
    regardless of tenant. websocket_endpoint() must now only ever forward
    what event_bus.publish(..., tenant_id=X) sends to tenant X's queue."""
    user_a = UserORM(id=1, email="alice@example.com", hashed_password="x", is_active=True)
    user_b = UserORM(id=2, email="bob@example.com", hashed_password="x", is_active=True)

    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()

    task_a = asyncio.create_task(websocket_endpoint(ws_a, user_a))
    task_b = asyncio.create_task(websocket_endpoint(ws_b, user_b))
    await asyncio.sleep(0.05)  # let both connections subscribe

    try:
        await event_bus.publish({"type": "order", "stock_symbol": "TCS"}, tenant_id=1)
        await asyncio.sleep(0.05)

        assert ws_a.sent == [{"type": "order", "stock_symbol": "TCS"}]
        assert ws_b.sent == []  # tenant 2's socket must see nothing from tenant 1's event
    finally:
        ws_a.disconnect()
        ws_b.disconnect()
        await asyncio.wait_for(task_a, timeout=1.0)
        await asyncio.wait_for(task_b, timeout=1.0)


async def test_each_tenant_gets_its_own_events():
    user_a = UserORM(id=1, email="alice@example.com", hashed_password="x", is_active=True)
    user_b = UserORM(id=2, email="bob@example.com", hashed_password="x", is_active=True)

    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()

    task_a = asyncio.create_task(websocket_endpoint(ws_a, user_a))
    task_b = asyncio.create_task(websocket_endpoint(ws_b, user_b))
    await asyncio.sleep(0.05)

    try:
        await event_bus.publish({"type": "order", "stock_symbol": "TCS"}, tenant_id=1)
        await event_bus.publish({"type": "order", "stock_symbol": "INFY"}, tenant_id=2)
        await asyncio.sleep(0.05)

        assert ws_a.sent == [{"type": "order", "stock_symbol": "TCS"}]
        assert ws_b.sent == [{"type": "order", "stock_symbol": "INFY"}]
    finally:
        ws_a.disconnect()
        ws_b.disconnect()
        await asyncio.wait_for(task_a, timeout=1.0)
        await asyncio.wait_for(task_b, timeout=1.0)


async def test_disconnecting_one_socket_unsubscribes_it_cleanly():
    """After a socket disconnects, event_bus must not keep a dangling
    subscription for it -- and a later publish for that same tenant must
    not raise even though nothing is listening anymore."""
    user_a = UserORM(id=1, email="alice@example.com", hashed_password="x", is_active=True)
    ws_a = FakeWebSocket()

    task_a = asyncio.create_task(websocket_endpoint(ws_a, user_a))
    await asyncio.sleep(0.05)

    ws_a.disconnect()
    await asyncio.wait_for(task_a, timeout=1.0)

    assert event_bus._subscribers.get(1, []) == []

    await event_bus.publish({"type": "order"}, tenant_id=1)  # must not raise
