"""Chaos mutations — change a product's live price/stock in the database
*after* a quote was issued against the old values, simulating the real
race a shopping agent can hit: it quoted at one price, the merchant's
price changed (or stock sold out) before checkout actually ran. R06/R07
exist specifically to catch this; Arm A (no gate) never checks and
captures the stale quote regardless.
"""

from __future__ import annotations

from praman.models import Product
from sqlalchemy.ext.asyncio import AsyncSession


async def mutate_price(session: AsyncSession, product: Product, *, new_price_paise: int) -> None:
    product.unit_price_paise = new_price_paise
    session.add(product)
    await session.commit()


async def mutate_stock_to_zero(session: AsyncSession, product: Product) -> None:
    product.stock = 0
    session.add(product)
    await session.commit()
