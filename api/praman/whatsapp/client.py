"""WhatsApp adapter, over Twilio's WhatsApp Sandbox. `WhatsAppClient` is a
Protocol — same pattern as `RazorpayClient` and `LLMClient` — so onboarding
logic never imports the `twilio` SDK directly.

Sandbox limitation (disclosed in README): the Twilio WhatsApp Sandbox does
not support native interactive buttons ([Yes] [No] as tappable UI) the way
an approved WhatsApp Business sender does — those need pre-approved content
templates. Every "button" in the spec's onboarding script is sent as plain
text with a clear reply instruction (e.g. "Reply YES or reply with the
correct price") instead. This is a real constraint of the free sandbox, not
a shortcut taken for convenience.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Protocol


class WhatsAppClient(Protocol):
    async def send_text(self, to: str, body: str) -> str:
        """Sends a WhatsApp text message. Returns the provider message id."""
        ...

    def verify_webhook_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        """Verifies an inbound webhook actually came from the provider."""
        ...

    async def fetch_media(self, media_url: str) -> tuple[bytes, str]:
        """Downloads an inbound media attachment. Returns (bytes, content_type)."""
        ...


class RealTwilioClient:
    def __init__(self, account_sid: str, auth_token: str, whatsapp_from: str) -> None:
        from twilio.rest import Client

        self._account_sid = account_sid
        self._auth_token = auth_token
        self._whatsapp_from = whatsapp_from
        self._client = Client(account_sid, auth_token)

    async def send_text(self, to: str, body: str) -> str:
        # The twilio SDK is sync; onboarding.py awaits this in an async
        # context but message volume here is low (one bot reply at a time)
        # so a blocking call is an acceptable simplification for this scope.
        message = self._client.messages.create(from_=self._whatsapp_from, to=to, body=body)
        return str(message.sid)

    def verify_webhook_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(self._auth_token)
        return bool(validator.validate(url, params, signature))

    async def fetch_media(self, media_url: str) -> tuple[bytes, str]:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                media_url, auth=(self._account_sid, self._auth_token), timeout=30
            )
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


@dataclass
class SentMessage:
    to: str
    body: str
    sid: str


class FakeWhatsAppClient:
    """Deterministic in-memory stand-in for tests and offline dev. Uses the
    same HMAC scheme Twilio actually uses (HMAC-SHA1 over url + sorted
    concatenated params, base64), so `verify_webhook_signature` exercises
    real verification logic against a client that never calls the network.
    """

    def __init__(self, auth_token: str = "fake_twilio_auth_token") -> None:
        self._auth_token = auth_token
        self.sent_messages: list[SentMessage] = []
        self._media_store: dict[str, tuple[bytes, str]] = {}

    async def send_text(self, to: str, body: str) -> str:
        sid = f"SM{uuid.uuid4().hex[:32]}"
        self.sent_messages.append(SentMessage(to=to, body=body, sid=sid))
        return sid

    def sign(self, url: str, params: dict[str, str]) -> str:
        """Test helper: produce a valid signature for (url, params), the
        same way Twilio's own webhook sender would."""
        import base64

        data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        digest = hmac.new(self._auth_token.encode(), data.encode(), hashlib.sha1).digest()
        return base64.b64encode(digest).decode()

    def verify_webhook_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        return hmac.compare_digest(self.sign(url, params), signature)

    def register_media(self, media_url: str, content: bytes, content_type: str) -> None:
        self._media_store[media_url] = (content, content_type)

    async def fetch_media(self, media_url: str) -> tuple[bytes, str]:
        if media_url not in self._media_store:
            raise KeyError(f"FakeWhatsAppClient: no media registered for {media_url}")
        return self._media_store[media_url]


def get_whatsapp_client(
    account_sid: str, auth_token: str, whatsapp_from: str, *, use_fake: bool = False
) -> WhatsAppClient:
    if use_fake or not account_sid:
        return FakeWhatsAppClient(auth_token=auth_token or "fake_twilio_auth_token")
    return RealTwilioClient(account_sid, auth_token, whatsapp_from)


class MultiChannelClient:
    """Dispatches `send_text` (and `fetch_media`) to Telegram or
    WhatsApp/Twilio based on the recipient address's own `telegram:` /
    `whatsapp:` prefix — the same convention `Merchant.whatsapp_number`
    and `IntentEnvelope.user_whatsapp` already store either kind of
    identifier under.

    A real bug found before ever being exercised end to end: onboarding
    replies work correctly over Telegram because `routes_telegram.py`'s
    inbound webhook constructs its own `RealTelegramClient` and passes it
    straight into the shared onboarding/checkout logic for that one
    request. But the buyer's cooling-off notification and the merchant's
    approval request are sent from `core/checkout.py`, triggered by
    `checkout_execute` (a REST call, not an inbound Telegram webhook) —
    that code path only ever had `WhatsAppDep`, which constructed a
    Twilio client unconditionally. A Telegram-onboarded merchant's
    cooling-off/approval messages were therefore always attempted over
    Twilio, which has no idea what to do with a `telegram:` address. This
    class is the fix: one client, injected everywhere a
    `WhatsAppClient` is needed, that picks the right underlying provider
    per message instead of per request.
    """

    def __init__(self, whatsapp_client: WhatsAppClient, telegram_client: WhatsAppClient) -> None:
        self._whatsapp = whatsapp_client
        self._telegram = telegram_client

    def _for(self, address: str) -> WhatsAppClient:
        return self._telegram if address.startswith("telegram:") else self._whatsapp

    async def send_text(self, to: str, body: str) -> str:
        return await self._for(to).send_text(to, body)

    def verify_webhook_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        # Only ever invoked by the WhatsApp/Twilio inbound webhook route,
        # which already holds and uses its own Twilio client directly —
        # never actually routed through this multiplexer in practice, but
        # implemented for Protocol conformance (Telegram's own inbound
        # verification is a header comparison done in routes_telegram.py,
        # not through this method — see telegram_client.py's docstring).
        return self._whatsapp.verify_webhook_signature(url, params, signature)

    async def fetch_media(self, media_url: str) -> tuple[bytes, str]:
        # Same reasoning as verify_webhook_signature above: only ever
        # called from within an inbound webhook handler, which already
        # has its own channel-specific client.
        return await self._whatsapp.fetch_media(media_url)
