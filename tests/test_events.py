from __future__ import annotations

import asyncio

from praman.events import BusEvent, EventBus


async def test_subscriber_receives_published_event() -> None:
    local_bus = EventBus()
    received: list[BusEvent] = []

    async def collect_one() -> None:
        async for event in local_bus.subscribe("s1"):
            received.append(event)
            break

    task = asyncio.create_task(collect_one())
    await asyncio.sleep(0.01)  # let the subscriber register
    await local_bus.publish(BusEvent(session_id="s1", event_type="TEST", payload={"n": 1}))
    await asyncio.wait_for(task, timeout=1)

    assert len(received) == 1
    assert received[0].payload == {"n": 1}


async def test_subscriber_filters_by_session_id() -> None:
    local_bus = EventBus()
    received: list[BusEvent] = []

    async def collect_one() -> None:
        async for event in local_bus.subscribe("s1"):
            received.append(event)
            break

    task = asyncio.create_task(collect_one())
    await asyncio.sleep(0.01)
    await local_bus.publish(BusEvent(session_id="other", event_type="TEST", payload={}))
    await local_bus.publish(BusEvent(session_id="s1", event_type="TEST", payload={"ok": True}))
    await asyncio.wait_for(task, timeout=1)

    assert received[0].session_id == "s1"


async def test_late_subscriber_gets_recent_backlog() -> None:
    local_bus = EventBus()
    await local_bus.publish(BusEvent(session_id="s1", event_type="TEST", payload={"n": 1}))

    events = []
    async for event in local_bus.subscribe("s1"):
        events.append(event)
        break

    assert len(events) == 1
    assert events[0].payload == {"n": 1}
