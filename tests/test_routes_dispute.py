"""Tests for `GET /api/dispute-pack/{cart_id}` and its `/pdf` sibling —
quote provenance, step-up fields, and merchant-key signing are all new
Phase 8 additions on top of the envelope/cart-mandate/gate-trail/ledger
fields the route already covered."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from praman.api.routes_dispute import dispute_pack, dispute_pack_pdf
from praman.config import get_settings
from praman.core.ledger import append_event
from praman.crypto import did as did_module
from praman.crypto.keys import encrypt_private_key, generate_keypair, verify
from praman.models import (
    CartMandate,
    GateDecision,
    IntentEnvelope,
    Merchant,
    Order,
    Product,
    Quote,
)
from praman.pdf.dispute_pack_template import render_dispute_pack_html
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)


async def _seed_full_cart(session: AsyncSession, **order_overrides: object) -> tuple[Merchant, str]:
    """Builds one merchant, product, quote, envelope, cart mandate, gate
    trail, order, and a two-event ledger trail — everything the dispute
    pack tries to assemble. Returns (merchant, cart_id)."""
    priv, pub = generate_keypair()
    settings = get_settings()
    merchant = Merchant(
        name="Dispute Test Merchant",
        did=did_module.did_from_public_key(pub),
        public_key=pub,
        private_key_enc=encrypt_private_key(priv, settings.app_secret),
        whatsapp_number="whatsapp:+919999999999",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=NOW,
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)

    product = Product(
        merchant_id=merchant.id,
        sku="DISPUTE-SKU-1",
        name="Dispute Test Product",
        category="grocery",
        category_class="consumable",
        unit_price_paise=5000,
        stock=100,
        return_window_days=2,
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

    quote = Quote(
        product_id=product.id,
        agent_did="did:key:zAgentDispute",
        unit_price_paise=5000,
        qty=2,
        total_paise=10000,
        stock_held=True,
        issued_at=NOW,
        expires_at=NOW,
        nonce="dispute-test-nonce",
        consumed_at=None,
        signature="quote-sig",
    )
    session.add(quote)

    envelope = IntentEnvelope(
        user_ref="dispute-test-user",
        user_whatsapp="whatsapp:+919000000001",
        merchant_id=merchant.id,
        agent_did="did:key:zAgentDispute",
        ceiling_paise=100_000,
        spent_paise=0,
        max_single_txn_paise=100_000,
        allowed_categories=["grocery"],
        min_reversibility=0.0,
        valid_from=NOW,
        valid_until=NOW,
        revoked_at=None,
        signature="envelope-sig",
    )
    session.add(envelope)
    await session.commit()
    await session.refresh(envelope)

    cart = CartMandate(
        envelope_id=envelope.envelope_id,
        agent_did="did:key:zAgentDispute",
        items=[
            {"sku": product.sku, "category": product.category, "qty": 2, "unit_price_paise": 5000}
        ],
        subtotal_paise=10000,
        tax_paise=0,
        total_paise=10000,
        reversibility_score=0.8,
        reversibility_breakdown={"f_unwind": 0.14, "f_class": 0.9},
        band="green",
        agent_sig="cart-sig",
        created_at=NOW,
    )
    session.add(cart)
    await session.commit()
    await session.refresh(cart)

    session.add(
        GateDecision(
            session_id=f"cart:{cart.cart_id}",
            cart_id=cart.cart_id,
            decision="ALLOW",
            reason_code="OK",
            rule_id=None,
            detail="all rules passed",
            remedy="",
            evaluated_at=NOW,
            latency_ms=1.2,
        )
    )

    order_defaults: dict[str, object] = {
        "cart_id": cart.cart_id,
        "razorpay_order_id": "order_test123",
        "razorpay_payment_id": "pay_test123",
        "status": "captured",
        "idempotency_key": f"idem-{cart.cart_id}",
        "created_at": NOW,
    }
    order_defaults.update(order_overrides)
    session.add(Order(**order_defaults))  # type: ignore[arg-type]
    await session.commit()

    await append_event(
        session, f"cart:{cart.cart_id}", "did:key:zAgentDispute", "ORDER_CREATED", {}
    )
    await append_event(
        session,
        f"cart:{cart.cart_id}",
        "did:key:zAgentDispute",
        "ORDER_CAPTURED",
        {"amount": 10000},
    )

    return merchant, cart.cart_id


async def test_missing_cart_raises_404(db_session: AsyncSession) -> None:
    try:
        await dispute_pack(db_session, "does-not-exist")
        raise AssertionError("expected HTTPException")
    except HTTPException as exc:
        assert exc.status_code == 404


async def test_dispute_pack_includes_quote_provenance(db_session: AsyncSession) -> None:
    _merchant, cart_id = await _seed_full_cart(db_session)
    pack = await dispute_pack(db_session, cart_id)

    assert len(pack.quote_provenance) == 1
    q = pack.quote_provenance[0]
    assert q["sku"] == "DISPUTE-SKU-1"
    assert q["unit_price_paise"] == 5000
    assert q["signature"] == "quote-sig"


async def test_dispute_pack_includes_stepup_fields(db_session: AsyncSession) -> None:
    _merchant, cart_id = await _seed_full_cart(
        db_session,
        stepup_token="stepup-abc",
        stepup_confirmed_at=NOW,
        stepup_channel="whatsapp",
    )
    pack = await dispute_pack(db_session, cart_id)

    assert pack.order is not None
    assert pack.order["stepup_token"] == "stepup-abc"
    assert pack.order["stepup_channel"] == "whatsapp"
    assert pack.order["stepup_confirmed_at"] is not None


async def test_dispute_pack_is_signed_with_a_verifiable_merchant_signature(
    db_session: AsyncSession,
) -> None:
    merchant, cart_id = await _seed_full_cart(db_session)
    pack = await dispute_pack(db_session, cart_id)

    assert pack.merchant_did == merchant.did
    assert pack.merchant_signature is not None
    assert verify(merchant.public_key, bytes.fromhex(pack.pack_hash), pack.merchant_signature)


async def test_dispute_pack_signature_rejects_a_tampered_hash(db_session: AsyncSession) -> None:
    """A signature that verifies against the real `pack_hash` must not also
    verify against a tampered one -- the whole point of signing the pack's
    own facts is that a modified pack no longer matches its signature."""
    merchant, cart_id = await _seed_full_cart(db_session)
    pack = await dispute_pack(db_session, cart_id)

    tampered_hash = pack.pack_hash[:-1] + ("0" if pack.pack_hash[-1] != "0" else "1")
    assert not verify(
        merchant.public_key, bytes.fromhex(tampered_hash), pack.merchant_signature or ""
    )


async def test_dispute_pack_pdf_returns_pdf_bytes(db_session: AsyncSession) -> None:
    _merchant, cart_id = await _seed_full_cart(db_session)
    response = await dispute_pack_pdf(db_session, cart_id)

    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")
    assert len(response.body) > 500


async def test_render_dispute_pack_html_contains_key_fields(db_session: AsyncSession) -> None:
    _merchant, cart_id = await _seed_full_cart(db_session)
    pack = await dispute_pack(db_session, cart_id)
    doc = render_dispute_pack_html(pack)

    assert cart_id in doc
    assert "DISPUTE-SKU-1" in doc
    assert "GREEN" in doc.upper()
    assert pack.pack_hash in doc
