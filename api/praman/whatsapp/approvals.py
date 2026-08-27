"""Merchant escalation inbox. When the gate ESCALATEs (R08/R11),
`checkout.py` creates a `pending_approval` Order and sends the merchant a
WhatsApp Approve/Decline (via `send_approval_request` below, called from
`checkout._handle_escalate`). This module handles the merchant's reply:

- APPROVE: re-runs the gate from R01 against the *original* signed request
  (`human_present=True` — see `core/gate.py`'s module docstring for why the
  nonce-replay check is safely skipped only here), then proceeds through
  checkout exactly as a fresh ALLOW/HOLD would, updating the same order
  row rather than creating a second one.
- DECLINE: closes the order cleanly, no payment ever touched.
- Timeout (`sweep_expired_approvals`, called by the scheduler): denies
  anything still pending past `MERCHANT_APPROVAL_TIMEOUT_S`.

Every transition ledgers (the design spec §6).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.adapters.llm import LLMClient
from praman.adapters.razorpay_client import RazorpayClient
from praman.core.checkout import execute_checkout
from praman.core.gate import deserialize_gate_request
from praman.core.ledger import append_event
from praman.core.registry import AgentRegistry
from praman.core.stepup import approval_deadline, is_approval_expired, merchant_approval_message
from praman.models import CartMandate, IntentEnvelope, Merchant, Order
from praman.timeutil import as_aware_utc
from praman.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)

_APPROVE_WORDS = {"approve", "yes", "y"}
_DECLINE_WORDS = {"decline", "no", "n"}


async def send_approval_request(
    session: AsyncSession,
    whatsapp: WhatsAppClient,
    merchant: Merchant,
    item_summary: str,
    amount_paise: int,
    reason: str,
) -> None:
    message = merchant_approval_message(item_summary, amount_paise, reason)
    try:
        await whatsapp.send_text(merchant.whatsapp_number, message)
    except Exception:
        logger.warning("approvals: failed to send merchant approval request", exc_info=True)


async def find_pending_approval_order(session: AsyncSession, merchant: Merchant) -> Order | None:
    result = await session.execute(
        select(Order)
        .join(CartMandate, Order.cart_id == CartMandate.cart_id)
        .join(IntentEnvelope, CartMandate.envelope_id == IntentEnvelope.envelope_id)
        .where(IntentEnvelope.merchant_id == merchant.id, Order.status == "pending_approval")
        .order_by(Order.created_at.desc())
    )
    return result.scalars().first()


async def list_pending_approvals(session: AsyncSession, merchant_id: str) -> list[Order]:
    """The full inbox (the design spec's §7 `/approvals` page), as opposed to
    `find_pending_approval_order`'s "most recent one" used by the WhatsApp
    reply handler, which only ever needs to act on a single message."""
    result = await session.execute(
        select(Order)
        .join(CartMandate, Order.cart_id == CartMandate.cart_id)
        .join(IntentEnvelope, CartMandate.envelope_id == IntentEnvelope.envelope_id)
        .where(IntentEnvelope.merchant_id == merchant_id, Order.status == "pending_approval")
        .order_by(Order.created_at.desc())
    )
    return list(result.scalars().all())


async def decide_by_order_id(
    session: AsyncSession,
    redis: Redis,
    registry: AgentRegistry,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    order_id: str,
    decision: str,
    *,
    demo_mode: bool = False,
    now: datetime | None = None,
) -> Order:
    """The REST `/api/approvals/{order_id}/decide` entrypoint — same
    `_approve`/`_decline` logic the WhatsApp reply handler uses, just
    addressed by `order_id` directly instead of "the merchant's most
    recent pending order", so the frontend's Approve/Decline buttons and a
    merchant's WhatsApp reply can never disagree about what actually ran."""
    resolved_now = now if now is not None else datetime.now(UTC)
    order_result = await session.execute(select(Order).where(Order.id == order_id))
    order = order_result.scalar_one_or_none()
    if order is None or order.status != "pending_approval":
        raise ValueError(f"order {order_id!r} is not pending approval")

    env_result = await session.execute(
        select(IntentEnvelope, Merchant)
        .join(CartMandate, IntentEnvelope.envelope_id == CartMandate.envelope_id)
        .join(Merchant, IntentEnvelope.merchant_id == Merchant.id)
        .where(CartMandate.cart_id == order.cart_id)
    )
    row = env_result.first()
    if row is None:
        raise ValueError(f"order {order_id!r} has no resolvable merchant")
    merchant = row[1]

    if decision == "decline":
        await _decline(session, whatsapp, merchant, order, resolved_now)
    elif decision == "approve":
        await _approve(
            session,
            redis,
            registry,
            razorpay,
            whatsapp,
            llm,
            merchant,
            order,
            demo_mode,
            resolved_now,
        )
    else:
        raise ValueError(f"decision must be 'approve' or 'decline', got {decision!r}")

    await session.refresh(order)
    return order


async def handle_merchant_reply(
    session: AsyncSession,
    redis: Redis,
    registry: AgentRegistry,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    merchant: Merchant,
    body: str,
    *,
    demo_mode: bool = False,
    now: datetime | None = None,
) -> bool:
    """Returns True if `body` was handled as an approve/decline reply to a
    pending order — False means there was nothing pending, or the message
    didn't look like one, and the caller should fall through to whatever
    else handles this merchant's messages. `now` defaults to the real wall
    clock in production; tests inject it, same discipline as `gate.py`."""
    resolved_now = now if now is not None else datetime.now(UTC)
    stripped = body.strip().lower()
    if stripped not in _APPROVE_WORDS and stripped not in _DECLINE_WORDS:
        return False

    order = await find_pending_approval_order(session, merchant)
    if order is None:
        return False

    if stripped in _DECLINE_WORDS:
        await _decline(session, whatsapp, merchant, order, resolved_now)
        return True

    await _approve(
        session, redis, registry, razorpay, whatsapp, llm, merchant, order, demo_mode, resolved_now
    )
    return True


async def _decline(
    session: AsyncSession, whatsapp: WhatsAppClient, merchant: Merchant, order: Order, now: datetime
) -> None:
    order.status = "declined"
    order.cancelled_at = now
    session.add(order)
    await session.commit()

    await append_event(session, f"order:{order.id}", None, "ORDER_DECLINED", {"order_id": order.id})
    try:
        await whatsapp.send_text(merchant.whatsapp_number, "Order declined. No payment was taken.")
    except Exception:
        logger.warning("approvals: failed to send decline confirmation", exc_info=True)


async def _approve(
    session: AsyncSession,
    redis: Redis,
    registry: AgentRegistry,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    merchant: Merchant,
    order: Order,
    demo_mode: bool,
    now: datetime,
) -> None:
    if order.pending_gate_request is None:
        logger.error("approvals: order %s has no pending_gate_request to replay", order.id)
        return

    cart_result = await session.execute(
        select(CartMandate).where(CartMandate.cart_id == order.cart_id)
    )
    cart = cart_result.scalar_one_or_none()
    if cart is None:
        logger.error("approvals: order %s references a missing cart %s", order.id, order.cart_id)
        return

    gate_req = deserialize_gate_request(order.pending_gate_request, now=now)

    result = await execute_checkout(
        session,
        redis,
        registry,
        razorpay,
        whatsapp,
        llm,
        cart,
        gate_req,
        demo_mode=demo_mode,
        existing_order=order,
    )

    await append_event(
        session,
        gate_req.session_id,
        gate_req.agent_did,
        "MERCHANT_APPROVED",
        {"order_id": order.id, "outcome": result.gate_result.decision},
    )

    try:
        if result.gate_result.decision in ("ALLOW", "HOLD"):
            await whatsapp.send_text(merchant.whatsapp_number, "Approved — order confirmed.")
        else:
            await whatsapp.send_text(
                merchant.whatsapp_number,
                f"Approved, but the order still couldn't proceed: {result.gate_result.detail}",
            )
    except Exception:
        logger.warning("approvals: failed to send approval confirmation", exc_info=True)


async def sweep_expired_approvals(
    session: AsyncSession, whatsapp: WhatsAppClient, now: datetime
) -> int:
    """Called by the scheduler sweep (the design spec §6: 'Timeout (default 15
    min) -> deny'). Returns how many orders were denied."""
    result = await session.execute(select(Order).where(Order.status == "pending_approval"))
    pending = result.scalars().all()

    denied_count = 0
    for order in pending:
        deadline_data = order.pending_gate_request
        if deadline_data is None:
            continue
        # The approval deadline isn't itself persisted on the order, so we
        # derive it from when the order was created — simpler than adding
        # yet another column for a single timestamp.
        deadline = approval_deadline(as_aware_utc(order.created_at))
        if not is_approval_expired(deadline, now):
            continue

        order.status = "denied"
        order.cancelled_at = now
        session.add(order)
        await session.commit()
        await append_event(
            session, f"order:{order.id}", None, "ORDER_APPROVAL_TIMED_OUT", {"order_id": order.id}
        )
        denied_count += 1

    return denied_count
