from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from praman.core.registry import LocalRegistry, verify_agent_request
from praman.crypto.keys import generate_keypair, sign
from praman.models import Agent
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _make_agent(session: AsyncSession, *, revoked: bool = False) -> tuple[Agent, str]:
    priv, pub = generate_keypair()
    agent = Agent(
        agent_did="did:key:zAgentTest",
        operator="Test Operator",
        public_key=pub,
        trust_tier="standard",
        max_txn_paise=100_000,
        daily_cap_paise=1_000_000,
        registered_at=NOW - timedelta(days=1),
        revoked_at=NOW - timedelta(hours=1) if revoked else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent, priv


def _sign_request(
    private_key_hex: str, method: str, body: bytes, timestamp: str, nonce: str
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()
    return sign(private_key_hex, message)


# --- LocalRegistry ---


async def test_resolve_returns_none_for_unknown_agent(db_session: AsyncSession) -> None:
    registry = LocalRegistry(db_session)
    assert await registry.resolve("did:key:zUnknown") is None


async def test_resolve_returns_record_for_known_agent(db_session: AsyncSession) -> None:
    agent, _priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    record = await registry.resolve(agent.agent_did)
    assert record is not None
    assert record.agent_did == agent.agent_did
    assert record.public_key == agent.public_key


async def test_is_revoked_true_for_unknown_agent(db_session: AsyncSession) -> None:
    registry = LocalRegistry(db_session)
    assert await registry.is_revoked("did:key:zUnknown") is True


async def test_is_revoked_true_for_revoked_agent(db_session: AsyncSession) -> None:
    agent, _priv = await _make_agent(db_session, revoked=True)
    registry = LocalRegistry(db_session)
    assert await registry.is_revoked(agent.agent_did) is True


async def test_is_revoked_false_for_active_agent(db_session: AsyncSession) -> None:
    agent, _priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    assert await registry.is_revoked(agent.agent_did) is False


# --- verify_agent_request ---


async def test_verify_agent_request_allows_valid_signed_request(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    agent, priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    timestamp = NOW.isoformat()
    nonce = "nonce-1"
    body = b'{"sku":"abc"}'
    signature = _sign_request(priv, "POST", body, timestamp, nonce)

    result = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        now=NOW,
    )
    assert result.decision == "ALLOW"


async def test_verify_agent_request_blocks_unknown_agent(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    registry = LocalRegistry(db_session)
    result = await verify_agent_request(
        registry,
        redis,
        agent_did="did:key:zUnknown",
        method="POST",
        body=b"{}",
        timestamp=NOW.isoformat(),
        nonce="n1",
        signature="deadbeef",
        now=NOW,
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "AGENT_REVOKED"


async def test_verify_agent_request_blocks_revoked_agent(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    agent, priv = await _make_agent(db_session, revoked=True)
    registry = LocalRegistry(db_session)
    timestamp = NOW.isoformat()
    nonce = "n1"
    body = b"{}"
    signature = _sign_request(priv, "POST", body, timestamp, nonce)

    result = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        now=NOW,
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "AGENT_REVOKED"


async def test_verify_agent_request_blocks_excessive_clock_skew(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    agent, priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    stale_timestamp = (NOW - timedelta(seconds=120)).isoformat()
    nonce = "n1"
    body = b"{}"
    signature = _sign_request(priv, "POST", body, stale_timestamp, nonce)

    result = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=body,
        timestamp=stale_timestamp,
        nonce=nonce,
        signature=signature,
        now=NOW,
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "CLOCK_SKEW_EXCEEDED"


async def test_verify_agent_request_allows_at_exact_skew_boundary(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    agent, priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    boundary_timestamp = (NOW - timedelta(seconds=60)).isoformat()
    nonce = "n1"
    body = b"{}"
    signature = _sign_request(priv, "POST", body, boundary_timestamp, nonce)

    result = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=body,
        timestamp=boundary_timestamp,
        nonce=nonce,
        signature=signature,
        now=NOW,
    )
    assert result.decision == "ALLOW"


async def test_verify_agent_request_blocks_replayed_nonce(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    agent, priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    timestamp = NOW.isoformat()
    nonce = "n1"
    body = b"{}"
    signature = _sign_request(priv, "POST", body, timestamp, nonce)

    first = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        now=NOW,
    )
    assert first.decision == "ALLOW"

    second = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        now=NOW,
    )
    assert second.decision == "BLOCK"
    assert second.reason_code == "NONCE_REPLAYED"


async def test_verify_agent_request_blocks_invalid_signature(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    agent, _priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    timestamp = NOW.isoformat()

    result = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=b"{}",
        timestamp=timestamp,
        nonce="n1",
        signature="00" * 64,  # well-formed hex, wrong signature
        now=NOW,
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "AGENT_SIG_INVALID"


async def test_verify_agent_request_blocks_tampered_body(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    agent, priv = await _make_agent(db_session)
    registry = LocalRegistry(db_session)
    timestamp = NOW.isoformat()
    nonce = "n1"
    signature = _sign_request(priv, "POST", b'{"sku":"abc"}', timestamp, nonce)

    result = await verify_agent_request(
        registry,
        redis,
        agent_did=agent.agent_did,
        method="POST",
        body=b'{"sku":"tampered"}',
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        now=NOW,
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "AGENT_SIG_INVALID"
