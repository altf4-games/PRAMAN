from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from praman.adapters.llm import FakeLLMClient
from praman.adapters.razorpay_client import FakeRazorpayClient
from praman.core.checkout import execute_checkout
from praman.core.envelope import Cart, CartItem
from praman.core.gate import GateRequest
from praman.core.quotes import QuoteData, issue_quote
from praman.core.registry import LocalRegistry
from praman.crypto.keys import generate_keypair, sign
from praman.models import Agent, CartMandate, IntentEnvelope, LedgerEvent, Merchant, Product
from praman.whatsapp.client import FakeWhatsAppClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@dataclass
class Scenario:
    merchant: Merchant
    merchant_priv: str
    agent: Agent
    agent_priv: str
    product: Product
    envelope: IntentEnvelope
    quote: QuoteData
    cart: CartMandate


def _sign_request(
    private_key_hex: str, method: str, body: bytes, timestamp: str, nonce: str
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}\n{body_hash}\n{timestamp}\n{nonce}".encode()
    return sign(private_key_hex, message)


async def _build_scenario(
    session: AsyncSession,
    redis: fakeredis.aioredis.FakeRedis,
    *,
    product_unit_price_paise: int = 1000,
    product_category_class: str = "consumable",
    envelope_min_reversibility: float = 0.0,
    agent_max_txn_paise: int = 1_000_000,
    agent_daily_cap_paise: int = 10_000_000,
) -> Scenario:
    merchant_priv, merchant_pub = generate_keypair()
    merchant = Merchant(
        name="Test Merchant",
        did="did:key:zMerchantTest",
        public_key=merchant_pub,
        private_key_enc="unused-in-tests",
        whatsapp_number=f"whatsapp:+91{uuid.uuid4().int % 10**10:010d}",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=NOW,
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)

    agent_priv, agent_pub = generate_keypair()
    agent = Agent(
        agent_did=f"did:key:zAgent{uuid.uuid4().hex[:8]}",
        operator="Test Operator",
        public_key=agent_pub,
        trust_tier="standard",
        max_txn_paise=agent_max_txn_paise,
        daily_cap_paise=agent_daily_cap_paise,
        registered_at=NOW - timedelta(days=1),
        revoked_at=None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)

    product = Product(
        merchant_id=merchant.id,
        sku="test-sku",
        name="Test Product",
        category="pulses",
        category_class=product_category_class,
        unit_price_paise=product_unit_price_paise,
        stock=100,
        return_window_days=14,
        fulfilment_hours=24,
        restocking_cost_pct=0.0,
        is_personalised=False,
        field_confidence={},
        needs_review=False,
        source="manual",
    )
    session.add(product)
    await session.commit()
    await session.refresh(product)

    envelope = IntentEnvelope(
        user_ref="user-1",
        user_whatsapp="whatsapp:+919000000001",
        merchant_id=merchant.id,
        agent_did=agent.agent_did,
        ceiling_paise=1_000_000,
        spent_paise=0,
        max_single_txn_paise=1_000_000,
        allowed_categories=["pulses"],
        min_reversibility=envelope_min_reversibility,
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
        revoked_at=None,
        signature="unused-in-tests",
    )
    session.add(envelope)
    await session.commit()
    await session.refresh(envelope)

    quote = await issue_quote(
        redis,
        product_id=product.id,
        sku=product.sku,
        category_class=product_category_class,
        unit_price_paise=product_unit_price_paise,
        qty=1,
        agent_did=agent.agent_did,
        merchant_did=merchant.did,
        merchant_private_key_hex=merchant_priv,
        now=NOW,
    )

    cart = CartMandate(
        envelope_id=envelope.envelope_id,
        agent_did=agent.agent_did,
        items=[
            {
                "sku": product.sku,
                "category": "pulses",
                "qty": 1,
                "unit_price_paise": product_unit_price_paise,
            }
        ],
        subtotal_paise=product_unit_price_paise,
        tax_paise=0,
        total_paise=product_unit_price_paise,
        reversibility_score=0.9,
        reversibility_breakdown={},
        band="green",
        agent_sig="unused-in-tests",
        created_at=NOW,
    )
    session.add(cart)
    await session.commit()
    await session.refresh(cart)

    return Scenario(merchant, merchant_priv, agent, agent_priv, product, envelope, quote, cart)


def _gate_request(scenario: Scenario, *, cart_id: str | None = None) -> GateRequest:
    nonce = uuid.uuid4().hex
    timestamp = NOW.isoformat()
    body = b"{}"
    signature = _sign_request(scenario.agent_priv, "POST", body, timestamp, nonce)
    return GateRequest(
        session_id=f"session-{uuid.uuid4().hex[:8]}",
        cart_id=cart_id or scenario.cart.cart_id,
        agent_did=scenario.agent.agent_did,
        method="POST",
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        envelope_id=scenario.envelope.envelope_id,
        cart=Cart(
            agent_did=scenario.agent.agent_did,
            items=(
                CartItem(
                    sku=scenario.product.sku,
                    category="pulses",
                    qty=1,
                    unit_price_paise=scenario.quote.unit_price_paise,
                ),
            ),
        ),
        reversibility_items=(),
        quotes=(scenario.quote,),
        idempotency_key=uuid.uuid4().hex,
        now=NOW,
    )


