"""Twilio WhatsApp Sandbox inbound webhook — `POST /wa/webhook`.

Verifies the Twilio signature, persists the message, and routes it. A
message from a number matching a *merchant* first tries the merchant
approval inbox (`whatsapp/approvals.py`) and otherwise falls through to
onboarding; any other number tries the buyer cooling-off cancel handler
(`whatsapp/cooling_off_notify.py`) and otherwise gets a generic reply.
Prompt-injection note (CLAUDE.md §8): whatever a vendor sends here —
including any text embedded in a photographed price list — only ever
reaches `ingest/extract.py`'s LLM call or the deterministic state machine's
plain-text matching. It never reaches `core/gate.py`, `core/envelope.py`,
or `core/reversibility.py`, which don't import this module or anything
downstream of it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from starlette.responses import Response

from praman.adapters.llm import get_llm_client
from praman.adapters.razorpay_client import get_razorpay_client
from praman.config import get_settings
from praman.core.registry import LocalRegistry
from praman.db import SessionLocal
from praman.models import Merchant
from praman.redis_client import get_redis
from praman.whatsapp.approvals import handle_merchant_reply
from praman.whatsapp.client import get_whatsapp_client
from praman.whatsapp.cooling_off_notify import handle_buyer_reply
from praman.whatsapp.onboarding import handle_inbound_whatsapp

router = APIRouter(tags=["whatsapp"])
logger = logging.getLogger(__name__)


def _webhook_url(request: Request) -> str:
    settings = get_settings()
    base = settings.public_base_url.rstrip("/")
    return f"{base}{request.url.path}"


@router.post("/wa/webhook")
async def whatsapp_webhook(request: Request) -> Response:
    settings = get_settings()
    form = await request.form()
    params = {key: str(value) for key, value in form.multi_items()}

    whatsapp = get_whatsapp_client(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.twilio_whatsapp_from,
        use_fake=settings.twilio_use_fake,
    )

    signature = request.headers.get("X-Twilio-Signature", "")
    if not settings.twilio_use_fake and not whatsapp.verify_webhook_signature(
        _webhook_url(request), params, signature
    ):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")

    from_number = params.get("From", "")
    body = params.get("Body", "")
    num_media = int(params.get("NumMedia", "0") or "0")

    media: list[tuple[bytes, str]] = []
    for i in range(num_media):
        media_url = params.get(f"MediaUrl{i}")
        if not media_url:
            continue
        try:
            content_bytes, content_type = await whatsapp.fetch_media(media_url)
        except Exception:
            # A media *download* failure (e.g. a Twilio trial account that
            # can't fetch Message/Media resources via the REST API at all —
            # a real, confirmed account-tier restriction, not a transient
            # blip) must not crash the whole webhook. Same "one bad photo
            # shouldn't sink the batch" principle already applied to a bad
            # *extraction* in onboarding.py — this is the same failure
            # mode one step earlier, and deserves the same resilience.
            logger.warning(
                "wa/webhook: failed to fetch media %s from %s",
                media_url,
                from_number,
                exc_info=True,
            )
            continue
        media.append((content_bytes, content_type))

    llm = get_llm_client(settings)
    async with SessionLocal() as session:
        merchant_result = await session.execute(
            select(Merchant).where(Merchant.whatsapp_number == from_number)
        )
        merchant = merchant_result.scalar_one_or_none()

        if merchant is not None and merchant.onboarding_state == "LIVE":
            razorpay = get_razorpay_client(
                settings.razorpay_key_id,
                settings.razorpay_key_secret,
                settings.razorpay_webhook_secret,
                use_fake=settings.razorpay_use_fake,
            )
            registry = LocalRegistry(session)
            handled = await handle_merchant_reply(
                session,
                get_redis(),
                registry,
                razorpay,
                whatsapp,
                llm,
                merchant,
                body,
                demo_mode=settings.demo_mode,
                now=datetime.now(UTC),
            )
            if handled:
                return Response(content="<Response></Response>", media_type="application/xml")

        if merchant is None:
            razorpay = get_razorpay_client(
                settings.razorpay_key_id,
                settings.razorpay_key_secret,
                settings.razorpay_webhook_secret,
                use_fake=settings.razorpay_use_fake,
            )
            handled = await handle_buyer_reply(
                session, razorpay, whatsapp, from_number, body, now=datetime.now(UTC)
            )
            if handled:
                return Response(content="<Response></Response>", media_type="application/xml")

        await handle_inbound_whatsapp(
            session, whatsapp, llm, from_number=from_number, body=body, media=media
        )

    return Response(content="<Response></Response>", media_type="application/xml")
