"""SSE endpoint — the live event stream the frontend's `/live` and
`/onboard` pages subscribe to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from praman.events import bus

router = APIRouter(tags=["events"])


@router.get("/events/stream")
async def stream_events(request: Request, session_id: str | None = None) -> EventSourceResponse:
    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        async for event in bus.subscribe(session_id):
            if await request.is_disconnected():
                break
            yield {"event": event.event_type, "data": event.to_sse_data()}

    return EventSourceResponse(event_generator())
