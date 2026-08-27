from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from praman.models import CartMandate, IntentEnvelope, LedgerEvent, Merchant, Order
from praman.scheduler import sweep_cooling_off_dispatch
from praman.whatsapp.client import FakeWhatsAppClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


async def _make_held_order(
    session: AsyncSession, *, cooling_off_until: datetime, cancelled: bool = False
) -> Order:
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
        user_whatsapp="whatsapp:+919000000001",
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

    order = Order(
        cart_id=cart.cart_id,
        razorpay_order_id="order_fake_1",
        razorpay_payment_id="pay_fake_1",
        status="captured",
        idempotency_key=uuid.uuid4().hex,
        cooling_off_until=cooling_off_until,
        dispatched_at=None,
        cancelled_at=NOW if cancelled else None,
        created_at=NOW,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


@pytest.fixture
def whatsapp() -> FakeWhatsAppClient:
    return FakeWhatsAppClient()


async def test_dispatches_order_whose_window_has_elapsed(db_session: AsyncSession) -> None:
    order = await _make_held_order(db_session, cooling_off_until=NOW - timedelta(minutes=1))
    count = await sweep_cooling_off_dispatch(db_session, NOW)
    assert count == 1
    await db_session.refresh(order)
    assert order.dispatched_at is not None

    events = await db_session.execute(
        select(LedgerEvent).where(LedgerEvent.event_type == "ORDER_DISPATCHED")
    )
    assert len(list(events.scalars().all())) == 1


async def test_does_not_dispatch_order_still_within_window(db_session: AsyncSession) -> None:
    order = await _make_held_order(db_session, cooling_off_until=NOW + timedelta(minutes=10))
    count = await sweep_cooling_off_dispatch(db_session, NOW)
    assert count == 0
    await db_session.refresh(order)
    assert order.dispatched_at is None


async def test_does_not_dispatch_already_cancelled_order(db_session: AsyncSession) -> None:
    await _make_held_order(db_session, cooling_off_until=NOW - timedelta(minutes=1), cancelled=True)
    count = await sweep_cooling_off_dispatch(db_session, NOW)
    assert count == 0


async def test_sweep_is_idempotent_on_repeated_runs(db_session: AsyncSession) -> None:
    await _make_held_order(db_session, cooling_off_until=NOW - timedelta(minutes=1))
    first = await sweep_cooling_off_dispatch(db_session, NOW)
    second = await sweep_cooling_off_dispatch(db_session, NOW)
    assert first == 1
    assert second == 0
