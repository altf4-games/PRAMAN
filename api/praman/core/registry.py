"""Agent Registry — a Protocol shaped for NPCI's forthcoming UAP (Unified
Agent Protocol), which will register, verify, and authorise agents atop
UPI Circle's delegated-payments model, pending RBI approval. `LocalRegistry`
is the only implementation today, backed by the `agents` table; when UAP
ships, it becomes a second implementation of the same interface and
nothing above this line changes.

Also home to agent request authentication: every agent request carries a
detached Ed25519 signature over `(method, sha256(body), timestamp, nonce)`.
Clock skew beyond 60s is rejected; nonces are tracked in Redis to reject
replay.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.config import AGENT_CLOCK_SKEW_TOLERANCE_S
from praman.core.gate_types import GateResult, allow
from praman.crypto.keys import verify
from praman.models import Agent

# Nonces are kept long enough to outlive the clock-skew window on both
# sides (a request timestamped up to 60s in the past or future must still
# have its nonce rejected as a replay for the full window it was valid).
NONCE_TTL_S = 2 * AGENT_CLOCK_SKEW_TOLERANCE_S + 10


@dataclass(frozen=True, slots=True)
class AgentRecord:
    agent_did: str
    operator: str
    public_key: str
    trust_tier: str
    max_txn_paise: int
    daily_cap_paise: int
    revoked_at: datetime | None


class AgentRegistry(Protocol):
    async def resolve(self, agent_did: str) -> AgentRecord | None: ...

    async def is_revoked(self, agent_did: str) -> bool: ...


class LocalRegistry:
    """The only implementation today. Backed by the `agents` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, agent_did: str) -> AgentRecord | None:
        result = await self._session.execute(select(Agent).where(Agent.agent_did == agent_did))
        agent = result.scalar_one_or_none()
        if agent is None:
            return None
        return AgentRecord(
            agent_did=agent.agent_did,
            operator=agent.operator,
            public_key=agent.public_key,
            trust_tier=agent.trust_tier,
            max_txn_paise=agent.max_txn_paise,
            daily_cap_paise=agent.daily_cap_paise,
            revoked_at=agent.revoked_at,
        )

    async def is_revoked(self, agent_did: str) -> bool:
        record = await self.resolve(agent_did)
        if record is None:
            # Fail closed: an agent the registry has never heard of is
            # treated as revoked, not as trusted-by-default.
            return True
        return record.revoked_at is not None


def _signing_message(method: str, body: bytes, timestamp: str, nonce: str) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()


async def verify_agent_request(
    registry: AgentRegistry,
    redis: Redis,
    *,
    agent_did: str,
    method: str,
    body: bytes,
    timestamp: str,
    nonce: str,
    signature: str,
    now: datetime,
) -> GateResult:
    """R01 (signature) + R02 (registered/not revoked) territory, plus the
    replay/skew checks that guard them. Ordered; first failure wins."""
    record = await registry.resolve(agent_did)
    if record is None:
        return GateResult(
            decision="BLOCK",
            reason_code="AGENT_REVOKED",
            detail=f"agent_did {agent_did!r} is not registered.",
            remedy="Register the agent before making requests.",
            rule_id="R02",
        )

    if record.revoked_at is not None:
        return GateResult(
            decision="BLOCK",
            reason_code="AGENT_REVOKED",
            detail=f"agent_did {agent_did!r} was revoked at {record.revoked_at.isoformat()}.",
            remedy="Use a currently-registered, non-revoked agent identity.",
            rule_id="R02",
        )

    try:
        request_time = datetime.fromisoformat(timestamp)
    except ValueError:
        return GateResult(
            decision="BLOCK",
            reason_code="AGENT_SIG_INVALID",
            detail=f"timestamp {timestamp!r} is not a valid ISO 8601 datetime.",
            remedy="Send an ISO 8601 UTC timestamp.",
            rule_id="R01",
        )

    skew_seconds = abs((now - request_time).total_seconds())
    if skew_seconds > AGENT_CLOCK_SKEW_TOLERANCE_S:
        return GateResult(
            decision="BLOCK",
            reason_code="CLOCK_SKEW_EXCEEDED",
            detail=f"request timestamp is {skew_seconds:.1f}s from server time "
            f"(tolerance: {AGENT_CLOCK_SKEW_TOLERANCE_S}s).",
            remedy="Resynchronise the client clock and retry with a fresh timestamp.",
            rule_id="R01",
        )

    nonce_key = f"nonce:{agent_did}:{nonce}"
    is_new_nonce = await redis.set(nonce_key, "1", nx=True, ex=NONCE_TTL_S)
    if not is_new_nonce:
        return GateResult(
            decision="BLOCK",
            reason_code="NONCE_REPLAYED",
            detail=f"nonce {nonce!r} has already been used by agent_did {agent_did!r}.",
            remedy="Generate a fresh nonce for each request.",
            rule_id="R01",
        )

    message = _signing_message(method, body, timestamp, nonce)
    if not verify(record.public_key, message, signature):
        return GateResult(
            decision="BLOCK",
            reason_code="AGENT_SIG_INVALID",
            detail="Ed25519 signature over (method, sha256(body), timestamp, nonce) did not verify.",
            remedy="Sign with the private key matching the registered agent public key.",
            rule_id="R01",
        )

    return allow(detail=f"agent {agent_did!r} authenticated", rule_id="R01")
