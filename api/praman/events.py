"""In-process SSE bus. Every `append_event` call publishes here so the
frontend's `/live` page and `/onboard` page can stream ledger activity as it
happens, without polling.

Single-process pub/sub is sufficient for this build — one API process, one
demo. It is not a durable queue: a subscriber connected before an event is
published receives it; nothing is replayed to late subscribers except
`get_recent` for the small backlog it keeps.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

_RECENT_BACKLOG = 50


@dataclass(frozen=True, slots=True)
class BusEvent:
    session_id: str
    event_type: str
    payload: dict[str, Any]

    def to_sse_data(self) -> str:
        return json.dumps({"event_type": self.event_type, "payload": self.payload})


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[BusEvent]] = set()
        self._recent: deque[BusEvent] = deque(maxlen=_RECENT_BACKLOG)
        self._lock = asyncio.Lock()

    async def publish(self, event: BusEvent) -> None:
        self._recent.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    def get_recent(self, session_id: str | None = None) -> list[BusEvent]:
        if session_id is None:
            return list(self._recent)
        return [e for e in self._recent if e.session_id == session_id]

    async def subscribe(self, session_id: str | None = None) -> AsyncIterator[BusEvent]:
        queue: asyncio.Queue[BusEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            for event in self.get_recent(session_id):
                yield event
            while True:
                event = await queue.get()
                if session_id is None or event.session_id == session_id:
                    yield event
        finally:
            self._subscribers.discard(queue)


bus = EventBus()
