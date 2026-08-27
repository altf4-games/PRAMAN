"""Phase 1 acceptance criteria (the design spec):
- chain verifies over 100 events
- corrupting row 50 returns first_bad_index == 50
"""

from __future__ import annotations

from praman.core.ledger import GENESIS_HASH, append_event, verify_chain
from praman.crypto.canonical import canonical_hash
from praman.models import LedgerEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_genesis_prev_hash_is_zeros(db_session: AsyncSession) -> None:
    event = await append_event(db_session, "s1", "did:key:zAgent", "TEST_EVENT", {"n": 0})
    assert event.prev_hash == GENESIS_HASH


async def test_chain_verifies_over_100_events(db_session: AsyncSession) -> None:
    for i in range(100):
        await append_event(db_session, "s1", "did:key:zAgent", "TEST_EVENT", {"n": i})

    result = await verify_chain(db_session, "s1")
    assert result.ok
    assert result.checked == 100
    assert result.first_bad_index is None


async def test_corrupting_row_50_is_detected_at_index_50(db_session: AsyncSession) -> None:
    for i in range(100):
        await append_event(db_session, "s1", "did:key:zAgent", "TEST_EVENT", {"n": i})

    result = await db_session.execute(
        select(LedgerEvent).where(LedgerEvent.session_id == "s1").order_by(LedgerEvent.ts)
    )
    events = result.scalars().all()
    victim = events[50]
    victim.payload_json = {"n": "TAMPERED"}
    await db_session.commit()

    result = await verify_chain(db_session, "s1")
    assert not result.ok
    assert result.first_bad_index == 50


async def test_chains_are_independent_per_session(db_session: AsyncSession) -> None:
    for i in range(5):
        await append_event(db_session, "session-a", "did:key:zA", "TEST_EVENT", {"n": i})
    for i in range(5):
        await append_event(db_session, "session-b", "did:key:zB", "TEST_EVENT", {"n": i})

    result_a = await verify_chain(db_session, "session-a")
    result_b = await verify_chain(db_session, "session-b")
    assert result_a.ok and result_a.checked == 5
    assert result_b.ok and result_b.checked == 5

    result_all = await verify_chain(db_session, None)
    assert result_all.ok
    assert result_all.checked == 10


async def test_payload_hash_matches_canonical_hash(db_session: AsyncSession) -> None:
    payload = {"b": 2, "a": 1}
    event = await append_event(db_session, "s1", "did:key:zAgent", "TEST_EVENT", payload)
    assert event.payload_hash == canonical_hash(payload)
    # key order must not affect the hash (JCS canonicalization)
    assert event.payload_hash == canonical_hash({"a": 1, "b": 2})


async def test_allow_decisions_are_persisted_too(db_session: AsyncSession) -> None:
    event = await append_event(
        db_session, "s1", "did:key:zAgent", "GATE_DECISION", {"decision": "ALLOW", "rule_id": None}
    )
    assert event.event_type == "GATE_DECISION"
    assert event.payload_json["decision"] == "ALLOW"
