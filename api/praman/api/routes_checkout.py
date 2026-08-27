"""`checkout_execute` — the sole money-path route (`destructiveHint: true`
in the MCP table), plus `substitution_accept`, `order_status`, and
`order_undo`. `checkout_execute` is the only place `core/gate.py::run_gate`
is invoked from an inbound HTTP request; everything upstream of it
(`cart_confirm`) only ever pre-checks, never dispatches payment.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from praman.adapters.razorpay_client import make_idempotency_key
from praman.api.deps import (
    DbSession,
    LLMDep,
    RazorpayDep,
    RedisDep,
    RegistryDep,
    SignatureHeadersDep,
    WhatsAppDep,
)
from praman.config import get_settings
from praman.core.checkout import cancel_order, confirm_real_payment, execute_checkout
from praman.core.envelope import Cart, CartItem
from praman.core.gate import GateRequest
from praman.core.quotes import QuoteData
from praman.core.registry import verify_agent_request
from praman.core.reversibility import ReversibilityItem
from praman.models import CartMandate, IntentEnvelope, Order, Product
from praman.schemas import (
    CheckoutConfirmIn,
    CheckoutConfirmOut,
    CheckoutExecuteIn,
    CheckoutExecuteOut,
    OrderOut,
    OrderUndoIn,
    OrderUndoOut,
    QuoteIn,
    SubstitutionAcceptIn,
    SubstitutionCandidateOut,
)

router = APIRouter(prefix="/api", tags=["checkout"])


def _quote_data_from_in(q: QuoteIn) -> QuoteData:
    return QuoteData(
        quote_id=q.quote_id,
        product_id=q.product_id,
        sku=q.sku,
        agent_did=q.agent_did,
        merchant_did=q.merchant_did,
        unit_price_paise=q.unit_price_paise,
        qty=q.qty,
        total_paise=q.total_paise,
        stock_held=q.stock_held,
        issued_at=datetime.fromisoformat(q.issued_at),
        expires_at=datetime.fromisoformat(q.expires_at),
        nonce=q.nonce,
        signature=q.signature,
        consumed_at=None,
    )


async def _load_cart(session: DbSession, cart_id: str) -> CartMandate:
    result = await session.execute(select(CartMandate).where(CartMandate.cart_id == cart_id))
    cart = result.scalar_one_or_none()
    if cart is None:
        raise HTTPException(status_code=404, detail="cart not found")
    return cart


async def _build_gate_request(
    session: DbSession,
    cart: CartMandate,
    *,
    agent_did: str,
    quotes_in: list[QuoteIn],
    method: str,
    raw_body: bytes,
    timestamp: str,
    nonce: str,
    signature: str,
    now: datetime,
) -> GateRequest:
    cart_items = tuple(
        CartItem(
            sku=i["sku"],
            category=i["category"],
            qty=i["qty"],
            unit_price_paise=i["unit_price_paise"],
        )
        for i in cart.items
    )
    reversibility_items = []
    for q in quotes_in:
        product_result = await session.execute(select(Product).where(Product.id == q.product_id))
        product = product_result.scalar_one_or_none()
        if product is None:
            raise HTTPException(status_code=404, detail=f"product {q.product_id} not found")
        reversibility_items.append(
            ReversibilityItem(
                category_class=product.category_class,
                is_personalised=product.is_personalised,
                return_window_days=product.return_window_days or 0,
                fulfilment_hours=product.fulfilment_hours or 0,
                restocking_cost_pct=product.restocking_cost_pct,
            )
        )

    return GateRequest(
        session_id=f"cart:{cart.cart_id}",
        cart_id=cart.cart_id,
        agent_did=agent_did,
        method=method,
        body=raw_body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        envelope_id=cart.envelope_id,
        cart=Cart(agent_did=agent_did, items=cart_items),
        reversibility_items=tuple(reversibility_items),
        quotes=tuple(_quote_data_from_in(q) for q in quotes_in),
        idempotency_key=make_idempotency_key(cart.cart_id, agent_did),
        now=now,
    )


@router.post("/checkout/execute")
async def checkout_execute(
    request: Request,
    body: CheckoutExecuteIn,
    session: DbSession,
    redis: RedisDep,
    registry: RegistryDep,
    razorpay: RazorpayDep,
    whatsapp: WhatsAppDep,
    llm: LLMDep,
    sig: SignatureHeadersDep,
) -> CheckoutExecuteOut:
    raw_body = await request.body()
    now = datetime.now(UTC)
    cart = await _load_cart(session, body.cart_id)

    gate_req = await _build_gate_request(
        session,
        cart,
        agent_did=body.agent_did,
        quotes_in=body.quotes,
        method="POST",
        raw_body=raw_body,
        timestamp=sig.timestamp,
        nonce=sig.nonce,
        signature=sig.signature,
        now=now,
    )

    settings = get_settings()
    result = await execute_checkout(
        session,
        redis,
        registry,
        razorpay,
        whatsapp,
        llm,
        cart,
        gate_req,
        demo_mode=settings.demo_mode,
    )

    return CheckoutExecuteOut(
        decision=result.gate_result.decision,
        reason_code=result.gate_result.reason_code,
        detail=result.gate_result.detail,
        remedy=result.gate_result.remedy,
        order_id=result.order.id if result.order else None,
        order_status=result.order.status if result.order else None,
        substitution_offer=[
            SubstitutionCandidateOut(
                product_id=c.product_id, sku=c.sku, name=c.name, unit_price_paise=c.unit_price_paise
            )
            for c in result.substitution_offer
        ],
        substitution_rationale=result.substitution_rationale,
    )


@router.post("/checkout/substitute")
async def substitution_accept(
    request: Request,
    body: SubstitutionAcceptIn,
    session: DbSession,
    redis: RedisDep,
    registry: RegistryDep,
    sig: SignatureHeadersDep,
) -> dict[str, str]:
    """Accepting a substitute doesn't itself capture payment — it just
    re-points the cart at the accepted product and asks the caller to
    request a fresh quote for it, then call `checkout_execute` again (a
    substitute is a *different* product with its own price/stock, so it
    needs its own signed quote, same as any other item would)."""
    raw_body = await request.body()
    now = datetime.now(UTC)
    auth_result = await verify_agent_request(
        registry,
        redis,
        agent_did=body.agent_did,
        method="POST",
        body=raw_body,
        timestamp=sig.timestamp,
        nonce=sig.nonce,
        signature=sig.signature,
        now=now,
    )
    if auth_result.decision != "ALLOW":
        raise HTTPException(
            status_code=403,
            detail={"reason_code": auth_result.reason_code, "detail": auth_result.detail},
        )

    cart = await _load_cart(session, body.cart_id)
    product_result = await session.execute(
        select(Product).where(Product.id == body.accepted_product_id)
    )
    if product_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="accepted product not found")

    return {
        "cart_id": cart.cart_id,
        "accepted_product_id": body.accepted_product_id,
        "next_step": "request a fresh quote for the accepted product, then call checkout_execute",
    }


@router.get("/orders/{order_id}")
async def order_status(session: DbSession, order_id: str) -> OrderOut:
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    amount_paise: int | None = None
    razorpay_key_id: str | None = None
    if order.status in ("awaiting_payment", "awaiting_payment_amber"):
        cart_result = await session.execute(
            select(CartMandate).where(CartMandate.cart_id == order.cart_id)
        )
        cart = cart_result.scalar_one_or_none()
        amount_paise = cart.total_paise if cart is not None else None
        razorpay_key_id = get_settings().razorpay_key_id

    return OrderOut(
        id=order.id,
        cart_id=order.cart_id,
        status=order.status,
        razorpay_order_id=order.razorpay_order_id,
        cooling_off_until=order.cooling_off_until.isoformat() if order.cooling_off_until else None,
        dispatched_at=order.dispatched_at.isoformat() if order.dispatched_at else None,
        cancelled_at=order.cancelled_at.isoformat() if order.cancelled_at else None,
        refunded_at=order.refunded_at.isoformat() if order.refunded_at else None,
        amount_paise=amount_paise,
        razorpay_key_id=razorpay_key_id,
    )


@router.post("/checkout/{order_id}/confirm")
async def checkout_confirm(
    session: DbSession,
    order_id: str,
    body: CheckoutConfirmIn,
    razorpay: RazorpayDep,
    whatsapp: WhatsAppDep,
) -> CheckoutConfirmOut:
    """Completes a real Razorpay Checkout.js round-trip for an order left
    `awaiting_payment(_amber)` by `checkout_execute` (see
    `core/checkout.py::confirm_real_payment` and
    `adapters/razorpay_client.py::create_and_capture_order`'s docstrings
    for why this second step exists at all — S2S test-card capture isn't
    enabled on this Razorpay test account, so a genuine payment needs a
    browser to actually drive it).
    """
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    if order.razorpay_order_id != body.razorpay_order_id:
        raise HTTPException(status_code=400, detail="razorpay_order_id does not match this order")

    settings = get_settings()
    try:
        confirmed = await confirm_real_payment(
            session,
            razorpay,
            whatsapp,
            order,
            razorpay_payment_id=body.razorpay_payment_id,
            razorpay_signature=body.razorpay_signature,
            now=datetime.now(UTC),
            demo_mode=settings.demo_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return CheckoutConfirmOut(
        order_id=confirmed.id,
        status=confirmed.status,
        dispatched_at=confirmed.dispatched_at.isoformat() if confirmed.dispatched_at else None,
        cooling_off_until=confirmed.cooling_off_until.isoformat()
        if confirmed.cooling_off_until
        else None,
    )


@router.post("/orders/{order_id}/undo")
async def order_undo(
    session: DbSession, order_id: str, body: OrderUndoIn, razorpay: RazorpayDep
) -> OrderUndoOut:
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")

    cart_result = await session.execute(
        select(CartMandate).where(CartMandate.cart_id == order.cart_id)
    )
    cart = cart_result.scalar_one_or_none()
    if cart is None:
        raise HTTPException(status_code=404, detail="cart not found for this order")

    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == cart.envelope_id)
    )
    env_row = env_result.scalar_one_or_none()
    if env_row is None or env_row.user_ref != body.user_ref:
        raise HTTPException(status_code=403, detail="user_ref does not match this order's buyer")

    cancelled = await cancel_order(
        session,
        razorpay,
        order,
        amount_paise=cart.total_paise,
        now=datetime.now(UTC),
        reason="buyer_rest_undo",
    )
    return OrderUndoOut(cancelled=cancelled)
