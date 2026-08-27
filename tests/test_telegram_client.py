"""Unit tests for the Telegram adapter — mirrors test_whatsapp_client.py's
structure for FakeTelegramClient/factory selection, plus a regression test
for a real bug found via live testing (see ARCHITECTURE.md): a bot reply
sent the whole internal "telegram:<id>" identifier to Telegram's API
instead of the bare numeric id, so every outbound reply failed with
"400 chat not found" even to a chat that had genuinely messaged the bot.
"""

from __future__ import annotations

import httpx
import pytest
from praman.whatsapp.telegram_client import (
    FakeTelegramClient,
    RealTelegramClient,
    get_telegram_client,
)


async def test_send_text_records_the_message() -> None:
    client = FakeTelegramClient()
    message_id = await client.send_text("telegram:12345", "hello")
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0].chat_id == "telegram:12345"
    assert client.sent_messages[0].body == "hello"
    assert client.sent_messages[0].message_id == message_id


async def test_fetch_media_roundtrip() -> None:
    client = FakeTelegramClient()
    client.register_media("file123", b"image-bytes", "image/jpeg")
    content, content_type = await client.fetch_media("file123")
    assert content == b"image-bytes"
    assert content_type == "image/jpeg"


async def test_fetch_media_raises_for_unregistered_file_id() -> None:
    client = FakeTelegramClient()
    with pytest.raises(KeyError):
        await client.fetch_media("unknown-file-id")


def test_verify_webhook_signature_is_a_documented_stub() -> None:
    client = FakeTelegramClient()
    with pytest.raises(NotImplementedError):
        client.verify_webhook_signature("https://example.com", {}, "sig")


def test_get_telegram_client_returns_fake_when_no_token() -> None:
    assert isinstance(get_telegram_client(""), FakeTelegramClient)


def test_get_telegram_client_returns_fake_when_use_fake_forced() -> None:
    assert isinstance(get_telegram_client("real-token", use_fake=True), FakeTelegramClient)


def test_get_telegram_client_returns_real_with_a_token() -> None:
    assert isinstance(get_telegram_client("real-token"), RealTelegramClient)


async def test_real_client_strips_the_channel_prefix_from_chat_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the real bug: `Merchant.whatsapp_number` stores
    Telegram chat ids as "telegram:<numeric id>" (the same "channel:id"
    convention Twilio's "whatsapp:+91..." uses) — `send_text` must strip
    that prefix before calling Telegram's API, which only understands the
    bare numeric id."""
    captured: dict[str, object] = {}

    class _FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"result": {"message_id": 42}}

    async def _fake_post(
        self: httpx.AsyncClient, url: str, json: dict[str, object], timeout: float
    ) -> _FakeResponse:
        captured["chat_id"] = json["chat_id"]
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    client = RealTelegramClient("fake-token")
    message_id = await client.send_text("telegram:8980338920", "hi")

    assert captured["chat_id"] == "8980338920"
    assert message_id == "42"
