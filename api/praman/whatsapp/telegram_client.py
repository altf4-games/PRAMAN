"""Telegram adapter — added after live phone testing found a real, account-
level Twilio restriction with no code-level workaround: this project's
Twilio account, on the Trial tier, can't fetch `Message`/`Media` REST
resources at all (`401 code 20003`), on top of the already-documented
outbound-`ContentSid` restriction (see ARCHITECTURE.md's "Post-Phase-7"
section). Telegram's Bot API has no equivalent approval gate — a bot
token from @BotFather can send freeform replies and download media
immediately, with no business verification step.

`RealTelegramClient`/`FakeTelegramClient` implement the same three-method
shape `whatsapp/client.py::WhatsAppClient` does (`send_text`,
`verify_webhook_signature`, `fetch_media`) so `api/routes_telegram.py` can
pass either into the exact same onboarding/approvals/cooling-off business
logic Twilio already uses — none of that logic is channel-aware, or needed
to change. Two shape notes specific to this channel:

- `send_text`'s first argument is a Telegram `chat_id` (as a string), not
  a phone number — callers already treat it as an opaque "address" string,
  so this is transparent to them.
- `fetch_media`'s argument is a Telegram `file_id`, not a URL — Telegram
  requires an extra `getFile` call to resolve a `file_id` into a
  downloadable path first. The parameter is still named generically at
  the call site for Protocol-shape compatibility; only this module needs
  to know it's actually a `file_id`.
- `verify_webhook_signature` is a stub (see its docstring) — Telegram's
  real inbound verification is a constant-time secret-token header
  comparison, done directly in `routes_telegram.py`, not through this
  Protocol method (which exists to match Twilio's HMAC-over-params shape
  and has no Telegram equivalent).
"""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass

_TELEGRAM_API_BASE = "https://api.telegram.org"


class RealTelegramClient:
    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token

    def _url(self, method: str) -> str:
        return f"{_TELEGRAM_API_BASE}/bot{self._bot_token}/{method}"

    async def send_text(self, chat_id: str, body: str) -> str:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._url("sendMessage"), json={"chat_id": chat_id, "text": body}, timeout=15
            )
        resp.raise_for_status()
        data = resp.json()
        return str(data["result"]["message_id"])

    def verify_webhook_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        """Not used — see the module docstring. `routes_telegram.py` checks
        the `X-Telegram-Bot-Api-Secret-Token` header directly instead, since
        Telegram has no per-request HMAC signature the way Twilio does."""
        raise NotImplementedError(
            "Telegram webhook verification is a secret-token header comparison, "
            "done in routes_telegram.py — not through this method."
        )

    async def fetch_media(self, file_id: str) -> tuple[bytes, str]:
        import httpx

        async with httpx.AsyncClient() as client:
            file_resp = await client.get(
                self._url("getFile"), params={"file_id": file_id}, timeout=15
            )
            file_resp.raise_for_status()
            file_path = file_resp.json()["result"]["file_path"]

            content_resp = await client.get(
                f"{_TELEGRAM_API_BASE}/file/bot{self._bot_token}/{file_path}", timeout=30
            )
            content_resp.raise_for_status()

        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        return content_resp.content, mime_type


@dataclass
class SentTelegramMessage:
    chat_id: str
    body: str
    message_id: str


class FakeTelegramClient:
    """Deterministic in-memory stand-in for tests and offline dev — same
    role as `whatsapp/client.py::FakeWhatsAppClient`."""

    def __init__(self) -> None:
        self.sent_messages: list[SentTelegramMessage] = []
        self._media_store: dict[str, tuple[bytes, str]] = {}

    async def send_text(self, chat_id: str, body: str) -> str:
        message_id = str(uuid.uuid4().int % 10**9)
        self.sent_messages.append(
            SentTelegramMessage(chat_id=chat_id, body=body, message_id=message_id)
        )
        return message_id

    def verify_webhook_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        raise NotImplementedError("Telegram webhook verification happens in routes_telegram.py")

    def register_media(self, file_id: str, content: bytes, mime_type: str) -> None:
        self._media_store[file_id] = (content, mime_type)

    async def fetch_media(self, file_id: str) -> tuple[bytes, str]:
        if file_id not in self._media_store:
            raise KeyError(f"FakeTelegramClient: no media registered for {file_id}")
        return self._media_store[file_id]


def get_telegram_client(
    bot_token: str, *, use_fake: bool = False
) -> RealTelegramClient | FakeTelegramClient:
    if use_fake or not bot_token:
        return FakeTelegramClient()
    return RealTelegramClient(bot_token)
