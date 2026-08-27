"""Buyer-side cooling-off undo. `checkout.py::_handle_hold` already sends the
buyer the outbound "reply CANCEL" WhatsApp message when an amber order is
held (`core/cooling_off.py::buyer_undo_message`). This module handles the
buyer's reply: CANCEL on a still-held order refunds it and marks it
cancelled, via the same `core.checkout.cancel_order` the REST `order_undo`
route uses, so the two entry points can never drift apart.

Once `cooling_off_until` elapses without a cancel, the scheduler sweep
(`scheduler.py`) dispatches the order instead — this module only ever
touches orders that are still within their window.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.adapters.razorpay_client import RazorpayClient
from praman.core.checkout import cancel_order
from praman.models import CartMandate, IntentEnvelope, Order
from praman.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)

_CANCEL_WORDS = {"cancel", "undo", "stop"}


async def find_pending_cooling_off_order(
    session: AsyncSession, user_whatsapp: str
) -> tuple[Order, CartMandate] | None:
    result = await session.execute(
        select(Order, CartMandate)
        .join(CartMandate, Order.cart_id == CartMandate.cart_id)
        .join(IntentEnvelope, CartMandate.envelope_id == IntentEnvelope.envelope_id)
        .where(
            IntentEnvelope.user_whatsapp == user_whatsapp,
            Order.status == "captured",
            Order.cooling_off_until.is_not(None),
            Order.dispatched_at.is_(None),
            Order.cancelled_at.is_(None),
        )
        .order_by(Order.created_at.desc())
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def handle_buyer_reply(
    session: AsyncSession,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    user_whatsapp: str,
    body: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Returns True if `body` was handled as a cancel reply to a pending
    cooling-off order — False means there was nothing pending, or the
    message didn't look like a cancel, and the caller should fall through
    to whatever else handles this buyer's messages."""
    resolved_now = now if now is not None else datetime.now(UTC)
    if body.strip().lower() not in _CANCEL_WORDS:
        return False

    found = await find_pending_cooling_off_order(session, user_whatsapp)
    if found is None:
        return False
    order, cart = found

    cancelled = await cancel_order(
        session,
        razorpay,
        order,
        amount_paise=cart.total_paise,
        now=resolved_now,
        reason="buyer_whatsapp_cancel",
    )
    if not cancelled:
        return False

    try:
        await whatsapp.send_text(
            user_whatsapp, "Order cancelled and refunded. It will not be dispatched."
        )
    except Exception:
        logger.warning("cooling_off_notify: failed to send cancel confirmation", exc_info=True)
    return True
