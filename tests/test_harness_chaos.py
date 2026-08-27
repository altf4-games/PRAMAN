"""`harness/chaos.py`'s two mutations are the entire stale-quote-race
attack: a quote gets issued at one price/stock, then the live row changes
underneath it before checkout runs. These just need to actually persist."""

from __future__ import annotations

from datetime import UTC, datetime

from praman.crypto import did as did_module
from praman.crypto.keys import generate_keypair
from praman.models import Merchant, Product
from sqlalchemy.ext.asyncio import AsyncSession

from harness.chaos import mutate_price, mutate_stock_to_zero


async def _make_product(session: AsyncSession) -> Product:
    _priv, pub = generate_keypair()
    merchant = Merchant(
        name="Chaos Test Merchant",
        did=did_module.did_from_public_key(pub),
        public_key=pub,
        private_key_enc="unused",
        whatsapp_number="whatsapp:+919999999999",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=datetime.now(UTC),
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)

    product = Product(
        merchant_id=merchant.id,
        sku="CHAOS-1",
        name="Chaos Product",
        category="grocery",
        category_class="consumable",
        unit_price_paise=10_000,
        stock=50,
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
    return product


async def test_mutate_price_persists_new_price(db_session: AsyncSession) -> None:
    product = await _make_product(db_session)
    await mutate_price(db_session, product, new_price_paise=1)
    assert product.unit_price_paise == 1


async def test_mutate_stock_to_zero_persists(db_session: AsyncSession) -> None:
    product = await _make_product(db_session)
    assert product.stock == 50
    await mutate_stock_to_zero(db_session, product)
    assert product.stock == 0
