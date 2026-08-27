"""Twilio WhatsApp Sandbox inbound webhook — `POST /wa/webhook`.

Verifies the Twilio signature, persists the message, and routes it into the
onboarding state machine. Prompt-injection note (CLAUDE.md §8): whatever a
vendor sends here — including any text embedded in a photographed price
list — only ever reaches `ingest/extract.py`'s LLM call or the deterministic
state machine's plain-text matching. It never reaches `core/gate.py`,
`core/envelope.py`, or `core/reversibility.py`, which don't import this
module or anything downstream of it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from praman.adapters.llm import get_llm_client
from praman.config import get_settings
from praman.db import SessionLocal
from praman.whatsapp.client import get_whatsapp_client
from praman.whatsapp.onboarding import handle_inbound_whatsapp

router = APIRouter(tags=["whatsapp"])


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
        content_bytes, content_type = await whatsapp.fetch_media(media_url)
        media.append((content_bytes, content_type))

    llm = get_llm_client(settings)
    async with SessionLocal() as session:
        await handle_inbound_whatsapp(
            session, whatsapp, llm, from_number=from_number, body=body, media=media
        )

    return Response(content="<Response></Response>", media_type="application/xml")
