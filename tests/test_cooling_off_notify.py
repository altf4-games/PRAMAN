from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from praman.adapters.razorpay_client import FakeRazorpayClient
from praman.models import CartMandate, IntentEnvelope, LedgerEvent, Merchant, Order
from praman.whatsapp.client import FakeWhatsAppClient
from praman.whatsapp.cooling_off_notify import find_pending_cooling_off_order, handle_buyer_reply
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
BUYER = "whatsapp:+919000000001"


async def _make_held_order(session: AsyncSession, razorpay: FakeRazorpayClient) -> Order:
    merchant = Merchant(
        name="M",
        did="did:key:zM",
        public_key="pub",
        private_key_enc="enc",
        whatsapp_number=f"whatsapp:+91{uuid.uuid4().int % 10**10:010d}",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=NOW,
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)

    envelope = IntentEnvelope(
        user_ref="user-1",
        user_whatsapp=BUYER,
        merchant_id=merchant.id,
        agent_did="did:key:zAgent",
        ceiling_paise=100_000,
        spent_paise=0,
        max_single_txn_paise=100_000,
        allowed_categories=["jewellery"],
        min_reversibility=0.0,
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
        revoked_at=None,
        signature="sig",
    )
    session.add(envelope)
    await session.commit()
    await session.refresh(envelope)

    cart = CartMandate(
        envelope_id=envelope.envelope_id,
        agent_did="did:key:zAgent",
        items=[{"sku": "x", "category": "jewellery", "qty": 1, "unit_price_paise": 5000}],
        subtotal_paise=5000,
        tax_paise=0,
        total_paise=5000,
        reversibility_score=0.5,
        reversibility_breakdown={},
        band="amber",
        agent_sig="sig",
        created_at=NOW,
    )
    session.add(cart)
    await session.commit()
    await session.refresh(cart)

    rp_order = razorpay.create_order(5000, "INR", cart.cart_id, {})
    payment = razorpay.simulate_payment(rp_order.order_id)

    order = Order(
        cart_id=cart.cart_id,
        razorpay_order_id=rp_order.order_id,
        razorpay_payment_id=payment.payment_id,
        status="captured",
        idempotency_key=uuid.uuid4().hex,
        cooling_off_until=NOW + timedelta(minutes=30),
        dispatched_at=None,
        created_at=NOW,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


@pytest.fixture
def razorpay() -> FakeRazorpayClient:
    return FakeRazorpayClient()


@pytest.fixture
def whatsapp() -> FakeWhatsAppClient:
    return FakeWhatsAppClient()


async def test_finds_pending_order_for_buyer(
    db_session: AsyncSession, razorpay: FakeRazorpayClient
) -> None:
    order = await _make_held_order(db_session, razorpay)
    found = await find_pending_cooling_off_order(db_session, BUYER)
    assert found is not None
    assert found[0].id == order.id


async def test_cancel_word_refunds_and_cancels_order(
    db_session: AsyncSession, razorpay: FakeRazorpayClient, whatsapp: FakeWhatsAppClient
) -> None:
    order = await _make_held_order(db_session, razorpay)
    handled = await handle_buyer_reply(db_session, razorpay, whatsapp, BUYER, "CANCEL", now=NOW)

    assert handled is True
    await db_session.refresh(order)
    assert order.status == "cancelled"
    assert order.cancelled_at is not None
    assert order.refunded_at is not None
    assert any("cancelled and refunded" in m.body for m in whatsapp.sent_messages)

    events = await db_session.execute(
        select(LedgerEvent).where(LedgerEvent.event_type == "ORDER_CANCELLED")
    )
    assert len(list(events.scalars().all())) == 1


async def test_non_cancel_message_is_not_handled(
    db_session: AsyncSession, razorpay: FakeRazorpayClient, whatsapp: FakeWhatsAppClient
) -> None:
    await _make_held_order(db_session, razorpay)
    handled = await handle_buyer_reply(db_session, razorpay, whatsapp, BUYER, "hello", now=NOW)
    assert handled is False


async def test_no_pending_order_returns_false(
    db_session: AsyncSession, razorpay: FakeRazorpayClient, whatsapp: FakeWhatsAppClient
) -> None:
    handled = await handle_buyer_reply(
        db_session, razorpay, whatsapp, "whatsapp:+910000000000", "CANCEL", now=NOW
    )
    assert handled is False


async def test_already_dispatched_order_is_not_cancellable(
    db_session: AsyncSession, razorpay: FakeRazorpayClient, whatsapp: FakeWhatsAppClient
) -> None:
    order = await _make_held_order(db_session, razorpay)
    order.dispatched_at = NOW
    db_session.add(order)
    await db_session.commit()

    handled = await handle_buyer_reply(db_session, razorpay, whatsapp, BUYER, "CANCEL", now=NOW)
    assert handled is False
