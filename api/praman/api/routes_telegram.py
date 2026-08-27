"""Telegram Bot API inbound webhook — `POST /tg/webhook`.

Added after live phone testing found a real, account-level Twilio
restriction with no code-level workaround (this project's Twilio account
can't fetch `Message`/`Media` resources at all on the Trial tier — see
ARCHITECTURE.md's "Post-Phase-7" section). Telegram has no equivalent
approval gate. Verifies the `X-Telegram-Bot-Api-Secret-Token` header
(Telegram's actual inbound-verification mechanism — there's no per-request
HMAC signature the way Twilio has), then routes through the exact same
`whatsapp/dispatch.py` logic the Twilio webhook uses.

Same prompt-injection note as `routes_whatsapp.py` applies identically:
whatever a vendor sends here only ever reaches `ingest/extract.py`'s LLM
call or the deterministic state machine's plain-text matching, never
`core/gate.py`/`core/envelope.py`/`core/reversibility.py`.
"""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from praman.adapters.llm import get_llm_client
from praman.adapters.razorpay_client import get_razorpay_client
from praman.config import get_settings
from praman.core.registry import LocalRegistry
from praman.db import SessionLocal
from praman.redis_client import get_redis
from praman.whatsapp.dispatch import dispatch_inbound_message
from praman.whatsapp.telegram_client import get_telegram_client

router = APIRouter(tags=["telegram"])
logger = logging.getLogger(__name__)


def _chat_id_str(chat_id: int) -> str:
    # Same "channel:identifier" convention as Twilio's "whatsapp:+91..." —
    # keeps the two channels' merchants unambiguous in the same column.
    return f"telegram:{chat_id}"


@router.post("/tg/webhook")
async def telegram_webhook(request: Request) -> Response:
    settings = get_settings()

    if not settings.telegram_use_fake:
        received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(received_secret, settings.telegram_webhook_secret):
            raise HTTPException(status_code=403, detail="invalid Telegram webhook secret")

    update: dict[str, Any] = await request.json()
    message = update.get("message")
    if not message:
        # Telegram sends other update types (edited_message, callback_query,
        # ...) this bot doesn't use — 200 so Telegram doesn't retry them.
        return Response(status_code=200)

    chat_id = _chat_id_str(message["chat"]["id"])
    body = message.get("text", "") or message.get("caption", "")
    photos = message.get("photo") or []

    telegram = get_telegram_client(settings.telegram_bot_token, use_fake=settings.telegram_use_fake)

    media: list[tuple[bytes, str]] = []
    if photos:
        # Telegram sends each photo as several resolutions, smallest first —
        # the last entry is the largest/highest quality.
        largest = photos[-1]
        try:
            content_bytes, content_type = await telegram.fetch_media(largest["file_id"])
        except Exception:
            # Same resilience as routes_whatsapp.py's media-fetch handling —
            # one undownloadable photo must not crash the whole webhook.
            logger.warning(
                "tg/webhook: failed to fetch media %s from %s",
                largest["file_id"],
                chat_id,
                exc_info=True,
            )
        else:
            media.append((content_bytes, content_type))

    llm = get_llm_client(settings)
    razorpay = get_razorpay_client(
        settings.razorpay_key_id,
        settings.razorpay_key_secret,
        settings.razorpay_webhook_secret,
        use_fake=settings.razorpay_use_fake,
    )
    async with SessionLocal() as session:
        await dispatch_inbound_message(
            session,
            get_redis(),
            LocalRegistry(session),
            razorpay,
            telegram,
            llm,
            from_id=chat_id,
            body=body,
            media=media,
            demo_mode=settings.demo_mode,
            now=datetime.now(UTC),
        )

    return Response(status_code=200)
