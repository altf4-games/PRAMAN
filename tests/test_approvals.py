from __future__ import annotations

import hashlib
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
from praman.models import Agent, CartMandate, IntentEnvelope, LedgerEvent, Merchant, Order, Product
from praman.whatsapp.approvals import (
    find_pending_approval_order,
    handle_merchant_reply,
    sweep_expired_approvals,
)
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
    envelope_min_reversibility: float = 0.99,
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
        max_txn_paise=1_000_000,
        daily_cap_paise=10_000_000,
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
        category_class="consumable",
        unit_price_paise=1000,
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
        category_class="consumable",
        unit_price_paise=1000,
        qty=1,
        agent_did=agent.agent_did,
        merchant_did=merchant.did,
        merchant_private_key_hex=merchant_priv,
        now=NOW,
    )

    cart = CartMandate(
        envelope_id=envelope.envelope_id,
        agent_did=agent.agent_did,
        items=[{"sku": product.sku, "category": "pulses", "qty": 1, "unit_price_paise": 1000}],
        subtotal_paise=1000,
        tax_paise=0,
        total_paise=1000,
        reversibility_score=0.5,
        reversibility_breakdown={},
        band="red",
        agent_sig="unused-in-tests",
        created_at=NOW,
    )
    session.add(cart)
    await session.commit()
    await session.refresh(cart)

    return Scenario(merchant, merchant_priv, agent, agent_priv, product, envelope, quote, cart)


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


def _gate_request(scenario: Scenario) -> GateRequest:
    nonce = uuid.uuid4().hex
    timestamp = NOW.isoformat()
    body = b"{}"
    signature = _sign_request(scenario.agent_priv, "POST", body, timestamp, nonce)
    return GateRequest(
        session_id=f"session-{uuid.uuid4().hex[:8]}",
        cart_id=scenario.cart.cart_id,
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
        reversibility_items=_reversibility_items(scenario),
        quotes=(scenario.quote,),
        idempotency_key=uuid.uuid4().hex,
        now=NOW,
    )


async def _create_pending_order(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis, scenario: Scenario
) -> Order:
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()
    req = _gate_request(scenario)

    result = await execute_checkout(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.cart, req
    )
    assert result.gate_result.decision == "ESCALATE"
    assert result.order is not None
    return result.order


async def test_find_pending_approval_order_returns_none_when_nothing_pending(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    order = await find_pending_approval_order(db_session, scenario.merchant)
    assert order is None


async def test_find_pending_approval_order_finds_it(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    pending = await _create_pending_order(db_session, redis, scenario)

    found = await find_pending_approval_order(db_session, scenario.merchant)

    assert found is not None
    assert found.id == pending.id


async def test_handle_merchant_reply_returns_false_for_unrelated_message(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    await _create_pending_order(db_session, redis, scenario)
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    handled = await handle_merchant_reply(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.merchant, "hello", now=NOW
    )
    assert handled is False


async def test_handle_merchant_reply_returns_false_when_nothing_pending(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    handled = await handle_merchant_reply(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.merchant, "approve", now=NOW
    )
    assert handled is False


async def test_decline_closes_order_cleanly(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    pending = await _create_pending_order(db_session, redis, scenario)
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    handled = await handle_merchant_reply(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.merchant, "decline", now=NOW
    )
    assert handled is True

    await db_session.refresh(pending)
    assert pending.status == "declined"
    assert pending.razorpay_order_id is None
    assert "declined" in whatsapp.sent_messages[-1].body.lower()


async def test_approve_updates_the_same_order_in_place(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    pending = await _create_pending_order(db_session, redis, scenario)
    pending_id = pending.id
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    handled = await handle_merchant_reply(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.merchant, "approve", now=NOW
    )
    assert handled is True

    orders_result = await db_session.execute(
        select(Order).where(Order.cart_id == scenario.cart.cart_id)
    )
    orders = list(orders_result.scalars().all())
    assert len(orders) == 1  # updated in place, not duplicated
    assert orders[0].id == pending_id
    assert orders[0].status == "captured"
    assert orders[0].razorpay_order_id is not None
    assert orders[0].stepup_confirmed_at is not None
    assert orders[0].stepup_channel == "whatsapp"
    assert "approved" in whatsapp.sent_messages[-1].body.lower()


async def test_approve_emits_merchant_approved_ledger_event(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    await _create_pending_order(db_session, redis, scenario)
    registry = LocalRegistry(db_session)
    razorpay = FakeRazorpayClient()
    whatsapp = FakeWhatsAppClient()
    llm = FakeLLMClient()

    await handle_merchant_reply(
        db_session, redis, registry, razorpay, whatsapp, llm, scenario.merchant, "approve", now=NOW
    )

    events = await db_session.execute(
        select(LedgerEvent).where(LedgerEvent.event_type == "MERCHANT_APPROVED")
    )
    rows = list(events.scalars().all())
    assert len(rows) == 1
    assert rows[0].payload_json["outcome"] == "ALLOW"


async def test_sweep_expired_approvals_denies_past_deadline_orders(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    from praman.config import MERCHANT_APPROVAL_TIMEOUT_S

    scenario = await _build_scenario(db_session, redis)
    pending = await _create_pending_order(db_session, redis, scenario)
    whatsapp = FakeWhatsAppClient()

    far_future = NOW + timedelta(seconds=MERCHANT_APPROVAL_TIMEOUT_S + 1)
    denied_count = await sweep_expired_approvals(db_session, whatsapp, far_future)

    assert denied_count == 1
    await db_session.refresh(pending)
    assert pending.status == "denied"


async def test_sweep_expired_approvals_leaves_fresh_orders_alone(
    db_session: AsyncSession, redis: fakeredis.aioredis.FakeRedis
) -> None:
    scenario = await _build_scenario(db_session, redis)
    pending = await _create_pending_order(db_session, redis, scenario)
    whatsapp = FakeWhatsAppClient()

    denied_count = await sweep_expired_approvals(db_session, whatsapp, NOW + timedelta(seconds=5))

    assert denied_count == 0
    await db_session.refresh(pending)
    assert pending.status == "pending_approval"
