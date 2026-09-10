from __future__ import annotations

import asyncio

from vibetrading.orchestrator.event_bus import EventBus


async def test_a_subscriber_only_receives_messages_for_its_own_tenant():
    bus = EventBus()
    queue_1 = bus.subscribe(1)
    queue_2 = bus.subscribe(2)

    await bus.publish({"type": "signal", "stock_symbol": "TCS"}, tenant_id=1)

    assert queue_1.get_nowait() == {"type": "signal", "stock_symbol": "TCS"}
    assert queue_2.empty()


async def test_multiple_subscribers_for_the_same_tenant_all_receive_it():
    bus = EventBus()
    queue_a = bus.subscribe(1)
    queue_b = bus.subscribe(1)

    await bus.publish({"type": "order"}, tenant_id=1)

    assert queue_a.get_nowait() == {"type": "order"}
    assert queue_b.get_nowait() == {"type": "order"}


async def test_unsubscribe_stops_further_delivery():
    bus = EventBus()
    queue = bus.subscribe(1)
    bus.unsubscribe(1, queue)

    await bus.publish({"type": "order"}, tenant_id=1)

    assert queue.empty()


async def test_unsubscribe_is_a_no_op_for_an_unknown_queue_or_tenant():
    bus = EventBus()
    bus.unsubscribe(1, asyncio.Queue())  # never subscribed -- must not raise
    bus.unsubscribe(999, asyncio.Queue())  # unknown tenant -- must not raise


async def test_publishing_to_a_tenant_with_no_subscribers_is_a_no_op():
    bus = EventBus()
    await bus.publish({"type": "order"}, tenant_id=1)  # must not raise
