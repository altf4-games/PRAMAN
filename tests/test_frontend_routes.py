"""Integration tests for the Phase 7 frontend-plumbing routes: merchant
listing, the catalog review queue, the REST approvals inbox (mirroring the
WhatsApp one), the dispute-pack preview, and the metrics summary. Reuses
the same real-signature ASGI-transport pattern as test_api_routes.py.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from praman.adapters.llm import FakeLLMClient
from praman.adapters.razorpay_client import FakeRazorpayClient
from praman.api.deps import (
    get_db_session,
    get_llm_dep,
    get_razorpay_dep,
    get_redis_dep,
    get_whatsapp_dep,
)
from praman.config import get_settings
from praman.crypto import did as did_module
from praman.crypto.keys import encrypt_private_key, generate_keypair, sign
from praman.main import app
from praman.models import Merchant, Product
from praman.whatsapp.client import FakeWhatsAppClient
from sqlalchemy.ext.asyncio import AsyncSession


def _sign_request(
    private_key_hex: str, method: str, body: bytes, timestamp: str, nonce: str
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()
    return sign(private_key_hex, message)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def razorpay() -> FakeRazorpayClient:
    return FakeRazorpayClient()


@pytest.fixture
def whatsapp() -> FakeWhatsAppClient:
    return FakeWhatsAppClient()


@pytest.fixture
async def client(
    db_session: AsyncSession,
    redis: fakeredis.aioredis.FakeRedis,
    razorpay: FakeRazorpayClient,
    whatsapp: FakeWhatsAppClient,
) -> AsyncClient:
    async def _get_db_session() -> AsyncSession:
        return db_session

    app.dependency_overrides[get_db_session] = _get_db_session
    app.dependency_overrides[get_redis_dep] = lambda: redis
    app.dependency_overrides[get_razorpay_dep] = lambda: razorpay
    app.dependency_overrides[get_whatsapp_dep] = lambda: whatsapp
    app.dependency_overrides[get_llm_dep] = lambda: FakeLLMClient()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_merchant(session: AsyncSession) -> Merchant:
    priv, pub = generate_keypair()
    merchant = Merchant(
        name="Sharma Jewellers",
        did=did_module.did_from_public_key(pub),
        public_key=pub,
        private_key_enc=encrypt_private_key(priv, get_settings().app_secret),
        whatsapp_number=f"whatsapp:+91{uuid.uuid4().int % 10**10:010d}",
        onboarding_state="LIVE",
        agent_policy={"max_txn_paise": 500_000, "cooling_off_hold": True},
        created_at=datetime.now(UTC),
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)
    return merchant


async def _seed_product(session: AsyncSession, merchant: Merchant, **overrides: object) -> Product:
    defaults: dict[str, object] = {
        "merchant_id": merchant.id,
        "sku": "toor-dal-1kg",
        "name": "Toor Dal 1kg",
        "category": "pulses",
        "category_class": "consumable",
        "unit_price_paise": 18000,
        "stock": 50,
        "return_window_days": 14,
        "fulfilment_hours": 12,
        "restocking_cost_pct": 0.0,
        "is_personalised": False,
        "field_confidence": {},
        "needs_review": False,
        "source": "manual",
    }
    defaults.update(overrides)
    product = Product(**defaults)  # type: ignore[arg-type]
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return product


def _sign_for(agent_priv: str, payload: dict[str, object]) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload).encode()
    timestamp = datetime.now(UTC).isoformat()
    nonce = uuid.uuid4().hex
    signature = _sign_request(agent_priv, "POST", raw, timestamp, nonce)
    headers = {
        "content-type": "application/json",
        "X-Praman-Timestamp": timestamp,
        "X-Praman-Nonce": nonce,
        "X-Praman-Signature": signature,
    }
    return raw, headers


async def _run_checkout(
    client: AsyncClient,
    merchant: Merchant,
    product: Product,
    *,
    ceiling: int,
    min_reversibility: float,
) -> dict[str, object]:
    reg = await client.post(
        "/api/agents/register",
        json={"operator": "test", "max_txn_paise": ceiling, "daily_cap_paise": ceiling * 10},
    )
    agent_did = reg.json()["agent_did"]
    agent_priv = reg.json()["private_key"]

    env = await client.post(
        "/api/envelopes",
        json={
            "merchant_id": merchant.id,
            "agent_did": agent_did,
            "user_ref": "buyer-1",
            "user_whatsapp": "whatsapp:+919000000099",
            "ceiling_paise": ceiling,
            "max_single_txn_paise": ceiling,
            "allowed_categories": [product.category],
            "min_reversibility": min_reversibility,
            "valid_hours": 24,
        },
    )
    envelope_id = env.json()["envelope_id"]

    quote_raw, quote_headers = _sign_for(
        agent_priv, {"product_id": product.id, "agent_did": agent_did, "qty": 1}
    )
    quote = (await client.post("/api/quotes", content=quote_raw, headers=quote_headers)).json()

    cart_raw, cart_headers = _sign_for(
        agent_priv, {"envelope_id": envelope_id, "agent_did": agent_did, "quotes": [quote]}
    )
    cart = (await client.post("/api/cart/confirm", content=cart_raw, headers=cart_headers)).json()

    checkout_raw, checkout_headers = _sign_for(
        agent_priv, {"cart_id": cart["cart_id"], "agent_did": agent_did, "quotes": [quote]}
    )
    result = (
        await client.post("/api/checkout/execute", content=checkout_raw, headers=checkout_headers)
    ).json()
    result["cart_id"] = cart["cart_id"]
    return result


async def test_list_and_get_merchant(client: AsyncClient, db_session: AsyncSession) -> None:
    merchant = await _seed_merchant(db_session)
    listed = (await client.get("/api/merchants")).json()
    assert any(m["id"] == merchant.id for m in listed)

    got = await client.get(f"/api/merchants/{merchant.id}")
    assert got.status_code == 200
    assert got.json()["name"] == "Sharma Jewellers"

    missing = await client.get("/api/merchants/does-not-exist")
    assert missing.status_code == 404


async def test_review_queue_only_lists_needs_review(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    merchant = await _seed_merchant(db_session)
    await _seed_product(db_session, merchant, sku="clear-item", needs_review=False)
    await _seed_product(db_session, merchant, sku="fuzzy-item", needs_review=True)

    resp = await client.get("/api/catalog/review-queue", params={"merchant_id": merchant.id})
    assert resp.status_code == 200
    skus = {p["sku"] for p in resp.json()}
    assert skus == {"fuzzy-item"}


async def test_approvals_inbox_and_decide_approve(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    merchant = await _seed_merchant(db_session)
    product = await _seed_product(
        db_session,
        merchant,
        sku="engraved-ring",
        category="rings",
        category_class="bespoke",
        unit_price_paise=420_000,
        return_window_days=0,
        is_personalised=True,
    )

    result = await _run_checkout(
        client, merchant, product, ceiling=1_000_000, min_reversibility=0.5
    )
    assert result["decision"] == "ESCALATE", result

    inbox = (await client.get("/api/approvals", params={"merchant_id": merchant.id})).json()
    assert len(inbox) == 1
    assert inbox[0]["order_id"] == result["order_id"]
    assert inbox[0]["reason_code"] == "STEP_UP_REQUIRED"
    assert inbox[0]["band"] == "red"

    decide = await client.post(
        f"/api/approvals/{result['order_id']}/decide", json={"decision": "approve"}
    )
    assert decide.status_code == 200
    assert decide.json()["order_status"] == "captured"

    inbox_after = (await client.get("/api/approvals", params={"merchant_id": merchant.id})).json()
    assert inbox_after == []


async def test_approvals_decide_rejects_bad_decision(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    merchant = await _seed_merchant(db_session)
    resp = await client.post(f"/api/approvals/{merchant.id}/decide", json={"decision": "maybe"})
    assert resp.status_code == 400


async def test_dispute_pack_has_full_structure(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    merchant = await _seed_merchant(db_session)
    product = await _seed_product(db_session, merchant)
    result = await _run_checkout(
        client, merchant, product, ceiling=1_000_000, min_reversibility=0.0
    )
    assert result["decision"] == "ALLOW"

    pack = await client.get(f"/api/dispute-pack/{result['cart_id']}")
    assert pack.status_code == 200
    body = pack.json()
    assert body["cart_id"] == result["cart_id"]
    assert body["envelope"]["agent_did"]
    assert body["order"]["status"] == "captured"
    assert len(body["gate_trail"]) >= 1
    assert body["ledger"]["chain_verified"] is True
    assert len(body["ledger"]["events"]) >= 1


async def test_dispute_pack_404_for_unknown_cart(client: AsyncClient) -> None:
    resp = await client.get("/api/dispute-pack/does-not-exist")
    assert resp.status_code == 404


async def test_metrics_reflect_seeded_orders(client: AsyncClient, db_session: AsyncSession) -> None:
    merchant = await _seed_merchant(db_session)
    product = await _seed_product(db_session, merchant)
    result = await _run_checkout(
        client, merchant, product, ceiling=1_000_000, min_reversibility=0.0
    )
    assert result["decision"] == "ALLOW"

    metrics = (await client.get("/api/metrics")).json()
    assert metrics["sessions_gated"] >= 1
    assert metrics["orders_by_status"].get("captured", 0) >= 1
    assert metrics["orders_by_band"].get("green", 0) >= 1
    assert metrics["disputes_resolvable"] >= 1
