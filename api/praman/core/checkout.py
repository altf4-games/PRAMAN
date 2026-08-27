"""Checkout orchestration — what happens after the gate decides. An order
is created only after ALLOW or HOLD (CLAUDE.md §6): a BLOCK/SUBSTITUTE
never touches Razorpay, and an ESCALATE creates a pending order awaiting
merchant approval but never captures payment until that approval lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from praman.adapters.llm import LLMClient
from praman.adapters.razorpay_client import (
    RazorpayClient,
    create_and_capture_order,
    make_idempotency_key,
)
from praman.core.cooling_off import buyer_undo_message, compute_cooling_off_until
from praman.core.crypto_helpers import cart_mandate_hash
from praman.core.gate import GateRequest, run_gate, serialize_gate_request
from praman.core.gate_types import GateResult
from praman.core.ledger import append_event
from praman.core.registry import AgentRegistry
from praman.core.stepup import generate_stepup_token, merchant_approval_message
from praman.core.substitution import SubstitutionCandidate, deterministic_filter, rank_candidates
from praman.models import CartMandate, IntentEnvelope, Merchant, Order, Product
from praman.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    gate_result: GateResult
    order: Order | None = None
    substitution_offer: list[SubstitutionCandidate] = field(default_factory=list)
    substitution_rationale: str | None = None


async def _get_merchant_for_envelope(session: AsyncSession, envelope_id: str) -> Merchant | None:
    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == envelope_id)
    )
    env_row = env_result.scalar_one_or_none()
    if env_row is None:
        return None
    merchant_result = await session.execute(
        select(Merchant).where(Merchant.id == env_row.merchant_id)
    )
    return merchant_result.scalar_one_or_none()


async def _create_order_row(
    session: AsyncSession,
    cart: CartMandate,
    *,
    razorpay_order_id: str | None,
    razorpay_payment_id: str | None,
    status: str,
    idempotency_key: str,
    cooling_off_until: datetime | None,
    dispatched_at: datetime | None,
    now: datetime,
    stepup_token: str | None = None,
    pending_gate_request: dict[str, object] | None = None,
    existing_order: Order | None = None,
) -> Order:
    """Inserts a new Order — or, if `existing_order` is given, updates it
    in place. The update path exists for exactly one case: a merchant's
    WhatsApp Approve turning a `pending_approval` order into a `captured`
    one. `idempotency_key` is deterministic (`hash(cart_id, agent_did)`)
    and unique-constrained, so the retry can't insert a second row with
    the same key — it must become the same row.

    `now` is always caller-injected (from `gate_req.now`), never read from
    the wall clock here — consistent with `gate.py`/`envelope.py`'s own
    testability discipline, and what makes `Order.created_at` (used by the
    merchant-approval timeout sweep) deterministic under test.
    """
    if existing_order is not None:
        existing_order.razorpay_order_id = razorpay_order_id
        existing_order.razorpay_payment_id = razorpay_payment_id
        existing_order.status = status
        existing_order.cooling_off_until = cooling_off_until
        existing_order.dispatched_at = dispatched_at
        existing_order.stepup_confirmed_at = now
        existing_order.stepup_channel = "whatsapp"
        session.add(existing_order)
        await session.commit()
        await session.refresh(existing_order)
        return existing_order

    order = Order(
        cart_id=cart.cart_id,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        status=status,
        idempotency_key=idempotency_key,
        cooling_off_until=cooling_off_until,
        dispatched_at=dispatched_at,
        stepup_token=stepup_token,
        pending_gate_request=pending_gate_request,
        created_at=now,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order


async def execute_checkout(
    session: AsyncSession,
    redis: Redis,
    registry: AgentRegistry,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    llm: LLMClient,
    cart: CartMandate,
    gate_req: GateRequest,
    *,
    demo_mode: bool = False,
    existing_order: Order | None = None,
) -> CheckoutResult:
    """`existing_order` is set only when this is a merchant-approval retry
    (see `whatsapp/approvals.py`) — the previously-pending order that an
    ALLOW/HOLD outcome now updates in place rather than duplicating."""
    result = await run_gate(session, redis, registry, gate_req, demo_mode=demo_mode)
    now = gate_req.now

    if result.decision == "ALLOW":
        return await _handle_allow(session, razorpay, cart, gate_req, result, now, existing_order)

    if result.decision == "HOLD":
        return await _handle_hold(
            session, razorpay, whatsapp, cart, gate_req, result, now, demo_mode, existing_order
        )

    if result.decision == "ESCALATE":
        return await _handle_escalate(
            session, whatsapp, cart, gate_req, result, now, existing_order
        )

    if result.decision == "SUBSTITUTE":
        return await _handle_substitute(session, llm, cart, gate_req, result)

    # BLOCK — no order, nothing further to do.
    return CheckoutResult(gate_result=result)


async def _handle_allow(
    session: AsyncSession,
    razorpay: RazorpayClient,
    cart: CartMandate,
    gate_req: GateRequest,
    result: GateResult,
    now: datetime,
    existing_order: Order | None = None,
) -> CheckoutResult:
    idempotency_key = make_idempotency_key(cart.cart_id, gate_req.agent_did)
    notes = {"cart_mandate_hash": cart_mandate_hash(cart), "praman_cart_id": cart.cart_id}
    order_row, payment, path = create_and_capture_order(
        razorpay, cart.total_paise, "INR", cart.cart_id, notes
    )
    logger.info("checkout: order %s captured via %s path", order_row.order_id, path)

    order = await _create_order_row(
        session,
        cart,
        razorpay_order_id=order_row.order_id,
        razorpay_payment_id=payment.payment_id,
        status="captured",
        idempotency_key=idempotency_key,
        cooling_off_until=None,
        dispatched_at=now,
        now=now,
        existing_order=existing_order,
    )
    await append_event(
        session,
        gate_req.session_id,
        gate_req.agent_did,
        "ORDER_DISPATCHED",
        {"order_id": order.id, "razorpay_order_id": order_row.order_id, "capture_path": path},
    )
    return CheckoutResult(gate_result=result, order=order)


async def _handle_hold(
    session: AsyncSession,
    razorpay: RazorpayClient,
    whatsapp: WhatsAppClient,
    cart: CartMandate,
    gate_req: GateRequest,
    result: GateResult,
    now: datetime,
    demo_mode: bool,
    existing_order: Order | None = None,
) -> CheckoutResult:
    idempotency_key = make_idempotency_key(cart.cart_id, gate_req.agent_did)
    notes = {"cart_mandate_hash": cart_mandate_hash(cart), "praman_cart_id": cart.cart_id}
    order_row, payment, path = create_and_capture_order(
        razorpay, cart.total_paise, "INR", cart.cart_id, notes
    )
    logger.info(
        "checkout: amber order %s captured via %s path, held for cooling-off",
        order_row.order_id,
        path,
    )

    cooling_off_until = compute_cooling_off_until(now, demo_mode=demo_mode)
    order = await _create_order_row(
        session,
        cart,
        razorpay_order_id=order_row.order_id,
        razorpay_payment_id=payment.payment_id,
        status="captured",
        idempotency_key=idempotency_key,
        cooling_off_until=cooling_off_until,
        dispatched_at=None,
        now=now,
        existing_order=existing_order,
    )
    await append_event(
        session,
        gate_req.session_id,
        gate_req.agent_did,
        "ORDER_HELD_COOLING_OFF",
        {
            "order_id": order.id,
            "razorpay_order_id": order_row.order_id,
            "cooling_off_until": cooling_off_until.isoformat(),
            "capture_path": path,
        },
    )

    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == gate_req.envelope_id)
    )
    env_row = env_result.scalar_one_or_none()
    merchant = await _get_merchant_for_envelope(session, gate_req.envelope_id)
    item_summary = ", ".join(f"{item['qty']}x {item['sku']}" for item in cart.items)
    if merchant is not None and env_row is not None:
        window_minutes = int((cooling_off_until - now).total_seconds() // 60) or 1
        message = buyer_undo_message(item_summary, cart.total_paise, merchant.name, window_minutes)
        try:
            await whatsapp.send_text(env_row.user_whatsapp, message)
        except Exception:
            logger.warning("checkout: failed to send buyer cooling-off notification", exc_info=True)

    return CheckoutResult(gate_result=result, order=order)


async def _handle_escalate(
    session: AsyncSession,
    whatsapp: WhatsAppClient,
    cart: CartMandate,
    gate_req: GateRequest,
    result: GateResult,
    now: datetime,
    existing_order: Order | None = None,
) -> CheckoutResult:
    # Re-escalating on a merchant-approval retry (e.g. R08 now passes but
    # R11 doesn't) refreshes the same pending order rather than inserting
    # a second row under the same idempotency_key.
    stepup_token = generate_stepup_token()
    if existing_order is not None:
        existing_order.stepup_token = stepup_token
        existing_order.pending_gate_request = serialize_gate_request(gate_req)
        session.add(existing_order)
        await session.commit()
        await session.refresh(existing_order)
        order = existing_order
    else:
        order = await _create_order_row(
            session,
            cart,
            razorpay_order_id=None,
            razorpay_payment_id=None,
            status="pending_approval",
            idempotency_key=make_idempotency_key(cart.cart_id, gate_req.agent_did),
            cooling_off_until=None,
            dispatched_at=None,
            now=now,
            stepup_token=stepup_token,
            pending_gate_request=serialize_gate_request(gate_req),
        )
    await append_event(
        session,
        gate_req.session_id,
        gate_req.agent_did,
        "ORDER_PENDING_APPROVAL",
        {"order_id": order.id, "reason_code": result.reason_code, "stepup_token": stepup_token},
    )

    merchant = await _get_merchant_for_envelope(session, gate_req.envelope_id)
    if merchant is not None:
        item_summary = ", ".join(f"{item['qty']}x {item['sku']}" for item in cart.items)
        message = merchant_approval_message(item_summary, cart.total_paise, result.detail)
        try:
            await whatsapp.send_text(merchant.whatsapp_number, message)
        except Exception:
            logger.warning("checkout: failed to send merchant approval request", exc_info=True)

    return CheckoutResult(gate_result=result, order=order)


async def cancel_order(
    session: AsyncSession,
    razorpay: RazorpayClient,
    order: Order,
    *,
    amount_paise: int,
    now: datetime,
    reason: str,
) -> bool:
    """Cancels a still-open amber (cooling-off) order and refunds it via
    Razorpay. Shared by the buyer's WhatsApp CANCEL reply
    (`whatsapp/cooling_off_notify.py`) and the REST `order_undo` route so
    the two entry points can't drift. Returns False (no-op, nothing
    ledgered) if the order isn't in a cancellable state — already
    dispatched, already cancelled, or never held for cooling-off in the
    first place; a cancellation only ever undoes a *held* order, never a
    dispatched one (CLAUDE.md's cooling-off window is exactly the "still
    undoable" window). `amount_paise` is the caller's responsibility (the
    cart total) since `Order` itself doesn't carry an amount column —
    every call site already has the cart in scope to read it from.

    Dispatch is withheld unconditionally (`status`/`cancelled_at` are
    always set once we get this far) — but `refunded_at` is set only if
    the refund call actually succeeded. A real bug lived here: it used to
    set `refunded_at` regardless of whether `refund_payment` raised,
    caught via `create_and_capture_order`'s own fallback-client bug (see
    its docstring) making refunds fail in a way this function then
    silently claimed had succeeded — exactly the kind of "evidence by
    construction" gap the whole project exists to prevent."""
    if order.status != "captured" or order.cooling_off_until is None:
        return False
    if order.dispatched_at is not None or order.cancelled_at is not None:
        return False

    refund_succeeded = False
    if order.razorpay_payment_id is not None:
        try:
            razorpay.refund_payment(order.razorpay_payment_id, amount_paise)
            refund_succeeded = True
        except Exception:
            logger.warning(
                "checkout: refund failed for order %s, cancelling anyway", order.id, exc_info=True
            )

    order.status = "cancelled"
    order.cancelled_at = now
    order.refunded_at = now if refund_succeeded else None
    session.add(order)
    await session.commit()

    await append_event(
        session,
        f"order:{order.id}",
        None,
        "ORDER_CANCELLED",
        {"order_id": order.id, "reason": reason, "refund_succeeded": refund_succeeded},
    )
    return True


