"""Regression test for a real bug: the MCP tool wrappers used to
re-serialize a signed tool's body via httpx's own JSON encoder (`json=`)
before sending it, rather than transmitting the exact bytes a caller
signed over. Any genuine external MCP caller — not this build's own
frontend or `agent_runner`, both of which bypass this layer and send
their own pre-signed raw bytes directly to the REST routes — would sign
over their own JSON serialization, get a different wire body from this
layer's re-encoding, and always see `AGENT_SIG_INVALID`.

Caught by connecting to the real deployed `/mcp` endpoint as a genuine
third-party MCP client and attempting a real signed purchase. Reproduced
and fixed here (`mcp/server.py::_signed_post` now transmits the RFC 8785
canonical bytes) so it can't silently return.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import fakeredis.aioredis
import httpx
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
from praman.crypto.canonical import canonicalize
from praman.crypto.keys import encrypt_private_key, generate_keypair, sign, verify
from praman.main import app
from praman.mcp import server as mcp_server
from praman.models import Agent, Merchant, Product
from praman.whatsapp.client import FakeWhatsAppClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _route_mcp_through_asgi(monkeypatch: pytest.MonkeyPatch) -> None:
    def _asgi_client(base_url: str) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    monkeypatch.setattr(mcp_server, "_new_client", _asgi_client)


@pytest.fixture
async def wired_app(db_session: AsyncSession) -> tuple[Merchant, Product]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _get_db_session() -> AsyncSession:
        return db_session

    app.dependency_overrides[get_db_session] = _get_db_session
    app.dependency_overrides[get_redis_dep] = lambda: redis
    app.dependency_overrides[get_razorpay_dep] = lambda: FakeRazorpayClient()
    app.dependency_overrides[get_whatsapp_dep] = lambda: FakeWhatsAppClient()
    app.dependency_overrides[get_llm_dep] = lambda: FakeLLMClient()

    priv, pub = generate_keypair()
    merchant = Merchant(
        name="MCP Test Merchant",
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
        sku="mcp-test-sku",
        name="MCP Test Product",
        category="pulses",
        category_class="consumable",
        unit_price_paise=10000,
        stock=10,
        return_window_days=14,
        fulfilment_hours=12,
        restocking_cost_pct=0.0,
        is_personalised=False,
        field_confidence={},
        needs_review=False,
        source="manual",
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    yield merchant, product
    app.dependency_overrides.clear()


async def _register_external_agent(db_session: AsyncSession, agent_did: str, pub: str) -> None:
    """A genuine external caller registering itself the same way any REST
    client would -- this MCP surface deliberately has no register_agent
    tool of its own (agents self-register via the REST route directly, a
    step that needs no signature since no identity exists yet)."""
    db_session.add(
        Agent(
            agent_did=agent_did,
            operator="external-mcp-test-caller",
            public_key=pub,
            trust_tier="standard",
            max_txn_paise=1_000_000,
            daily_cap_paise=10_000_000,
            registered_at=datetime.now(UTC),
            revoked_at=None,
        )
    )
    await db_session.commit()


def _sign_canonical(
    private_key_hex: str, method: str, body: dict[str, object]
) -> tuple[str, str, str]:
    """Exactly what a genuine external MCP caller (any language) is meant
    to do per `mcp/server.py`'s own docstring: canonicalize the body
    (RFC 8785) and sign sha256 of *that*, not of some arbitrary JSON
    serialization."""
    body_sha = hashlib.sha256(canonicalize(body)).hexdigest()
    timestamp = datetime.now(UTC).isoformat()
    nonce = uuid.uuid4().hex
    message = f"{method}\n{body_sha}\n{timestamp}\n{nonce}".encode()
    return timestamp, nonce, sign(private_key_hex, message)


async def test_signed_mcp_tool_call_succeeds_for_a_genuine_external_caller(
    db_session: AsyncSession, wired_app: tuple[Merchant, Product]
) -> None:
    """The actual regression test: an external caller who only has access
    to the public MCP tool functions (never this app's internals) signs
    over the RFC 8785 canonical form of their own body and must succeed —
    exactly the contract this module's docstring promises."""
    merchant, product = wired_app
    priv, pub = generate_keypair()
    agent_did = did_module.did_from_public_key(pub)
    await _register_external_agent(db_session, agent_did, pub)

    envelope = await mcp_server.envelope_submit(
        merchant_id=merchant.id,
        agent_did=agent_did,
        user_ref="mcp-external-test",
        user_whatsapp="whatsapp:+919000000042",
        ceiling_paise=1_000_000,
        max_single_txn_paise=1_000_000,
        allowed_categories=["pulses"],
    )
    assert "envelope_id" in envelope

    qbody = {"product_id": product.id, "agent_did": agent_did, "qty": 1}
    ts, nonce, sig = _sign_canonical(priv, "POST", qbody)
    quote = await mcp_server.quote_request(
        product_id=product.id, agent_did=agent_did, qty=1, timestamp=ts, nonce=nonce, signature=sig
    )
    assert "quote_id" in quote, quote

    cbody = {"envelope_id": envelope["envelope_id"], "agent_did": agent_did, "quotes": [quote]}
    ts, nonce, sig = _sign_canonical(priv, "POST", cbody)
    cart = await mcp_server.cart_confirm(
        envelope_id=envelope["envelope_id"],
        agent_did=agent_did,
        quotes=[quote],
        timestamp=ts,
        nonce=nonce,
        signature=sig,
    )
    assert "cart_id" in cart, cart

    xbody = {"cart_id": cart["cart_id"], "agent_did": agent_did, "quotes": [quote]}
    ts, nonce, sig = _sign_canonical(priv, "POST", xbody)
    checkout = await mcp_server.checkout_execute(
        cart_id=cart["cart_id"],
        agent_did=agent_did,
        quotes=[quote],
        timestamp=ts,
        nonce=nonce,
        signature=sig,
    )
    assert checkout["decision"] in ("ALLOW", "HOLD", "ESCALATE", "SUBSTITUTE", "BLOCK")


async def test_signed_mcp_tool_call_fails_if_signed_over_non_canonical_json(
    db_session: AsyncSession, wired_app: tuple[Merchant, Product]
) -> None:
    """The negative case, proving the fix is specifically about
    canonicalization rather than "any signature now works": signing over
    plain `json.dumps`'s default output (insertion-order, spaced
    separators) instead of the RFC 8785 canonical form must still fail."""
    _merchant, product = wired_app
    priv, pub = generate_keypair()
    agent_did = did_module.did_from_public_key(pub)
    await _register_external_agent(db_session, agent_did, pub)

    body = {"product_id": product.id, "agent_did": agent_did, "qty": 1}
    non_canonical = json.dumps(body).encode()  # default json.dumps, NOT RFC 8785
    body_sha = hashlib.sha256(non_canonical).hexdigest()
    timestamp = datetime.now(UTC).isoformat()
    nonce = uuid.uuid4().hex
    message = f"POST\n{body_sha}\n{timestamp}\n{nonce}".encode()
    signature = sign(priv, message)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await mcp_server.quote_request(
            product_id=product.id,
            agent_did=agent_did,
            qty=1,
            timestamp=timestamp,
            nonce=nonce,
            signature=signature,
        )
    assert exc_info.value.response.status_code == 403


def test_canonicalize_ignores_key_order() -> None:
    """The property the whole fix depends on: two independently-built
    dicts with the same keys/values canonicalize identically regardless
    of insertion order — verified directly, not just implied."""
    assert canonicalize({"b": 2, "a": 1}) == canonicalize({"a": 1, "b": 2})


def test_sign_canonical_helper_round_trips_against_verify() -> None:
    """Sanity check for this test file's own `_sign_canonical` helper,
    independent of the MCP layer."""
    priv, pub = generate_keypair()
    body = {"z": 1, "a": 2}
    ts, nonce, sig = _sign_canonical(priv, "POST", body)
    expected_hash = hashlib.sha256(canonicalize(body)).hexdigest()
    message = f"POST\n{expected_hash}\n{ts}\n{nonce}".encode()
    assert verify(pub, message, sig)
