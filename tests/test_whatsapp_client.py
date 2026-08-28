from __future__ import annotations

import pytest
from praman.whatsapp.client import FakeWhatsAppClient, get_whatsapp_client


async def test_send_text_records_the_message() -> None:
    client = FakeWhatsAppClient()
    sid = await client.send_text("whatsapp:+911234567890", "hello")
    assert len(client.sent_messages) == 1
    assert client.sent_messages[0].to == "whatsapp:+911234567890"
    assert client.sent_messages[0].body == "hello"
    assert client.sent_messages[0].sid == sid


def test_signature_roundtrip() -> None:
    client = FakeWhatsAppClient(auth_token="secret")
    url = "https://example.com/wa/webhook"
    params = {"From": "whatsapp:+911234567890", "Body": "hi"}
    signature = client.sign(url, params)
    assert client.verify_webhook_signature(url, params, signature)


def test_signature_rejects_tampered_params() -> None:
    client = FakeWhatsAppClient(auth_token="secret")
    url = "https://example.com/wa/webhook"
    params = {"From": "whatsapp:+911234567890", "Body": "hi"}
    signature = client.sign(url, params)
    tampered = {**params, "Body": "tampered"}
    assert not client.verify_webhook_signature(url, tampered, signature)


def test_signature_rejects_wrong_auth_token() -> None:
    signer = FakeWhatsAppClient(auth_token="secret")
    verifier = FakeWhatsAppClient(auth_token="different-secret")
    url = "https://example.com/wa/webhook"
    params = {"From": "whatsapp:+911234567890"}
    signature = signer.sign(url, params)
    assert not verifier.verify_webhook_signature(url, params, signature)


async def test_fetch_media_roundtrip() -> None:
    client = FakeWhatsAppClient()
    client.register_media("https://example.com/media/1", b"image-bytes", "image/png")
    content, content_type = await client.fetch_media("https://example.com/media/1")
    assert content == b"image-bytes"
    assert content_type == "image/png"


async def test_fetch_media_raises_for_unregistered_url() -> None:
    client = FakeWhatsAppClient()
    with pytest.raises(KeyError):
        await client.fetch_media("https://example.com/media/unknown")


def test_get_whatsapp_client_returns_fake_when_no_account_sid() -> None:
    client = get_whatsapp_client("", "", "whatsapp:+14155238886")
    assert isinstance(client, FakeWhatsAppClient)


def test_get_whatsapp_client_returns_fake_when_use_fake_forced() -> None:
    client = get_whatsapp_client(
        "AC_real_sid", "real_token", "whatsapp:+14155238886", use_fake=True
    )
    assert isinstance(client, FakeWhatsAppClient)


def test_get_whatsapp_client_returns_real_with_credentials() -> None:
    client = get_whatsapp_client("AC_real_sid", "real_token", "whatsapp:+14155238886")
    assert type(client).__name__ == "RealTwilioClient"


# --- MultiChannelClient: routes by recipient address prefix ---


async def test_multi_channel_client_routes_telegram_addresses_to_telegram() -> None:
    from praman.whatsapp.client import MultiChannelClient
    from praman.whatsapp.telegram_client import FakeTelegramClient

    whatsapp = FakeWhatsAppClient()
    telegram = FakeTelegramClient()
    client = MultiChannelClient(whatsapp, telegram)

    await client.send_text("telegram:8980338920", "cooling-off notice")

    assert len(telegram.sent_messages) == 1
    assert telegram.sent_messages[0].chat_id == "telegram:8980338920"
    assert telegram.sent_messages[0].body == "cooling-off notice"
    assert whatsapp.sent_messages == []


async def test_multi_channel_client_routes_whatsapp_addresses_to_whatsapp() -> None:
    from praman.whatsapp.client import MultiChannelClient
    from praman.whatsapp.telegram_client import FakeTelegramClient

    whatsapp = FakeWhatsAppClient()
    telegram = FakeTelegramClient()
    client = MultiChannelClient(whatsapp, telegram)

    await client.send_text("whatsapp:+919000000001", "merchant approval request")

    assert len(whatsapp.sent_messages) == 1
    assert whatsapp.sent_messages[0].to == "whatsapp:+919000000001"
    assert telegram.sent_messages == []
