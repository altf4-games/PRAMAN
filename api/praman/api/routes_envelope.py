"""`envelope_submit` (CLAUDE.md §6). An Intent Envelope is countersigned by
the *merchant* — the storefront issuing the one-time consent a buyer's agent
will operate within (UPI Reserve Pay semantics: one-time consent, a
ceiling, instant revocability) — not by the agent itself, so this route
takes no agent signature. `agent_did` must already be a registered agent
(`POST /api/agents/register`) and `merchant_id` a live merchant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from praman.api.deps import DbSession
from praman.config import get_settings
from praman.core.ledger import append_event
from praman.crypto.canonical import canonicalize
from praman.crypto.keys import decrypt_private_key, sign
from praman.models import Agent, IntentEnvelope, Merchant
from praman.schemas import EnvelopeOut, EnvelopeSubmitIn

router = APIRouter(prefix="/api", tags=["envelope"])


def _signing_payload(env: IntentEnvelope) -> dict[str, object]:
    return {
        "envelope_id": env.envelope_id,
        "merchant_id": env.merchant_id,
        "agent_did": env.agent_did,
        "user_ref": env.user_ref,
        "ceiling_paise": env.ceiling_paise,
        "max_single_txn_paise": env.max_single_txn_paise,
        "allowed_categories": env.allowed_categories,
        "min_reversibility": env.min_reversibility,
        "valid_from": env.valid_from.isoformat(),
        "valid_until": env.valid_until.isoformat(),
    }


@router.post("/envelopes")
async def envelope_submit(session: DbSession, body: EnvelopeSubmitIn) -> EnvelopeOut:
    merchant_result = await session.execute(select(Merchant).where(Merchant.id == body.merchant_id))
    merchant = merchant_result.scalar_one_or_none()
    if merchant is None:
        raise HTTPException(status_code=404, detail="merchant not found")

    agent_result = await session.execute(select(Agent).where(Agent.agent_did == body.agent_did))
    if agent_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="agent not registered")

    now = datetime.now(UTC)
    env = IntentEnvelope(
        user_ref=body.user_ref,
        user_whatsapp=body.user_whatsapp,
        merchant_id=body.merchant_id,
        agent_did=body.agent_did,
        ceiling_paise=body.ceiling_paise,
        spent_paise=0,
        max_single_txn_paise=body.max_single_txn_paise,
        allowed_categories=body.allowed_categories,
        min_reversibility=body.min_reversibility,
        valid_from=now,
        valid_until=now + timedelta(hours=body.valid_hours),
        revoked_at=None,
        signature="",
    )
    session.add(env)
    await session.flush()  # assigns env.envelope_id (default=_uuid) without committing yet

    private_key_hex = decrypt_private_key(merchant.private_key_enc, get_settings().app_secret)
    env.signature = sign(private_key_hex, canonicalize(_signing_payload(env)))
    await session.commit()
    await session.refresh(env)

    await append_event(
        session,
        f"envelope:{env.envelope_id}",
        body.agent_did,
        "ENVELOPE_ISSUED",
        {
            "envelope_id": env.envelope_id,
            "merchant_id": body.merchant_id,
            "agent_did": body.agent_did,
        },
    )

    return EnvelopeOut(
        envelope_id=env.envelope_id,
        merchant_id=env.merchant_id,
        agent_did=env.agent_did,
        ceiling_paise=env.ceiling_paise,
        spent_paise=env.spent_paise,
        max_single_txn_paise=env.max_single_txn_paise,
        allowed_categories=env.allowed_categories,
        min_reversibility=env.min_reversibility,
        valid_from=env.valid_from.isoformat(),
        valid_until=env.valid_until.isoformat(),
        signature=env.signature,
    )
