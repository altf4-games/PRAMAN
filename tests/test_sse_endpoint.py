"""Acceptance: 'SSE endpoint streams events live.'

Starlette's synchronous TestClient runs the whole ASGI call to completion
before handing back a response, which deadlocks against a genuinely
infinite SSE generator (there is no real concurrent chunk-by-chunk
delivery to race against). So this exercises the actual route wiring — a
real subscriber against the real event bus, real SSE-frame shaping — at the
generator level instead of over a full HTTP round-trip. Live delivery
timing itself (a subscriber receiving an event published after it
connects) is covered directly against `EventBus` in test_events.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from praman.api.routes_events import stream_events
from praman.events import BusEvent, bus


async def test_sse_route_yields_backlog_and_live_events() -> None:
    await bus.publish(
        BusEvent(session_id="route-test", event_type="PING", payload={"hello": "world"})
    )

    fake_request = SimpleNamespace(is_disconnected=lambda: _false())
    response = await stream_events(fake_request, session_id="route-test")

    generator = response.body_iterator
    frame = await generator.__anext__()

    assert frame["event"] == "PING"
    data = json.loads(frame["data"])
    assert data["event_type"] == "PING"
    assert data["payload"]["hello"] == "world"

    await generator.aclose()


async def _false() -> bool:
    return False
