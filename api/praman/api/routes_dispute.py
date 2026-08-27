"""`GET /api/dispute-pack/{cart_id}` — a Phase 8 preview, pulled forward
because `/dispute/[orderId]` needs real content to render rather than a
stub. Assembles envelope, cart mandate, gate trail, order (if any), and
the ordered ledger trail with its chain-verification result — the same
`session_id` convention (`cart:{cart_id}`) every Phase 6 route already
appends events under, so this needs no new bookkeeping.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select

from praman.api.deps import DbSession
from praman.config import get_settings
from praman.core.ledger import dispute_pack_events
from praman.crypto.canonical import canonical_hash
from praman.crypto.keys import decrypt_private_key, sign
from praman.models import CartMandate, GateDecision, IntentEnvelope, Merchant, Order, Product, Quote
from praman.schemas import DisputePackOut
from praman.timeutil import as_aware_utc

router = APIRouter(prefix="/api", tags=["dispute"])
logger = logging.getLogger(__name__)


async def _assemble(session: DbSession, cart_id: str) -> DisputePackOut:
    cart_result = await session.execute(select(CartMandate).where(CartMandate.cart_id == cart_id))
    cart = cart_result.scalar_one_or_none()
    if cart is None:
        raise HTTPException(status_code=404, detail="cart not found")

    env_result = await session.execute(
        select(IntentEnvelope).where(IntentEnvelope.envelope_id == cart.envelope_id)
    )
    env = env_result.scalar_one_or_none()

    order_result = await session.execute(select(Order).where(Order.cart_id == cart_id))
    order = order_result.scalar_one_or_none()

    gate_result = await session.execute(
        select(GateDecision)
        .where(GateDecision.cart_id == cart_id)
        .order_by(GateDecision.evaluated_at)
    )
    gate_trail: list[dict[str, object]] = [
        {
            "decision": g.decision,
            "reason_code": g.reason_code,
            "rule_id": g.rule_id,
            "detail": g.detail,
            "remedy": g.remedy,
            "evaluated_at": as_aware_utc(g.evaluated_at).isoformat(),
            "latency_ms": g.latency_ms,
        }
        for g in gate_result.scalars().all()
    ]

    # --- quote provenance: every Quote row issued for this cart's agent and
    # any of its SKUs, up to and including the cart's creation time. There's
    # no direct FK from CartMandate to the quote(s) it was built from (the
    # signed quote lives in Redis with a short TTL and is consumed, not
    # persisted, at checkout time) -- this is a best-effort reconstruction
    # from the DB rows `POST /api/quotes` does persist, documented as such.
    cart_skus = {item["sku"] for item in cart.items if "sku" in item}
    quote_provenance: list[dict[str, object]] = []
    if cart_skus:
        product_result = await session.execute(
            select(Product.id, Product.sku).where(Product.sku.in_(cart_skus))
        )
        product_id_by_sku: dict[str, str] = {sku: pid for pid, sku in product_result.all()}
        if product_id_by_sku:
            quote_result = await session.execute(
                select(Quote)
                .where(
                    Quote.product_id.in_(product_id_by_sku.values()),
                    Quote.agent_did == cart.agent_did,
                    Quote.issued_at <= cart.created_at,
                )
                .order_by(Quote.issued_at)
            )
            sku_by_product_id = {v: k for k, v in product_id_by_sku.items()}
            quote_provenance = [
                {
                    "quote_id": q.quote_id,
                    "sku": sku_by_product_id.get(q.product_id),
                    "unit_price_paise": q.unit_price_paise,
                    "qty": q.qty,
                    "total_paise": q.total_paise,
                    "stock_held": q.stock_held,
                    "issued_at": as_aware_utc(q.issued_at).isoformat(),
                    "expires_at": as_aware_utc(q.expires_at).isoformat(),
                    "nonce": q.nonce,
                    "signature": q.signature,
                }
                for q in quote_result.scalars().all()
            ]

    ledger = await dispute_pack_events(session, f"cart:{cart_id}")
    ledger_events = ledger.get("events", [])
    latest_chain_hash = ledger_events[-1]["chain_hash"] if ledger_events else None

    envelope_out: dict[str, object] = (
        {
            "envelope_id": env.envelope_id,
            "merchant_id": env.merchant_id,
            "agent_did": env.agent_did,
            "ceiling_paise": env.ceiling_paise,
            "spent_paise": env.spent_paise,
            "max_single_txn_paise": env.max_single_txn_paise,
            "allowed_categories": env.allowed_categories,
            "min_reversibility": env.min_reversibility,
            "valid_from": as_aware_utc(env.valid_from).isoformat(),
            "valid_until": as_aware_utc(env.valid_until).isoformat(),
            "signature": env.signature,
        }
        if env is not None
        else {}
    )

    order_out: dict[str, object] | None = (
        {
            "id": order.id,
            "status": order.status,
            "razorpay_order_id": order.razorpay_order_id,
            "cooling_off_until": as_aware_utc(order.cooling_off_until).isoformat()
            if order.cooling_off_until
            else None,
            "dispatched_at": as_aware_utc(order.dispatched_at).isoformat()
            if order.dispatched_at
            else None,
            "cancelled_at": as_aware_utc(order.cancelled_at).isoformat()
            if order.cancelled_at
            else None,
            "refunded_at": as_aware_utc(order.refunded_at).isoformat()
            if order.refunded_at
            else None,
            "stepup_token": order.stepup_token,
            "stepup_confirmed_at": as_aware_utc(order.stepup_confirmed_at).isoformat()
            if order.stepup_confirmed_at
            else None,
            "stepup_channel": order.stepup_channel,
        }
        if order is not None
        else None
    )

    cart_mandate_out = {
        "cart_id": cart.cart_id,
        "agent_did": cart.agent_did,
        "items": cart.items,
        "subtotal_paise": cart.subtotal_paise,
        "total_paise": cart.total_paise,
        "agent_sig": cart.agent_sig,
        "created_at": as_aware_utc(cart.created_at).isoformat(),
    }

    # --- merchant-key signature over the pack's own facts. Signed after
    # everything above is assembled, over a canonical (JCS) hash of the
    # exact fields a dispute reader would check -- envelope, cart mandate,
    # gate trail, quote provenance, and the ledger's own chain hash --
    # so the signature is verifiable proof this specific pack came from
    # this merchant's key, not just a "trust the API" claim.
    merchant_did: str | None = None
    merchant_signature: str | None = None
    signable = {
        "cart_id": cart.cart_id,
        "envelope": envelope_out,
        "cart_mandate": cart_mandate_out,
        "order": order_out,
        "gate_trail": gate_trail,
        "quote_provenance": quote_provenance,
        "reversibility_breakdown": cart.reversibility_breakdown,
        "band": cart.band,
        "chain_hash": latest_chain_hash,
    }
    pack_hash = canonical_hash(signable)

    if env is not None:
        merchant_row = (
            await session.execute(select(Merchant).where(Merchant.id == env.merchant_id))
        ).scalar_one_or_none()
        if merchant_row is not None:
            merchant_did = merchant_row.did
            settings = get_settings()
            try:
                merchant_priv = decrypt_private_key(
                    merchant_row.private_key_enc, settings.app_secret
                )
                merchant_signature = sign(merchant_priv, bytes.fromhex(pack_hash))
            except Exception:
                # A dispute pack should still be readable even if signing
                # fails for some reason (e.g. a harness-seeded merchant
                # whose private_key_enc is a placeholder, not real
                # Fernet ciphertext) -- absence of a signature is visible
                # in the output, not a 500.
                logger.warning(
                    "dispute_pack: merchant-key signing failed for cart %s", cart_id, exc_info=True
                )
                merchant_signature = None

    return DisputePackOut(
        cart_id=cart.cart_id,
        envelope=envelope_out,
        cart_mandate=cart_mandate_out,
        order=order_out,
        gate_trail=gate_trail,
        quote_provenance=quote_provenance,
        reversibility_breakdown=cart.reversibility_breakdown,
        band=cart.band,
        ledger=ledger,
        merchant_did=merchant_did,
        pack_hash=pack_hash,
        merchant_signature=merchant_signature,
    )


@router.get("/dispute-pack/{cart_id}")
async def dispute_pack(session: DbSession, cart_id: str) -> DisputePackOut:
    return await _assemble(session, cart_id)


@router.get("/dispute-pack/{cart_id}/pdf")
async def dispute_pack_pdf(session: DbSession, cart_id: str) -> Response:
    pack = await _assemble(session, cart_id)
    # Imported lazily -- WeasyPrint pulls in pango/gdk-pixbuf FFI bindings
    # at import time, which needn't be paid by every request that never
    # touches this route.
    from weasyprint import HTML

    from praman.pdf.dispute_pack_template import render_dispute_pack_html

    html_doc = render_dispute_pack_html(pack)
    pdf_bytes = HTML(string=html_doc).write_pdf()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="dispute-pack-{cart_id}.pdf"'},
    )
