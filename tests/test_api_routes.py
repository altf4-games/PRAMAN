"""Integration tests for the REST surface built in Phase 6, part 2 —
exercises the full `catalog_search -> agents/register -> envelopes ->
quotes -> cart/confirm -> checkout/execute` flow end to end over real HTTP
(via an ASGI transport, no live server), plus the Razorpay webhook and the
`.well-known` discovery document. Uses the real signature scheme
(`Ed25519` over `method\\nsha256(body)\\ntimestamp\\nnonce`) end to end —
these tests build the exact bytes an agent would sign, not a shortcut.
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


async def _seed_merchant_and_product(session: AsyncSession) -> tuple[Merchant, Product]:
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

    product = Product(
        merchant_id=merchant.id,
        sku="toor-dal-1kg",
        name="Toor Dal 1kg",
        category="pulses",
        category_class="consumable",
        unit_price_paise=18000,
        stock=50,
        return_window_days=14,
        fulfilment_hours=12,
        restocking_cost_pct=0.0,
        is_personalised=False,
        field_confidence={},
        needs_review=False,
        source="manual",
    )
    session.add(product)
    await session.commit()
    await session.refresh(product)
    return merchant, product


async def test_well_known_manifest(client: AsyncClient) -> None:
    resp = await client.get("/.well-known/agent-commerce.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol"] == "praman-agent-commerce"
    assert "checkout_execute" in body["tools"]["destructive"]


async def test_catalog_search_hides_needs_review_products(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    merchant, _product = await _seed_merchant_and_product(db_session)
    hidden = Product(
        merchant_id=merchant.id,
        sku="mystery",
        name="Mystery Item",
        category="pulses",
        category_class="consumable",
        unit_price_paise=1000,
        stock=5,
        return_window_days=2,
        fulfilment_hours=12,
        restocking_cost_pct=0.0,
        is_personalised=False,
        field_confidence={},
        needs_review=True,
        source="manual",
    )
    db_session.add(hidden)
    await db_session.commit()

    resp = await client.get("/api/catalog/search", params={"merchant_id": merchant.id})
    assert resp.status_code == 200
    skus = {p["sku"] for p in resp.json()}
    assert skus == {"toor-dal-1kg"}

    resp = await client.get(f"/api/catalog/{hidden.id}")
    assert resp.status_code == 404


async def test_policy_get_reflects_onboarding_policy(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    merchant, _product = await _seed_merchant_and_product(db_session)
    resp = await client.get(f"/api/policy/{merchant.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_txn_paise"] == 500_000
    assert body["cooling_off_hold"] is True


async def test_agent_register_generates_demo_keypair(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/agents/register",
        json={
            "operator": "Test Operator",
            "trust_tier": "standard",
            "max_txn_paise": 1_000_000,
            "daily_cap_paise": 5_000_000,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_did"].startswith("did:key:z")
    assert body["private_key"] is not None


async def _register_agent(client: AsyncClient) -> tuple[str, str]:
    resp = await client.post(
        "/api/agents/register",
        json={
            "operator": "Test Operator",
            "max_txn_paise": 1_000_000,
            "daily_cap_paise": 5_000_000,
        },
    )
    body = resp.json()
    return body["agent_did"], body["private_key"]


async def test_full_green_checkout_flow(client: AsyncClient, db_session: AsyncSession) -> None:
    merchant, product = await _seed_merchant_and_product(db_session)
    agent_did, agent_priv = await _register_agent(client)

    env_resp = await client.post(
        "/api/envelopes",
        json={
            "merchant_id": merchant.id,
            "agent_did": agent_did,
            "user_ref": "buyer-1",
            "user_whatsapp": "whatsapp:+919000000099",
            "ceiling_paise": 1_000_000,
            "max_single_txn_paise": 1_000_000,
            "allowed_categories": ["pulses"],
            "min_reversibility": 0.0,
            "valid_hours": 24,
        },
    )
    assert env_resp.status_code == 200
    envelope_id = env_resp.json()["envelope_id"]

    def _sign(payload: dict[str, object]) -> tuple[bytes, dict[str, str]]:
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

    quote_raw, quote_headers = _sign({"product_id": product.id, "agent_did": agent_did, "qty": 1})
    quote_resp = await client.post("/api/quotes", content=quote_raw, headers=quote_headers)
    assert quote_resp.status_code == 200, quote_resp.text
    quote = quote_resp.json()

    cart_raw, cart_headers = _sign(
        {"envelope_id": envelope_id, "agent_did": agent_did, "quotes": [quote]}
    )
    cart_resp = await client.post("/api/cart/confirm", content=cart_raw, headers=cart_headers)
    assert cart_resp.status_code == 200, cart_resp.text
    cart_out = cart_resp.json()
    assert cart_out["band"] == "green"
    assert cart_out["envelope_check_decision"] == "ALLOW"

    checkout_raw, checkout_headers = _sign(
        {"cart_id": cart_out["cart_id"], "agent_did": agent_did, "quotes": [quote]}
    )
    checkout_resp = await client.post(
        "/api/checkout/execute", content=checkout_raw, headers=checkout_headers
    )
    assert checkout_resp.status_code == 200, checkout_resp.text
    result = checkout_resp.json()
    assert result["decision"] == "ALLOW"
    assert result["order_status"] == "captured"

    order_resp = await client.get(f"/api/orders/{result['order_id']}")
    assert order_resp.status_code == 200
    assert order_resp.json()["status"] == "captured"


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


async def test_amber_checkout_holds_then_buyer_undo_cancels(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    priv, pub = generate_keypair()
    merchant = Merchant(
        name="Sharma Jewellers",
        did=did_module.did_from_public_key(pub),
        public_key=pub,
        private_key_enc=encrypt_private_key(priv, get_settings().app_secret),
        whatsapp_number=f"whatsapp:+91{uuid.uuid4().int % 10**10:010d}",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=datetime.now(UTC),
    )
    db_session.add(merchant)
    await db_session.commit()
    await db_session.refresh(merchant)

    product = Product(
        merchant_id=merchant.id,
        sku="silver-chain",
        name="Silver Chain",
        category="jewellery",
        category_class="durable",
        unit_price_paise=640_000,
        stock=10,
        return_window_days=10,
        fulfilment_hours=48,
        restocking_cost_pct=0.05,
        is_personalised=False,
        field_confidence={},
        needs_review=False,
        source="manual",
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    agent_did, agent_priv = await _register_agent(client)

    env_resp = await client.post(
        "/api/envelopes",
        json={
            "merchant_id": merchant.id,
            "agent_did": agent_did,
            "user_ref": "buyer-amber",
            "user_whatsapp": "whatsapp:+919000000098",
            "ceiling_paise": 2_000_000,
            "max_single_txn_paise": 2_000_000,
            "allowed_categories": ["jewellery"],
            "min_reversibility": 0.0,
            "valid_hours": 24,
        },
    )
    envelope_id = env_resp.json()["envelope_id"]

    quote_raw, quote_headers = _sign_for(
        agent_priv, {"product_id": product.id, "agent_did": agent_did, "qty": 1}
    )
    quote = (await client.post("/api/quotes", content=quote_raw, headers=quote_headers)).json()

    cart_raw, cart_headers = _sign_for(
        agent_priv, {"envelope_id": envelope_id, "agent_did": agent_did, "quotes": [quote]}
    )
    cart_out = (
        await client.post("/api/cart/confirm", content=cart_raw, headers=cart_headers)
    ).json()
    assert cart_out["band"] == "amber"

    checkout_raw, checkout_headers = _sign_for(
        agent_priv, {"cart_id": cart_out["cart_id"], "agent_did": agent_did, "quotes": [quote]}
    )
    result = (
        await client.post("/api/checkout/execute", content=checkout_raw, headers=checkout_headers)
    ).json()
    assert result["decision"] == "HOLD"
    order_id = result["order_id"]

    order = (await client.get(f"/api/orders/{order_id}")).json()
    assert order["cooling_off_until"] is not None
    assert order["dispatched_at"] is None

    undo_resp = await client.post(f"/api/orders/{order_id}/undo", json={"user_ref": "buyer-amber"})
    assert undo_resp.status_code == 200
    assert undo_resp.json()["cancelled"] is True

    order = (await client.get(f"/api/orders/{order_id}")).json()
    assert order["cancelled_at"] is not None


async def test_order_undo_rejects_mismatched_user_ref(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    merchant, product = await _seed_merchant_and_product(db_session)
    agent_did, agent_priv = await _register_agent(client)
    env_resp = await client.post(
        "/api/envelopes",
        json={
            "merchant_id": merchant.id,
            "agent_did": agent_did,
            "user_ref": "real-buyer",
            "user_whatsapp": "whatsapp:+919000000097",
            "ceiling_paise": 1_000_000,
            "max_single_txn_paise": 1_000_000,
            "allowed_categories": ["pulses"],
            "min_reversibility": 0.0,
            "valid_hours": 24,
        },
    )
    envelope_id = env_resp.json()["envelope_id"]

    quote_raw, quote_headers = _sign_for(
        agent_priv, {"product_id": product.id, "agent_did": agent_did, "qty": 1}
    )
    quote = (await client.post("/api/quotes", content=quote_raw, headers=quote_headers)).json()
    cart_raw, cart_headers = _sign_for(
        agent_priv, {"envelope_id": envelope_id, "agent_did": agent_did, "quotes": [quote]}
    )
    cart_out = (
        await client.post("/api/cart/confirm", content=cart_raw, headers=cart_headers)
    ).json()
    checkout_raw, checkout_headers = _sign_for(
        agent_priv, {"cart_id": cart_out["cart_id"], "agent_did": agent_did, "quotes": [quote]}
    )
    result = (
        await client.post("/api/checkout/execute", content=checkout_raw, headers=checkout_headers)
    ).json()

    resp = await client.post(
        f"/api/orders/{result['order_id']}/undo", json={"user_ref": "impostor"}
    )
    assert resp.status_code == 403


async def test_quote_request_rejects_bad_signature(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _merchant, product = await _seed_merchant_and_product(db_session)
    agent_did, _agent_priv = await _register_agent(client)
    _wrong_priv, _wrong_pub = generate_keypair()

    timestamp = datetime.now(UTC).isoformat()
    nonce = uuid.uuid4().hex
    payload = {"product_id": product.id, "agent_did": agent_did, "qty": 1}
    raw = json.dumps(payload).encode()
    signature = _sign_request(_wrong_priv, "POST", raw, timestamp, nonce)

    resp = await client.post(
        "/api/quotes",
        content=raw,
        headers={
            "content-type": "application/json",
            "X-Praman-Timestamp": timestamp,
            "X-Praman-Nonce": nonce,
            "X-Praman-Signature": signature,
        },
    )
    assert resp.status_code == 403


async def test_razorpay_webhook_rejects_bad_signature(client: AsyncClient) -> None:
    resp = await client.post(
        "/webhooks/razorpay",
        content=b'{"event": "payment.captured"}',
        headers={
            "X-Razorpay-Signature": "not-a-real-signature",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 403


async def test_razorpay_webhook_accepts_valid_signature(
    client: AsyncClient, razorpay: FakeRazorpayClient
) -> None:
    body = b'{"event": "payment.captured", "payload": {"payment": {"entity": {"order_id": "order_1"}}}}'
    signature = razorpay.sign(body)
    resp = await client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"X-Razorpay-Signature": signature, "content-type": "application/json"},
    )
    assert resp.status_code == 200
