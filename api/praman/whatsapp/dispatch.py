"""Shared inbound-message dispatch — merchant approval reply, then buyer
cooling-off cancel, then onboarding — used identically by both channel
webhooks (`api/routes_whatsapp.py` for Twilio, `api/routes_telegram.py`
for Telegram). Factored out so the two channels' routing can't drift: a
channel-specific route's only job is to normalize that channel's payload
into `(from_id, body, media)` and hand it here.
"""

from __future__ import annotations

from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.adapters.llm import LLMClient
from praman.adapters.razorpay_client import RazorpayClient
from praman.core.registry import AgentRegistry
from praman.models import Merchant
from praman.whatsapp.approvals import handle_merchant_reply
from praman.whatsapp.client import WhatsAppClient
from praman.whatsapp.cooling_off_notify import handle_buyer_reply
from praman.whatsapp.onboarding import handle_inbound_whatsapp

# `WhatsAppClient` is the shared shape (`send_text`/`verify_webhook_signature`/
# `fetch_media`) both `whatsapp/client.py` (Twilio) and
# `whatsapp/telegram_client.py` (Telegram) implement — see the latter's
# module docstring for why a Telegram client structurally satisfies a
# Protocol named for the other channel.


async def dispatch_inbound_message(
    session: AsyncSession,
    redis: Redis,
    registry: AgentRegistry,
    razorpay: RazorpayClient,
    messaging: WhatsAppClient,
    llm: LLMClient,
    *,
    from_id: str,
    body: str,
    media: list[tuple[bytes, str]],
    demo_mode: bool,
    now: datetime,
) -> None:
    merchant_result = await session.execute(
        select(Merchant).where(Merchant.whatsapp_number == from_id)
    )
    merchant = merchant_result.scalar_one_or_none()

    if merchant is not None and merchant.onboarding_state == "LIVE":
        handled = await handle_merchant_reply(
            session,
            redis,
            registry,
            razorpay,
            messaging,
            llm,
            merchant,
            body,
            demo_mode=demo_mode,
            now=now,
        )
        if handled:
            return

    if merchant is None:
        handled = await handle_buyer_reply(session, razorpay, messaging, from_id, body, now=now)
        if handled:
            return

    await handle_inbound_whatsapp(
        session, messaging, llm, from_number=from_id, body=body, media=media
    )
