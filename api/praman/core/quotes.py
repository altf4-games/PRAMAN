"""Quotes. A quote is a merchant-signed price/stock commitment an agent can
act on for a limited window — TTL scales with how fast the underlying
product actually turns over (a perishable's price is stale in 60s; a
bespoke item's isn't for 15 minutes). The soft stock hold in Redis shares
the quote's TTL so an expired quote's hold releases itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from redis.asyncio import Redis

from praman.config import (
    QUOTE_TTL_BESPOKE_DEMO_MODE_S,
    QUOTE_TTL_BESPOKE_S,
    QUOTE_TTL_DEMO_MODE_S,
    QUOTE_TTL_DURABLE_S,
    QUOTE_TTL_PERISHABLE_CONSUMABLE_S,
)
from praman.core.gate_types import GateResult, allow
from praman.crypto.canonical import canonicalize
from praman.crypto.keys import sign, verify

_PERISHABLE_CONSUMABLE_CLASSES = {"perishable", "consumable"}


def quote_ttl_seconds(category_class: str, *, demo_mode: bool = False) -> int:
    if demo_mode:
        # Bespoke is the exception: it's the class that drives R08
        # merchant-approval escalation, and a human needs real time to see
        # the message and tap Approve (MERCHANT_APPROVAL_TIMEOUT_S gives
        # them 15 minutes) -- flattening it to the same 30s as everything
        # else made the approval flow structurally unable to succeed.
        if category_class == "bespoke":
            return QUOTE_TTL_BESPOKE_DEMO_MODE_S
        return QUOTE_TTL_DEMO_MODE_S
    if category_class in _PERISHABLE_CONSUMABLE_CLASSES:
        return QUOTE_TTL_PERISHABLE_CONSUMABLE_S
    if category_class == "bespoke":
        return QUOTE_TTL_BESPOKE_S
    # durable, digital, service: not individually specified by the spec —
    # durable's TTL is a reasonable default for anything not perishable-fast
    # or bespoke-slow.
    return QUOTE_TTL_DURABLE_S


@dataclass(frozen=True, slots=True)
class QuoteData:
    quote_id: str
    product_id: str
    sku: str
    agent_did: str
    merchant_did: str
    unit_price_paise: int
    qty: int
    total_paise: int
    stock_held: bool
    issued_at: datetime
    expires_at: datetime
    nonce: str
    signature: str
    consumed_at: datetime | None = None


def _signing_payload(quote: QuoteData) -> dict[str, object]:
    return {
        "quote_id": quote.quote_id,
        "sku": quote.sku,
        "unit_price_paise": quote.unit_price_paise,
        "qty": quote.qty,
        "total_paise": quote.total_paise,
        "stock_held": quote.stock_held,
        "issued_at": quote.issued_at.isoformat(),
        "expires_at": quote.expires_at.isoformat(),
        "nonce": quote.nonce,
        "merchant_did": quote.merchant_did,
    }


def _stock_hold_key(product_id: str, quote_id: str) -> str:
    return f"stock_hold:{product_id}:{quote_id}"


async def hold_stock(redis: Redis, *, product_id: str, quote_id: str, qty: int, ttl_s: int) -> bool:
    """A *soft* hold: one Redis key per (product, quote) pair, expiring
    with the quote's own TTL. `held_stock_for_product` sums the currently
    live keys via SCAN — best-effort under concurrency, not a hard atomic
    reservation. Documented tradeoff: see ARCHITECTURE.md Phase 4."""
    await redis.set(_stock_hold_key(product_id, quote_id), qty, ex=ttl_s)
    return True


async def release_stock_hold(redis: Redis, *, product_id: str, quote_id: str) -> None:
    await redis.delete(_stock_hold_key(product_id, quote_id))


async def held_stock_for_product(redis: Redis, product_id: str) -> int:
    pattern = f"stock_hold:{product_id}:*"
    total = 0
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            values = await redis.mget(keys)
            total += sum(int(v) for v in values if v is not None)
        if cursor == 0:
            break
    return total


async def issue_quote(
    redis: Redis,
    *,
    product_id: str,
    sku: str,
    category_class: str,
    unit_price_paise: int,
    qty: int,
    agent_did: str,
    merchant_did: str,
    merchant_private_key_hex: str,
    now: datetime,
    demo_mode: bool = False,
) -> QuoteData:
    ttl_s = quote_ttl_seconds(category_class, demo_mode=demo_mode)
    quote_id = uuid.uuid4().hex
    nonce = uuid.uuid4().hex
    stock_held = await hold_stock(
        redis, product_id=product_id, quote_id=quote_id, qty=qty, ttl_s=ttl_s
    )

    quote = QuoteData(
        quote_id=quote_id,
        product_id=product_id,
        sku=sku,
        agent_did=agent_did,
        merchant_did=merchant_did,
        unit_price_paise=unit_price_paise,
        qty=qty,
        total_paise=unit_price_paise * qty,
        stock_held=stock_held,
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl_s),
        nonce=nonce,
        signature="",
    )
    signature = sign(merchant_private_key_hex, canonicalize(_signing_payload(quote)))
    return replace(quote, signature=signature)


def verify_quote(
    quote: QuoteData,
    merchant_public_key: str,
    *,
    live_unit_price_paise: int,
    live_stock: int | None,
    now: datetime,
) -> GateResult:
    """Checks, in order: signature, expiry, already-consumed, live price
    match, live stock availability. First failure wins."""
    if not verify(merchant_public_key, canonicalize(_signing_payload(quote)), quote.signature):
        return GateResult(
            decision="BLOCK",
            reason_code="QUOTE_SIG_INVALID",
            detail="quote signature did not verify against the merchant's public key.",
            remedy="Request a fresh quote; do not modify a signed quote's fields.",
            rule_id="R05",
        )

    if now > quote.expires_at:
        return GateResult(
            decision="BLOCK",
            reason_code="QUOTE_EXPIRED",
            detail=f"quote expired at {quote.expires_at.isoformat()}; now is {now.isoformat()}.",
            remedy="Request a fresh quote and retry.",
            rule_id="R05",
        )

    if quote.consumed_at is not None:
        return GateResult(
            decision="BLOCK",
            reason_code="QUOTE_EXPIRED",
            detail=f"quote was already consumed at {quote.consumed_at.isoformat()}.",
            remedy="Request a fresh quote — each quote is single-use.",
            rule_id="R05",
        )

    if live_unit_price_paise != quote.unit_price_paise:
        return GateResult(
            decision="BLOCK",
            reason_code="PRICE_DRIFT",
            detail=f"quoted price {quote.unit_price_paise} != live price {live_unit_price_paise}.",
            remedy="Request a fresh quote at the current price.",
            rule_id="R06",
        )

    if live_stock is not None and live_stock < quote.qty:
        return GateResult(
            decision="SUBSTITUTE",
            reason_code="OUT_OF_STOCK",
            detail=f"live stock {live_stock} is below the quoted quantity {quote.qty}.",
            remedy="Accept a substitution or reduce quantity.",
            rule_id="R07",
        )

    return allow(detail="quote is fresh, unconsumed, and matches live price/stock", rule_id="R05")
