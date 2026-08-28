"""Shared FastAPI dependencies. Every route gets its DB session, Redis
client, registry, and adapters through these — never constructs them
inline — so tests can override exactly these functions
(`app.dependency_overrides`) to swap in `fakeredis`/`FakeRazorpayClient`/
`FakeWhatsAppClient`/`FakeLLMClient` without touching route code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from praman.adapters.llm import LLMClient, get_llm_client
from praman.adapters.razorpay_client import RazorpayClient, get_razorpay_client
from praman.config import Settings, get_settings
from praman.core.registry import AgentRegistry, LocalRegistry
from praman.db import SessionLocal
from praman.redis_client import get_redis
from praman.whatsapp.client import MultiChannelClient, WhatsAppClient, get_whatsapp_client
from praman.whatsapp.telegram_client import get_telegram_client


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def get_redis_dep() -> Redis:
    return get_redis()


def get_registry_dep(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AgentRegistry:
    return LocalRegistry(session)


def get_razorpay_dep(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RazorpayClient:
    return get_razorpay_client(
        settings.razorpay_key_id,
        settings.razorpay_key_secret,
        settings.razorpay_webhook_secret,
        use_fake=settings.razorpay_use_fake,
    )


def get_whatsapp_dep(
    settings: Annotated[Settings, Depends(get_settings)],
) -> WhatsAppClient:
    """Routes by recipient address (`telegram:` vs `whatsapp:`) rather than
    returning a single fixed provider — see `MultiChannelClient`'s own
    docstring for the real bug this fixes (cooling-off/approval messages
    to a Telegram-onboarded merchant were always attempted over Twilio)."""
    whatsapp_client = get_whatsapp_client(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.twilio_whatsapp_from,
        use_fake=settings.twilio_use_fake,
    )
    telegram_client = get_telegram_client(
        settings.telegram_bot_token, use_fake=settings.telegram_use_fake
    )
    return MultiChannelClient(whatsapp_client, telegram_client)


def get_llm_dep(settings: Annotated[Settings, Depends(get_settings)]) -> LLMClient:
    return get_llm_client(settings)


@dataclass(frozen=True, slots=True)
class SignatureHeaders:
    """`X-Praman-Timestamp` / `X-Praman-Nonce` / `X-Praman-Signature` — the
    agent auth fields for a signed request, carried as headers rather than
    body fields (see `schemas.py`'s module docstring for why). Any signed
    route depends on `get_signature_headers` instead of parsing these
    itself."""

    timestamp: str
    nonce: str
    signature: str


def get_signature_headers(request: Request) -> SignatureHeaders:
    timestamp = request.headers.get("X-Praman-Timestamp")
    nonce = request.headers.get("X-Praman-Nonce")
    signature = request.headers.get("X-Praman-Signature")
    if not timestamp or not nonce or not signature:
        raise HTTPException(
            status_code=401,
            detail="X-Praman-Timestamp, X-Praman-Nonce, and X-Praman-Signature headers are required",
        )
    return SignatureHeaders(timestamp=timestamp, nonce=nonce, signature=signature)


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
SignatureHeadersDep = Annotated[SignatureHeaders, Depends(get_signature_headers)]
RedisDep = Annotated[Redis, Depends(get_redis_dep)]
RegistryDep = Annotated[AgentRegistry, Depends(get_registry_dep)]
RazorpayDep = Annotated[RazorpayClient, Depends(get_razorpay_dep)]
WhatsAppDep = Annotated[WhatsAppClient, Depends(get_whatsapp_dep)]
LLMDep = Annotated[LLMClient, Depends(get_llm_dep)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
