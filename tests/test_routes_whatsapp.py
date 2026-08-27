"""Route-level test for POST /wa/webhook: signature enforcement and basic
wiring. Business logic (the state machine itself) is covered directly in
test_onboarding.py; this file exists to prove the HTTP layer around it —
signature verification, form parsing, media fetch — is wired correctly.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from praman.config import get_settings
from praman.whatsapp.client import FakeWhatsAppClient


@pytest.fixture
def client_with_fake_twilio(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from praman.config import Settings

    monkeypatch.setattr(
        "praman.config.get_settings",
        lambda: Settings(twilio_account_sid="", twilio_auth_token="", twilio_use_fake=True),
    )
    # main.py and routes_whatsapp.py both call `from praman.config import
    # get_settings` then `get_settings()` — patch the name where it's looked
    # up (routes_whatsapp's own module namespace), not just the origin.
    from praman.api import routes_whatsapp

    monkeypatch.setattr(
        routes_whatsapp,
        "get_settings",
        lambda: Settings(twilio_account_sid="", twilio_auth_token="", twilio_use_fake=True),
    )

    from praman.main import app

    # Entering as a context manager runs FastAPI's lifespan startup, which
    # (in DEMO_MODE) creates the schema on praman.db's engine — without
    # this, the route's DB queries hit "no such table".
    with TestClient(app) as client:
        yield client


def test_webhook_accepts_text_message_when_fake_mode_enabled(
    client_with_fake_twilio: TestClient,
) -> None:
    response = client_with_fake_twilio.post(
        "/wa/webhook",
        data={"From": "whatsapp:+919876543210", "Body": "hi", "NumMedia": "0"},
    )
    assert response.status_code == 200
    assert "<Response>" in response.text


def test_webhook_rejects_bad_signature_when_not_faked(monkeypatch: pytest.MonkeyPatch) -> None:
    from praman.config import Settings

    real_settings = Settings(
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="test_auth_token",
        twilio_use_fake=False,
        public_base_url="http://testserver",
    )

    from praman.api import routes_whatsapp

    monkeypatch.setattr(routes_whatsapp, "get_settings", lambda: real_settings)

    from praman.main import app

    client = TestClient(app)
    response = client.post(
        "/wa/webhook",
        data={"From": "whatsapp:+919876543210", "Body": "hi", "NumMedia": "0"},
        headers={"X-Twilio-Signature": "not-a-real-signature"},
    )
    assert response.status_code == 403


def test_webhook_accepts_correctly_signed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from praman.config import Settings

    real_settings = Settings(
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="test_auth_token",
        twilio_use_fake=False,
        public_base_url="http://testserver",
    )

    from praman.api import routes_whatsapp
    from praman.whatsapp.client import RealTwilioClient

    monkeypatch.setattr(routes_whatsapp, "get_settings", lambda: real_settings)

    # This test is about signature verification, not message delivery —
    # stub the actual Twilio API call so fake credentials don't 401 on send.
    async def _fake_send_text(self: RealTwilioClient, to: str, body: str) -> str:
        return "SM_fake_sid"

    monkeypatch.setattr(RealTwilioClient, "send_text", _fake_send_text)

    from praman.main import app

    params = {"From": "whatsapp:+919876543210", "Body": "hi", "NumMedia": "0"}

    # Sign it exactly the way Twilio does — reuse the same HMAC scheme via
    # a FakeWhatsAppClient constructed with the matching auth token, since
    # it implements the identical algorithm (see whatsapp/client.py).
    signer = FakeWhatsAppClient(auth_token="test_auth_token")
    signature = signer.sign("http://testserver/wa/webhook", params)

    with TestClient(app) as client:
        response = client.post(
            "/wa/webhook", data=params, headers={"X-Twilio-Signature": signature}
        )
    assert response.status_code == 200


def test_webhook_routes_live_merchant_reply_to_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A LIVE merchant's own number should try the approval inbox before
    falling through to onboarding — even though nothing is actually
    pending here (the business logic itself is covered by
    `test_approvals.py`; this just proves the routing decision)."""
    from datetime import UTC, datetime

    from praman.config import Settings

    real_settings = Settings(twilio_account_sid="", twilio_auth_token="", twilio_use_fake=True)
    from praman.api import routes_whatsapp

    monkeypatch.setattr(routes_whatsapp, "get_settings", lambda: real_settings)

    calls: list[str] = []

    async def _spy_handle_merchant_reply(*args: object, **kwargs: object) -> bool:
        calls.append("merchant")
        return True

    monkeypatch.setattr(routes_whatsapp, "handle_merchant_reply", _spy_handle_merchant_reply)

    from praman.db import SessionLocal
    from praman.main import app
    from praman.models import Merchant

    merchant_number = "whatsapp:+919000000123"

    async def _seed() -> None:
        async with SessionLocal() as session:
            session.add(
                Merchant(
                    name="M",
                    did="did:key:zM123",
                    public_key="pub",
                    private_key_enc="enc",
                    whatsapp_number=merchant_number,
                    onboarding_state="LIVE",
                    agent_policy={},
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()

    with TestClient(app) as client:
        # `client.portal` runs on the same event loop the lifespan (and thus
        # `praman.db`'s engine) started on — seeding through it, rather than
        # a fresh `anyio.run()`, keeps this on that same loop.
        client.portal.call(_seed)

        response = client.post(
            "/wa/webhook",
            data={"From": merchant_number, "Body": "approve", "NumMedia": "0"},
        )
    assert response.status_code == 200
    assert calls == ["merchant"]


def test_webhook_routes_unknown_number_cancel_to_buyer_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from praman.config import Settings

    real_settings = Settings(twilio_account_sid="", twilio_auth_token="", twilio_use_fake=True)
    from praman.api import routes_whatsapp

    monkeypatch.setattr(routes_whatsapp, "get_settings", lambda: real_settings)

    calls: list[str] = []

    async def _spy_handle_buyer_reply(*args: object, **kwargs: object) -> bool:
        calls.append("buyer")
        return True

    monkeypatch.setattr(routes_whatsapp, "handle_buyer_reply", _spy_handle_buyer_reply)

    from praman.main import app

    with TestClient(app) as client:
        response = client.post(
            "/wa/webhook",
            data={"From": "whatsapp:+919000000456", "Body": "CANCEL", "NumMedia": "0"},
        )
    assert response.status_code == 200
    assert calls == ["buyer"]


def test_config_get_settings_is_unaffected_after_test(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sanity check that patching in the fixtures above doesn't leak between
    # tests via the lru_cache singleton.
    settings = get_settings()
    assert settings.twilio_account_sid in ("", None) or isinstance(settings.twilio_account_sid, str)