def _reversibility_items(scenario: Scenario, **overrides: object) -> tuple:
    from praman.core.reversibility import ReversibilityItem

    defaults: dict[str, object] = {
        "category_class": scenario.product.category_class,
        "is_personalised": scenario.product.is_personalised,
        "return_window_days": scenario.product.return_window_days,
        "fulfilment_hours": scenario.product.fulfilment_hours,
        "restocking_cost_pct": scenario.product.restocking_cost_pct,
    }
    defaults.update(overrides)
    return (ReversibilityItem(**defaults),)  # type: ignore[arg-type]


async def test_allow_creates_captured_order_and_ledger_event(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()
    req = _gate_request(scenario)
    from dataclasses import replace

    req = replace(req, reversibility_items=_reversibility_items(scenario))

    result = await execute_checkout(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.cart, req
    )

    assert result.gate_result.decision == "ALLOW"
    assert result.order is not None
    assert result.order.status == "captured"
    assert result.order.dispatched_at is not None
    assert result.order.cooling_off_until is None

    events = await db_session.execute(
        select(LedgerEvent).where(LedgerEvent.event_type == "ORDER_DISPATCHED")
    )
    assert len(list(events.scalars().all())) == 1


async def test_hold_creates_order_with_cooling_off_and_notifies_buyer(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, product_category_class="durable")
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()
    from dataclasses import replace

    amber_items = _reversibility_items(
        scenario,
        category_class="durable",
        return_window_days=10,
        fulfilment_hours=48,
        restocking_cost_pct=0.10,
    )
    req = replace(_gate_request(scenario), reversibility_items=amber_items)

    result = await execute_checkout(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.cart, req
    )

    assert result.gate_result.decision == "HOLD"
    assert result.order is not None
    assert result.order.status == "captured"
    assert result.order.cooling_off_until is not None
    assert result.order.dispatched_at is None

    # buyer (envelope.user_whatsapp), not the merchant, gets the undo message
    assert len(whatsapp.sent_messages) == 1
    assert whatsapp.sent_messages[0].to == scenario.envelope.user_whatsapp
    assert "cancel" in whatsapp.sent_messages[0].body.lower()


async def test_escalate_creates_pending_order_and_notifies_merchant(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis, envelope_min_reversibility=0.99)
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()
    from dataclasses import replace

    req = replace(_gate_request(scenario), reversibility_items=_reversibility_items(scenario))

    result = await execute_checkout(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.cart, req
    )

    assert result.gate_result.decision == "ESCALATE"
    assert result.order is not None
    assert result.order.status == "pending_approval"
    assert result.order.razorpay_order_id is None
    assert result.order.stepup_token is not None

    assert len(whatsapp.sent_messages) == 1
    assert whatsapp.sent_messages[0].to == scenario.merchant.whatsapp_number
    assert "APPROVE" in whatsapp.sent_messages[0].body


async def test_block_creates_no_order(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()
    from dataclasses import replace

    req = replace(
        _gate_request(scenario),
        reversibility_items=_reversibility_items(scenario),
        signature="00" * 64,  # invalid signature -> R01 BLOCK
    )

    result = await execute_checkout(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.cart, req
    )

    assert result.gate_result.decision == "BLOCK"
    assert result.order is None
    assert len(whatsapp.sent_messages) == 0


async def test_substitute_offers_ranked_candidates(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    # a second product in the same category, in stock, eligible to substitute
    substitute_product = Product(
        merchant_id=scenario.merchant.id,
        sku="substitute-sku",
        name="Substitute Product",
        category="pulses",
        category_class="consumable",
        unit_price_paise=950,
        stock=50,
        return_window_days=14,
        fulfilment_hours=24,
        restocking_cost_pct=0.0,
        is_personalised=False,
        field_confidence={},
        needs_review=False,
        source="manual",
    )
    db_session.add(substitute_product)
    await db_session.commit()

    # drop the original product's stock below the quoted qty
    scenario.product.stock = 0
    db_session.add(scenario.product)
    await db_session.commit()

    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()
    llm.enqueue(json.dumps({"ranked_skus": ["substitute-sku"], "rationale": "closest match"}))
    from dataclasses import replace

    req = replace(_gate_request(scenario), reversibility_items=_reversibility_items(scenario))

    result = await execute_checkout(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.cart, req
    )

    assert result.gate_result.decision == "SUBSTITUTE"
    assert result.order is None
    assert [c.sku for c in result.substitution_offer] == ["substitute-sku"]
    assert result.substitution_rationale == "closest match"
