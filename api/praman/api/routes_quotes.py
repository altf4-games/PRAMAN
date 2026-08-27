"""`quote_request` (CLAUDE.md §6, `idempotentHint: true`). Requires the
requesting agent's Ed25519 signature (R01/R02 territory, via
`verify_agent_request`) since issuing a quote commits the merchant to a
soft stock hold — an unauthenticated caller shouldn't be able to churn a
merchant's stock holds for free. Not part of the money path itself, so this
runs the auth check directly rather than the full R01-R12 gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from praman.api.deps import DbSession, RedisDep, RegistryDep, SignatureHeadersDep
from praman.config import get_settings
from praman.core.quotes import issue_quote
from praman.core.registry import verify_agent_request
from praman.crypto.keys import decrypt_private_key
from praman.models import Merchant, Product, Quote
from praman.schemas import QuoteOut, QuoteRequestIn

router = APIRouter(prefix="/api", tags=["quotes"])


@router.post("/quotes")
async def quote_request(
    request: Request,
    body: QuoteRequestIn,
    session: DbSession,
    redis: RedisDep,
    registry: RegistryDep,
    sig: SignatureHeadersDep,
) -> QuoteOut:
    raw_body = await request.body()
    settings = get_settings()
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

    product_result = await session.execute(select(Product).where(Product.id == body.product_id))
    product = product_result.scalar_one_or_none()
    if product is None or product.needs_review:
        raise HTTPException(status_code=404, detail="product not found")

    merchant_result = await session.execute(
        select(Merchant).where(Merchant.id == product.merchant_id)
    )
    merchant = merchant_result.scalar_one()

    merchant_private_key_hex = decrypt_private_key(merchant.private_key_enc, settings.app_secret)
    quote = await issue_quote(
        redis,
        product_id=product.id,
        sku=product.sku,
        category_class=product.category_class,
        unit_price_paise=product.unit_price_paise,
        qty=body.qty,
        agent_did=body.agent_did,
        merchant_did=merchant.did,
        merchant_private_key_hex=merchant_private_key_hex,
        now=now,
        demo_mode=settings.demo_mode,
    )

    session.add(
        Quote(
            quote_id=quote.quote_id,
            product_id=quote.product_id,
            agent_did=quote.agent_did,
            unit_price_paise=quote.unit_price_paise,
            qty=quote.qty,
            total_paise=quote.total_paise,
            stock_held=quote.stock_held,
            issued_at=quote.issued_at,
            expires_at=quote.expires_at,
            nonce=quote.nonce,
            consumed_at=None,
            signature=quote.signature,
        )
    )
    await session.commit()

    return QuoteOut(
        quote_id=quote.quote_id,
        product_id=quote.product_id,
        sku=quote.sku,
        category=product.category,
        agent_did=quote.agent_did,
        merchant_did=quote.merchant_did,
        unit_price_paise=quote.unit_price_paise,
        qty=quote.qty,
        total_paise=quote.total_paise,
        stock_held=quote.stock_held,
        issued_at=quote.issued_at.isoformat(),
        expires_at=quote.expires_at.isoformat(),
        nonce=quote.nonce,
        signature=quote.signature,
    )
