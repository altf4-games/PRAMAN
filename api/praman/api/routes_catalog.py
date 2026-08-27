"""Read-only catalog + policy routes — `catalog_search`, `catalog_get`,
`policy_get` in the MCP tool table (CLAUDE.md §6), all `readOnlyHint: true`.
No agent signature required: this is the publicly agent-readable part of a
storefront, by design — an agent needs to be able to browse before it has
any reason to authenticate.

A product with `needs_review=True` is never returned here, at either route
— CLAUDE.md §2's anti-hallucination gate at the data layer ("never exposed
to agents until confirmed") applies exactly as much to a direct `catalog_get`
by id as it does to `catalog_search`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from praman.api.deps import DbSession
from praman.models import Merchant, Product
from praman.schemas import PolicyOut, ProductOut

router = APIRouter(prefix="/api", tags=["catalog"])


def _to_product_out(product: Product) -> ProductOut:
    return ProductOut(
        id=product.id,
        sku=product.sku,
        name=product.name,
        category=product.category,
        category_class=product.category_class,
        unit_price_paise=product.unit_price_paise,
        stock=product.stock,
        return_window_days=product.return_window_days,
        fulfilment_hours=product.fulfilment_hours,
        is_personalised=product.is_personalised,
    )


@router.get("/catalog/search")
async def catalog_search(
    session: DbSession,
    merchant_id: str,
    category: str | None = None,
    q: str | None = None,
) -> list[ProductOut]:
    stmt = select(Product).where(
        Product.merchant_id == merchant_id, Product.needs_review.is_(False)
    )
    if category is not None:
        stmt = stmt.where(Product.category == category)
    if q is not None:
        stmt = stmt.where(Product.name.ilike(f"%{q}%"))
    result = await session.execute(stmt.order_by(Product.name))
    return [_to_product_out(p) for p in result.scalars().all()]


@router.get("/catalog/{product_id}")
async def catalog_get(session: DbSession, product_id: str) -> ProductOut:
    result = await session.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if product is None or product.needs_review:
        raise HTTPException(status_code=404, detail="product not found")
    return _to_product_out(product)


@router.get("/policy/{merchant_id}")
async def policy_get(session: DbSession, merchant_id: str) -> PolicyOut:
    result = await session.execute(select(Merchant).where(Merchant.id == merchant_id))
    merchant = result.scalar_one_or_none()
    if merchant is None:
        raise HTTPException(status_code=404, detail="merchant not found")
    policy = merchant.agent_policy or {}
    return PolicyOut(
        merchant_id=merchant.id,
        name=merchant.name,
        did=merchant.did,
        max_txn_paise=policy.get("max_txn_paise"),
        cooling_off_hold=policy.get("cooling_off_hold"),
    )
