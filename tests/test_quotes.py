from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from praman.config import (
    QUOTE_TTL_BESPOKE_S,
    QUOTE_TTL_DEMO_MODE_S,
    QUOTE_TTL_DURABLE_S,
    QUOTE_TTL_PERISHABLE_CONSUMABLE_S,
)
from praman.core.quotes import (
    held_stock_for_product,
    hold_stock,
    issue_quote,
    quote_ttl_seconds,
    release_stock_hold,
    verify_quote,
)
from praman.crypto.keys import generate_keypair

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# --- quote_ttl_seconds ---


def test_ttl_perishable_and_consumable() -> None:
    assert quote_ttl_seconds("perishable") == QUOTE_TTL_PERISHABLE_CONSUMABLE_S
    assert quote_ttl_seconds("consumable") == QUOTE_TTL_PERISHABLE_CONSUMABLE_S


def test_ttl_bespoke() -> None:
    assert quote_ttl_seconds("bespoke") == QUOTE_TTL_BESPOKE_S


def test_ttl_durable() -> None:
    assert quote_ttl_seconds("durable") == QUOTE_TTL_DURABLE_S


def test_ttl_demo_mode_overrides_everything() -> None:
    assert quote_ttl_seconds("bespoke", demo_mode=True) == QUOTE_TTL_DEMO_MODE_S
    assert quote_ttl_seconds("perishable", demo_mode=True) == QUOTE_TTL_DEMO_MODE_S


# --- stock holds ---


async def test_hold_and_sum_stock_for_product(redis: fakeredis.aioredis.FakeRedis) -> None:
    await hold_stock(redis, product_id="p1", quote_id="q1", qty=3, ttl_s=60)
    await hold_stock(redis, product_id="p1", quote_id="q2", qty=5, ttl_s=60)
    await hold_stock(redis, product_id="p2", quote_id="q3", qty=100, ttl_s=60)

    assert await held_stock_for_product(redis, "p1") == 8
    assert await held_stock_for_product(redis, "p2") == 100
    assert await held_stock_for_product(redis, "p3") == 0


async def test_release_stock_hold_removes_it(redis: fakeredis.aioredis.FakeRedis) -> None:
    await hold_stock(redis, product_id="p1", quote_id="q1", qty=3, ttl_s=60)
    await release_stock_hold(redis, product_id="p1", quote_id="q1")
    assert await held_stock_for_product(redis, "p1") == 0


# --- issue_quote / verify_quote ---


async def _issued_quote(
    redis: fakeredis.aioredis.FakeRedis, priv: str, pub: str, **overrides: object
):
    defaults: dict[str, object] = {
        "product_id": "p1",
        "sku": "toor-dal-1kg",
        "category_class": "consumable",
        "unit_price_paise": 18000,
        "qty": 2,
        "agent_did": "did:key:zAgent",
        "merchant_did": "did:key:zMerchant",
        "merchant_private_key_hex": priv,
        "now": NOW,
    }
    defaults.update(overrides)
    quote = await issue_quote(redis, **defaults)  # type: ignore[arg-type]
    return quote


async def test_issue_quote_produces_expected_fields(redis: fakeredis.aioredis.FakeRedis) -> None:
    priv, _pub = generate_keypair()
    quote = await _issued_quote(redis, priv, _pub)
    assert quote.total_paise == 36000
    assert quote.stock_held is True
    assert quote.expires_at == NOW + timedelta(seconds=QUOTE_TTL_PERISHABLE_CONSUMABLE_S)
    assert quote.signature


async def test_verify_quote_allows_fresh_matching_quote(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    result = verify_quote(
        quote, pub, live_unit_price_paise=quote.unit_price_paise, live_stock=10, now=NOW
    )
    assert result.decision == "ALLOW"


async def test_verify_quote_blocks_invalid_signature(redis: fakeredis.aioredis.FakeRedis) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    tampered = replace(quote, unit_price_paise=quote.unit_price_paise + 1)
    result = verify_quote(
        tampered, pub, live_unit_price_paise=tampered.unit_price_paise, live_stock=10, now=NOW
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "QUOTE_SIG_INVALID"


async def test_verify_quote_blocks_expired_quote(redis: fakeredis.aioredis.FakeRedis) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    after_expiry = quote.expires_at + timedelta(seconds=1)
    result = verify_quote(
        quote,
        pub,
        live_unit_price_paise=quote.unit_price_paise,
        live_stock=10,
        now=after_expiry,
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "QUOTE_EXPIRED"


async def test_verify_quote_allows_at_exact_expiry_boundary(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    result = verify_quote(
        quote,
        pub,
        live_unit_price_paise=quote.unit_price_paise,
        live_stock=10,
        now=quote.expires_at,
    )
    assert result.decision == "ALLOW"


async def test_verify_quote_blocks_already_consumed(redis: fakeredis.aioredis.FakeRedis) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    consumed = replace(quote, consumed_at=NOW)
    result = verify_quote(
        consumed, pub, live_unit_price_paise=quote.unit_price_paise, live_stock=10, now=NOW
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "QUOTE_EXPIRED"


async def test_verify_quote_blocks_price_drift(redis: fakeredis.aioredis.FakeRedis) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    result = verify_quote(
        quote, pub, live_unit_price_paise=quote.unit_price_paise + 100, live_stock=10, now=NOW
    )
    assert result.decision == "BLOCK"
    assert result.reason_code == "PRICE_DRIFT"


async def test_verify_quote_substitutes_on_insufficient_stock(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub, qty=5)
    result = verify_quote(
        quote, pub, live_unit_price_paise=quote.unit_price_paise, live_stock=1, now=NOW
    )
    assert result.decision == "SUBSTITUTE"
    assert result.reason_code == "OUT_OF_STOCK"


async def test_verify_quote_allows_when_stock_unknown(redis: fakeredis.aioredis.FakeRedis) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    result = verify_quote(
        quote, pub, live_unit_price_paise=quote.unit_price_paise, live_stock=None, now=NOW
    )
    assert result.decision == "ALLOW"


async def test_verify_quote_ordering_expired_checked_before_price_drift(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    priv, pub = generate_keypair()
    quote = await _issued_quote(redis, priv, pub)
    after_expiry = quote.expires_at + timedelta(seconds=1)
    result = verify_quote(
        quote,
        pub,
        live_unit_price_paise=quote.unit_price_paise + 999,  # also wrong
        live_stock=10,
        now=after_expiry,
    )
    assert result.reason_code == "QUOTE_EXPIRED"
