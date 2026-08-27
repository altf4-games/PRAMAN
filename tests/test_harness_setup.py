"""`harness/setup.py` seeds the one merchant + both catalogs every harness
session runs against — a bug here would silently corrupt every category's
results, not just the ones exercising it directly."""

from __future__ import annotations

from praman.adapters.llm import FakeLLMClient
from praman.adapters.razorpay_client import FakeRazorpayClient
from praman.models import Product
from praman.whatsapp.client import FakeWhatsAppClient
from sqlalchemy.ext.asyncio import AsyncSession

from harness.setup import HarnessAdapters, load_catalog, seed_merchant_and_catalog


def test_load_catalog_grocery_and_jewellery_are_nonempty() -> None:
    grocery = load_catalog("grocery")
    jewellery = load_catalog("jewellery")
    assert len(grocery) == 40
    assert len(jewellery) == 40
    for item in (*grocery, *jewellery):
        assert item["sku"]
        assert item["unit_price_paise"] > 0


async def test_seed_merchant_and_catalog_creates_one_row_per_sku(
    db_session: AsyncSession,
) -> None:
    grocery = load_catalog("grocery")
    jewellery = load_catalog("jewellery")

    merchant, priv, products = await seed_merchant_and_catalog(db_session)

    assert merchant.onboarding_state == "LIVE"
    assert isinstance(priv, str) and priv
    assert len(products) == len(grocery) + len(jewellery)
    for item in (*grocery, *jewellery):
        product = products[item["sku"]]
        assert isinstance(product, Product)
        assert product.merchant_id == merchant.id
        assert product.unit_price_paise == item["unit_price_paise"]


async def test_seed_merchant_and_catalog_defaults_null_stock_to_500(
    db_session: AsyncSession,
) -> None:
    _merchant, _priv, products = await seed_merchant_and_catalog(db_session)
    for item in load_catalog("grocery") + load_catalog("jewellery"):
        if item["stock"] is None:
            assert products[item["sku"]].stock == 500


def test_harness_adapters_are_fakes() -> None:
    adapters = HarnessAdapters()
    assert isinstance(adapters.razorpay, FakeRazorpayClient)
    assert isinstance(adapters.whatsapp, FakeWhatsAppClient)
    assert isinstance(adapters.llm, FakeLLMClient)
