"""`cart_confirm` (CLAUDE.md §6). Builds and persists a `CartMandate` from a
set of already-issued, merchant-signed quotes. Requires the agent's
signature over the request (the same auth `checkout_execute` will require
again on the *next* call — `cart_confirm` doesn't run the money-path gate,
only R04's envelope pre-check, so an agent gets fast feedback on an
obviously-doomed cart before committing to a signed checkout call).

Every field that matters (category, category_class, is_personalised,
return_window_days, fulfilment_hours, restocking_cost_pct) is read from the
`Product` row the quote references, never trusted from the request body —
an agent can't declare its own item as more reversible than the merchant's
catalog says it is.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from praman.api.deps import DbSession, RedisDep, RegistryDep, SignatureHeadersDep
from praman.core.envelope import Cart, CartItem, verify_cart_within_envelope
from praman.core.gate import envelope_from_row
from praman.core.ledger import append_event
from praman.core.quotes import QuoteData, verify_quote
from praman.core.registry import verify_agent_request
from praman.core.reversibility import ReversibilityItem, band, reversibility_score_detailed
from praman.models import CartMandate, IntentEnvelope, Merchant, Product
from praman.schemas import CartConfirmIn, CartConfirmOut, QuoteIn

router = APIRouter(prefix="/api", tags=["cart"])


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


@router.post("/cart/confirm")
async def cart_confirm(
    request: Request,
    body: CartConfirmIn,
    session: DbSession,
    redis: RedisDep,
    registry: RegistryDep,
    sig: SignatureHeadersDep,
) -> CartConfirmOut:
    if not body.quotes:
        raise HTTPException(status_code=400, detail="cart must have at least one quote")

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

    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == body.envelope_id)
    )
    env_row = env_result.scalar_one_or_none()
    if env_row is None:
        raise HTTPException(status_code=404, detail="envelope not found")

    merchant_result = await session.execute(
        select(Merchant).where(Merchant.id == env_row.merchant_id)
    )
    merchant = merchant_result.scalar_one()

    cart_items: list[CartItem] = []
    reversibility_items: list[ReversibilityItem] = []
    for q in body.quotes:
        product_result = await session.execute(select(Product).where(Product.id == q.product_id))
        product = product_result.scalar_one_or_none()
        if product is None:
            raise HTTPException(status_code=404, detail=f"product {q.product_id} not found")

        quote_data = _quote_data_from_in(q)
        quote_check = verify_quote(
            quote_data,
            merchant.public_key,
            live_unit_price_paise=product.unit_price_paise,
            live_stock=product.stock,
            now=now,
        )
        if quote_check.decision != "ALLOW":
            raise HTTPException(
                status_code=409,
                detail={"reason_code": quote_check.reason_code, "detail": quote_check.detail},
            )

        cart_items.append(
            CartItem(
                sku=product.sku,
                category=product.category,
                qty=q.qty,
                unit_price_paise=q.unit_price_paise,
            )
        )
        reversibility_items.append(
            ReversibilityItem(
                category_class=product.category_class,
                is_personalised=product.is_personalised,
                return_window_days=product.return_window_days or 0,
                fulfilment_hours=product.fulfilment_hours or 0,
                restocking_cost_pct=product.restocking_cost_pct,
            )
        )

    cart = Cart(agent_did=body.agent_did, items=tuple(cart_items))
    env = envelope_from_row(env_row)
    envelope_check = verify_cart_within_envelope(cart, env, now)

    score, breakdown = reversibility_score_detailed(reversibility_items, cart.total_paise, env)
    cart_band = band(score)

    mandate = CartMandate(
        envelope_id=body.envelope_id,
        agent_did=body.agent_did,
        items=[
            {
                "sku": i.sku,
                "category": i.category,
                "qty": i.qty,
                "unit_price_paise": i.unit_price_paise,
            }
            for i in cart_items
        ],
        subtotal_paise=cart.total_paise,
        tax_paise=0,
        total_paise=cart.total_paise,
        reversibility_score=score,
        reversibility_breakdown=breakdown,
        band=cart_band,
        merchant_sig=None,
        agent_sig=sig.signature,
        created_at=now,
    )
    session.add(mandate)
    await session.commit()
    await session.refresh(mandate)

    await append_event(
        session,
        f"cart:{mandate.cart_id}",
        body.agent_did,
        "CART_CONFIRMED",
        {
            "cart_id": mandate.cart_id,
            "envelope_id": body.envelope_id,
            "total_paise": cart.total_paise,
            "band": cart_band,
            "envelope_check": envelope_check.decision,
        },
    )

    return CartConfirmOut(
        cart_id=mandate.cart_id,
        subtotal_paise=mandate.subtotal_paise,
        total_paise=mandate.total_paise,
        reversibility_score=score,
        reversibility_breakdown=breakdown,
        band=cart_band,
        envelope_check_decision=envelope_check.decision,
        envelope_check_reason_code=envelope_check.reason_code,
    )
