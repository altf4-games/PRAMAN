"""Harness fixture setup — one merchant, both seed catalogs (80 SKUs) as
real `Product` rows, one Fake* adapter set. Every harness session runs
against this same seeded state; `run.py` builds it once per run.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from praman.adapters.llm import FakeLLMClient
from praman.adapters.razorpay_client import FakeRazorpayClient
from praman.crypto import did as did_module
from praman.crypto.keys import generate_keypair
from praman.models import Merchant, Product
from praman.whatsapp.client import FakeWhatsAppClient
from sqlalchemy.ext.asyncio import AsyncSession

SEED_DIR = Path(__file__).resolve().parent.parent / "api" / "praman" / "seed"


def load_catalog(name: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = json.loads((SEED_DIR / f"catalog_{name}.json").read_text())
    return result


async def seed_merchant_and_catalog(
    session: AsyncSession,
) -> tuple[Merchant, str, dict[str, Product]]:
    """Returns (merchant, {sku: Product}) — every SKU from both seed
    catalogs, real rows, real keypair. `stock` defaults to a large number
    where the seed data left it null (a real vendor's actual stock count
    was never captured for these SKUs), so a harness session's quoted
    quantity always clears R07 unless a chaos mutation deliberately drops
    it — matching how the real onboarding flow behaves for the same data."""
    priv, pub = generate_keypair()
    merchant = Merchant(
        name="Harness Merchant",
        did=did_module.did_from_public_key(pub),
        public_key=pub,
        private_key_enc="unused-in-harness",
        whatsapp_number=f"whatsapp:+91{uuid.uuid4().int % 10**10:010d}",
        onboarding_state="LIVE",
        agent_policy={},
        created_at=datetime.now(UTC),
    )
    session.add(merchant)
    await session.commit()
    await session.refresh(merchant)

    products: dict[str, Product] = {}
    for catalog_name in ("grocery", "jewellery"):
        for item in load_catalog(catalog_name):
            product = Product(
                merchant_id=merchant.id,
                sku=item["sku"],
                name=item["name"],
                category=item["category"],
                category_class=item["category_class"],
                unit_price_paise=item["unit_price_paise"],
                stock=item["stock"] if item["stock"] is not None else 500,
                return_window_days=item["return_window_days"],
                fulfilment_hours=item["fulfilment_hours"],
                restocking_cost_pct=item["restocking_cost_pct"],
                is_personalised=item["is_personalised"],
                field_confidence=item["field_confidence"],
                needs_review=False,
                source=item["source"],
            )
            session.add(product)
            products[item["sku"]] = product
    await session.commit()
    for p in products.values():
        await session.refresh(p)

    return merchant, priv, products


class HarnessAdapters:
    """One fake adapter set, reused across every session in a run — cheap
    to construct, and Fake* is exactly what every automated test in this
    repo already exercises (see README's 'what's real vs mocked')."""

    def __init__(self) -> None:
        self.razorpay = FakeRazorpayClient()
        self.whatsapp = FakeWhatsAppClient()
        self.llm = FakeLLMClient()
