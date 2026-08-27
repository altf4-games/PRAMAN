"""Acceptance (the design spec's Phase 4): >=18 unit tests including exact
boundaries (total == ceiling, == ceiling+1, envelope expiring mid-request,
empty cart, duplicate SKUs), plus a hypothesis property test: no passing
cart can ever push spent above ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st
from praman.core.envelope import Cart, CartItem, Envelope, verify_cart_within_envelope

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _envelope(**overrides: object) -> Envelope:
    defaults: dict[str, object] = {
        "agent_did": "did:key:zAgent",
        "revoked_at": None,
        "valid_from": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(hours=1),
        "allowed_categories": ("groceries", "pulses"),
        "max_single_txn_paise": 100_000,
        "ceiling_paise": 500_000,
        "spent_paise": 0,
    }
    defaults.update(overrides)
    return Envelope(**defaults)  # type: ignore[arg-type]


def _cart(*items: CartItem, agent_did: str = "did:key:zAgent") -> Cart:
    return Cart(agent_did=agent_did, items=tuple(items))


def _item(price: int, qty: int = 1, category: str = "groceries", sku: str = "sku-1") -> CartItem:
    return CartItem(sku=sku, category=category, qty=qty, unit_price_paise=price)


# --- Happy path ---


def test_allow_when_well_within_envelope() -> None:
    cart = _cart(_item(1000, qty=2))
    result = verify_cart_within_envelope(cart, _envelope(), NOW)
    assert result.decision == "ALLOW"
    assert result.reason_code == "OK"


def test_allow_with_multiple_items_across_allowed_categories() -> None:
    cart = _cart(_item(1000, category="groceries"), _item(2000, category="pulses"))
    result = verify_cart_within_envelope(cart, _envelope(), NOW)
    assert result.decision == "ALLOW"


# --- R04 sub-check 1: revoked ---


def test_block_when_envelope_revoked() -> None:
    env = _envelope(revoked_at=NOW - timedelta(minutes=1))
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "ENVELOPE_REVOKED"


def test_revoked_check_wins_over_also_expired_envelope() -> None:
    # ordering: revoked is checked before expiry — both true, revoked wins
    env = _envelope(
        revoked_at=NOW - timedelta(minutes=1),
        valid_from=NOW - timedelta(days=2),
        valid_until=NOW - timedelta(days=1),
    )
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.reason_code == "ENVELOPE_REVOKED"


# --- R04 sub-check 2: expiry, including exact boundaries ---


def test_allow_at_exact_valid_from_boundary() -> None:
    env = _envelope(valid_from=NOW, valid_until=NOW + timedelta(hours=1))
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "ALLOW"


def test_allow_at_exact_valid_until_boundary() -> None:
    env = _envelope(valid_from=NOW - timedelta(hours=1), valid_until=NOW)
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "ALLOW"


def test_block_one_microsecond_past_valid_until() -> None:
    env = _envelope(
        valid_from=NOW - timedelta(hours=1), valid_until=NOW - timedelta(microseconds=1)
    )
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "ENVELOPE_EXPIRED"


def test_block_before_valid_from() -> None:
    env = _envelope(valid_from=NOW + timedelta(seconds=1), valid_until=NOW + timedelta(hours=1))
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "ENVELOPE_EXPIRED"


def test_envelope_expiring_mid_request() -> None:
    # 'now' injected as a parameter is exactly what makes this testable:
    # the envelope was valid a second ago and is not valid now.
    env = _envelope(valid_from=NOW - timedelta(hours=1), valid_until=NOW - timedelta(seconds=1))
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "ENVELOPE_EXPIRED"


# --- R04 sub-check 3: agent mismatch ---


def test_block_on_agent_mismatch() -> None:
    cart = _cart(_item(1000), agent_did="did:key:zOtherAgent")
    result = verify_cart_within_envelope(cart, _envelope(), NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "AGENT_MISMATCH"


# --- R04 sub-check 4: category ---


def test_block_on_disallowed_category() -> None:
    cart = _cart(_item(1000, category="electronics"))
    result = verify_cart_within_envelope(cart, _envelope(), NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "CATEGORY_DENIED"


def test_block_when_only_one_of_several_items_is_disallowed() -> None:
    cart = _cart(_item(1000, category="groceries"), _item(500, category="electronics"))
    result = verify_cart_within_envelope(cart, _envelope(), NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "CATEGORY_DENIED"


# --- R04 sub-check 5: single-txn limit, exact boundaries ---


def test_allow_at_exact_single_txn_boundary() -> None:
    env = _envelope(max_single_txn_paise=1000)
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "ALLOW"


def test_block_one_paisa_over_single_txn_boundary() -> None:
    env = _envelope(max_single_txn_paise=1000)
    result = verify_cart_within_envelope(_cart(_item(1001)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "SINGLE_TXN_EXCEEDED"


# --- R04 sub-check 6: ceiling, exact boundaries ---


def test_allow_at_exact_ceiling_boundary() -> None:
    env = _envelope(ceiling_paise=1000, max_single_txn_paise=1000, spent_paise=0)
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "ALLOW"


def test_block_one_paisa_over_ceiling_boundary() -> None:
    env = _envelope(ceiling_paise=999, max_single_txn_paise=1000, spent_paise=0)
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "ENVELOPE_CEILING_EXCEEDED"


def test_block_when_prior_spend_plus_cart_exceeds_ceiling() -> None:
    env = _envelope(ceiling_paise=1000, max_single_txn_paise=1000, spent_paise=500)
    result = verify_cart_within_envelope(_cart(_item(501)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code == "ENVELOPE_CEILING_EXCEEDED"


def test_allow_when_prior_spend_plus_cart_exactly_equals_ceiling() -> None:
    env = _envelope(ceiling_paise=1000, max_single_txn_paise=1000, spent_paise=500)
    result = verify_cart_within_envelope(_cart(_item(500)), env, NOW)
    assert result.decision == "ALLOW"


# --- Edge cases explicitly called out by the spec ---


def test_empty_cart_is_allowed() -> None:
    result = verify_cart_within_envelope(_cart(), _envelope(), NOW)
    assert result.decision == "ALLOW"


def test_duplicate_skus_sum_correctly_toward_ceiling() -> None:
    cart = _cart(_item(300, qty=1, sku="sku-1"), _item(300, qty=1, sku="sku-1"))
    env = _envelope(ceiling_paise=600, max_single_txn_paise=1000, spent_paise=0)
    result = verify_cart_within_envelope(cart, env, NOW)
    assert result.decision == "ALLOW"

    env_tight = _envelope(ceiling_paise=599, max_single_txn_paise=1000, spent_paise=0)
    result_tight = verify_cart_within_envelope(cart, env_tight, NOW)
    assert result_tight.decision == "BLOCK"
    assert result_tight.reason_code == "ENVELOPE_CEILING_EXCEEDED"


def test_ordering_agent_mismatch_checked_before_category() -> None:
    # both agent mismatch AND category-denied are true; agent mismatch
    # (checked third) must win over category (checked fourth).
    cart = _cart(_item(1000, category="electronics"), agent_did="did:key:zOtherAgent")
    result = verify_cart_within_envelope(cart, _envelope(), NOW)
    assert result.reason_code == "AGENT_MISMATCH"


def test_ordering_category_checked_before_single_txn_limit() -> None:
    # both category-denied AND single-txn-exceeded are true; category
    # (checked fourth) must win over single-txn (checked fifth).
    env = _envelope(max_single_txn_paise=100)
    cart = _cart(_item(1000, category="electronics"))
    result = verify_cart_within_envelope(cart, env, NOW)
    assert result.reason_code == "CATEGORY_DENIED"


def test_every_block_result_carries_reason_detail_and_remedy() -> None:
    env = _envelope(revoked_at=NOW)
    result = verify_cart_within_envelope(_cart(_item(1000)), env, NOW)
    assert result.decision == "BLOCK"
    assert result.reason_code
    assert result.detail
    assert result.remedy


# --- Property test: no passing cart can ever push spent above ceiling ---


@given(
    ceiling=st.integers(min_value=0, max_value=10_000_000),
    spent=st.integers(min_value=0, max_value=10_000_000),
    price=st.integers(min_value=0, max_value=1_000_000),
    qty=st.integers(min_value=1, max_value=100),
    max_single_txn=st.integers(min_value=0, max_value=10_000_000),
)
def test_property_no_allowed_cart_pushes_spent_above_ceiling(
    ceiling: int, spent: int, price: int, qty: int, max_single_txn: int
) -> None:
    env = _envelope(ceiling_paise=ceiling, spent_paise=spent, max_single_txn_paise=max_single_txn)
    cart = _cart(_item(price, qty=qty))
    result = verify_cart_within_envelope(cart, env, NOW)
    if result.decision == "ALLOW":
        assert env.spent_paise + cart.total_paise <= env.ceiling_paise
