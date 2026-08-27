"""Route-level tests for POST /tg/webhook — secret-token enforcement, JSON
parsing, media-fetch resilience, and routing into the shared dispatch
logic. Mirrors test_routes_whatsapp.py's structure for the Twilio route.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_fake_telegram(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from praman.config import Settings

    monkeypatch.setattr(
        "praman.config.get_settings",
        lambda: Settings(telegram_bot_token="", telegram_use_fake=True),
    )
    from praman.api import routes_telegram

    monkeypatch.setattr(
        routes_telegram,
        "get_settings",
        lambda: Settings(telegram_bot_token="", telegram_use_fake=True),
    )

    from praman.main import app

    with TestClient(app) as client:
        yield client


def test_webhook_accepts_text_message_when_fake_mode_enabled(
    client_with_fake_telegram: TestClient,
) -> None:
    response = client_with_fake_telegram.post(
        "/tg/webhook",
        json={
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 12345, "type": "private"},
                "text": "hi",
            },
        },
    )
    assert response.status_code == 200


def test_webhook_ignores_non_message_updates(client_with_fake_telegram: TestClient) -> None:
    response = client_with_fake_telegram.post(
        "/tg/webhook", json={"update_id": 2, "edited_message": {"text": "edited"}}
    )
    assert response.status_code == 200


def test_webhook_rejects_bad_secret_when_not_faked(monkeypatch: pytest.MonkeyPatch) -> None:
    from praman.config import Settings

    real_settings = Settings(
        telegram_bot_token="fake-token",
        telegram_webhook_secret="the-real-secret",
        telegram_use_fake=False,
    )
    from praman.api import routes_telegram

    monkeypatch.setattr(routes_telegram, "get_settings", lambda: real_settings)

    from praman.main import app

    client = TestClient(app)
    response = client.post(
        "/tg/webhook",
        json={"update_id": 3, "message": {"chat": {"id": 1}, "text": "hi"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert response.status_code == 403


def test_webhook_accepts_correct_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from praman.config import Settings

    real_settings = Settings(
        telegram_bot_token="fake-token",
        telegram_webhook_secret="the-real-secret",
        telegram_use_fake=False,
    )
    from praman.api import routes_telegram

    monkeypatch.setattr(routes_telegram, "get_settings", lambda: real_settings)

    from praman.whatsapp.telegram_client import FakeTelegramClient

    monkeypatch.setattr(
        routes_telegram, "get_telegram_client", lambda *a, **k: FakeTelegramClient()
    )

    from praman.main import app

    with TestClient(app) as client:
        response = client.post(
            "/tg/webhook",
            json={"update_id": 4, "message": {"chat": {"id": 1}, "text": "hi"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": "the-real-secret"},
        )
    assert response.status_code == 200


def test_webhook_survives_a_photo_fetch_failure(client_with_fake_telegram: TestClient) -> None:
    """Mirrors test_routes_whatsapp.py's equivalent — a photo whose file_id
    was never registered with FakeTelegramClient raises the same way a real
    fetch failure would; the webhook must not crash."""
    response = client_with_fake_telegram.post(
        "/tg/webhook",
        json={
            "update_id": 5,
            "message": {
                "chat": {"id": 99},
                "photo": [{"file_id": "unregistered-file-id", "file_size": 100}],
            },
        },
    )
    assert response.status_code == 200


def test_webhook_routes_live_merchant_reply_to_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from praman.config import Settings

    real_settings = Settings(telegram_bot_token="", telegram_use_fake=True)
    from praman.api import routes_telegram

    monkeypatch.setattr(routes_telegram, "get_settings", lambda: real_settings)

    calls: list[str] = []

    async def _spy_dispatch(*args: object, **kwargs: object) -> None:
        calls.append("dispatch")

    # routes_telegram.py imports dispatch_inbound_message by name (`from
    # praman.whatsapp.dispatch import dispatch_inbound_message`), so patch
    # it where it's looked up — routes_telegram's own module namespace.
    monkeypatch.setattr(routes_telegram, "dispatch_inbound_message", _spy_dispatch)

    from praman.main import app

    with TestClient(app) as client:
        response = client.post(
            "/tg/webhook",
            json={"update_id": 6, "message": {"chat": {"id": 555}, "text": "hello"}},
        )
    assert response.status_code == 200
    assert calls == ["dispatch"]
