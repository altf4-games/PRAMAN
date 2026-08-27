"""The hash-chained ledger. Every gate decision, onboarding step, quote,
order transition, and cooling-off event is appended here — ALLOW included,
per the design spec's non-negotiable rule that every gate decision persists.

`append_event` is the only way rows enter this table. Each row's
`chain_hash` commits to the previous row's `chain_hash` and to this row's
own `payload_hash`, so altering any historical payload — or reordering,
deleting, or inserting a row — breaks every chain hash from that point
forward. `verify_chain` recomputes the whole chain and reports exactly
where it first diverges.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.crypto.canonical import canonical_hash, sha256_hex
from praman.events import BusEvent, bus
from praman.models import LedgerEvent
from praman.timeutil import as_aware_utc as _as_aware_utc

GENESIS_HASH = "0" * 64


# Per-session locks serialize append_event so chain order is deterministic
# even under concurrent requests within this one process (the design spec's "one
# transaction, row-locked per session" — here achieved with an in-process
# lock plus a strictly-increasing timestamp, which is sufficient for a
# single-instance deployment).
_session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass(frozen=True, slots=True)
class ChainResult:
    ok: bool
    first_bad_index: int | None
    expected: str | None
    actual: str | None
    checked: int


async def append_event(
    session: AsyncSession,
    session_id: str,
    agent_did: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> LedgerEvent:
    lock = _session_locks[session_id]
    async with lock:
        result = await session.execute(
            select(LedgerEvent)
            .where(LedgerEvent.session_id == session_id)
            .order_by(LedgerEvent.ts.desc())
            .limit(1)
        )
        last = result.scalar_one_or_none()
        prev_hash = last.chain_hash if last is not None else GENESIS_HASH

        now = datetime.now(UTC)
        if last is not None and now <= _as_aware_utc(last.ts):
            now = _as_aware_utc(last.ts) + timedelta(microseconds=1)

        payload_hash = canonical_hash(payload)
        chain_hash = sha256_hex((prev_hash + payload_hash).encode())

        event = LedgerEvent(
            ts=now,
            session_id=session_id,
            agent_did=agent_did,
            event_type=event_type,
            payload_json=payload,
            payload_hash=payload_hash,
            prev_hash=prev_hash,
            chain_hash=chain_hash,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)

    await bus.publish(
        BusEvent(
            session_id=session_id,
            event_type=event_type,
            payload={
                "event_id": event.event_id,
                "agent_did": agent_did,
                "ts": event.ts.isoformat(),
                "chain_hash": event.chain_hash,
                **payload,
            },
        )
    )
    return event


async def verify_chain(session: AsyncSession, session_id: str | None = None) -> ChainResult:
    """Walk events in (ts, event_id) order, recompute both hashes."""
    stmt = select(LedgerEvent).order_by(LedgerEvent.ts, LedgerEvent.event_id)
    if session_id is not None:
        stmt = stmt.where(LedgerEvent.session_id == session_id)
    result = await session.execute(stmt)
    events = result.scalars().all()

    # Chains are per-session; track prev_hash independently per session_id
    # so verifying "all sessions" doesn't cross-contaminate their chains.
    prev_hash_by_session: dict[str, str] = {}

    for index, event in enumerate(events):
        expected_prev = prev_hash_by_session.get(event.session_id, GENESIS_HASH)
        expected_payload_hash = canonical_hash(event.payload_json)
        expected_chain_hash = sha256_hex((expected_prev + expected_payload_hash).encode())

        if event.payload_hash != expected_payload_hash or event.chain_hash != expected_chain_hash:
            return ChainResult(
                ok=False,
                first_bad_index=index,
                expected=expected_chain_hash,
                actual=event.chain_hash,
                checked=index + 1,
            )

        prev_hash_by_session[event.session_id] = event.chain_hash

    return ChainResult(
        ok=True, first_bad_index=None, expected=None, actual=None, checked=len(events)
    )


async def get_session_events(session: AsyncSession, session_id: str) -> list[LedgerEvent]:
    result = await session.execute(
        select(LedgerEvent)
        .where(LedgerEvent.session_id == session_id)
        .order_by(LedgerEvent.ts, LedgerEvent.event_id)
    )
    return list(result.scalars().all())


async def dispute_pack_events(session: AsyncSession, session_id: str) -> dict[str, Any]:
    """Minimal dispute-pack fragment: the session's ordered ledger trail plus
    its chain-verification result. Phase 8 assembles the full pack (envelope,
    cart mandate, quote provenance, reversibility breakdown, cooling-off
    timeline, step-up record) around this.
    """
    events = await get_session_events(session, session_id)
    chain_result = await verify_chain(session, session_id)
    return {
        "session_id": session_id,
        "events": [
            {
                "event_id": e.event_id,
                "ts": e.ts.isoformat(),
                "event_type": e.event_type,
                "agent_did": e.agent_did,
                "payload": e.payload_json,
                "payload_hash": e.payload_hash,
                "prev_hash": e.prev_hash,
                "chain_hash": e.chain_hash,
            }
            for e in events
        ],
        "chain_verified": chain_result.ok,
    }