async def _handle_substitute(
    session: AsyncSession,
    llm: LLMClient,
    cart: CartMandate,
    gate_req: GateRequest,
    result: GateResult,
) -> CheckoutResult:
    # The out-of-stock item is whichever cart item's quote failed R07 —
    # gate.py stops at the first failing quote, so it's the item at that
    # same index in the cart/quotes pair.
    out_of_stock_sku = None
    for item, quote in zip(gate_req.cart.items, gate_req.quotes, strict=False):
        product_result = await session.execute(
            select(Product).where(Product.id == quote.product_id)
        )
        product = product_result.scalar_one_or_none()
        if product is not None and (product.stock or 0) < quote.qty:
            out_of_stock_sku = item.sku
            original_product = product
            original_qty = quote.qty
            break
    else:
        return CheckoutResult(gate_result=result)

    catalog_result = await session.execute(
        select(Product).where(Product.merchant_id == original_product.merchant_id)
    )
    catalog = catalog_result.scalars().all()

    def _to_candidate(p: Product) -> SubstitutionCandidate:
        return SubstitutionCandidate(
            product_id=p.id,
            sku=p.sku,
            name=p.name,
            category=p.category,
            category_class=p.category_class,
            unit_price_paise=p.unit_price_paise,
            stock=p.stock,
            return_window_days=p.return_window_days or 0,
            fulfilment_hours=p.fulfilment_hours or 0,
            restocking_cost_pct=p.restocking_cost_pct,
            is_personalised=p.is_personalised,
        )

    original_candidate = _to_candidate(original_product)
    all_candidates = [_to_candidate(p) for p in catalog]

    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == gate_req.envelope_id)
    )
    env_row = env_result.scalar_one()
    from praman.core.gate import envelope_from_row

    env = envelope_from_row(env_row)

    eligible = deterministic_filter(original_candidate, original_qty, all_candidates, env)
    ranked, rationale = await rank_candidates(llm, original_candidate, eligible)

    await append_event(
        session,
        gate_req.session_id,
        gate_req.agent_did,
        "SUBSTITUTION_OFFERED",
        {
            "out_of_stock_sku": out_of_stock_sku,
            "offered_skus": [c.sku for c in ranked],
            "rationale": rationale,
        },
    )

    return CheckoutResult(
        gate_result=result, substitution_offer=ranked, substitution_rationale=rationale
    )
