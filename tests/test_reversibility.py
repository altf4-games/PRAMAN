from __future__ import annotations

from datetime import UTC, datetime

import pytest
from praman.core.envelope import Envelope
from praman.core.reversibility import ReversibilityItem, band, reversibility_score_detailed

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _envelope(ceiling_paise: int = 100_000) -> Envelope:
    return Envelope(
        agent_did="did:key:zAgent",
        revoked_at=None,
        valid_from=NOW,
        valid_until=NOW,
        allowed_categories=("groceries",),
        max_single_txn_paise=ceiling_paise,
        ceiling_paise=ceiling_paise,
        spent_paise=0,
    )


def _item(**overrides: object) -> ReversibilityItem:
    defaults: dict[str, object] = {
        "category_class": "consumable",
        "is_personalised": False,
        "return_window_days": 14,
        "fulfilment_hours": 24,
        "restocking_cost_pct": 0.0,
    }
    defaults.update(overrides)
    return ReversibilityItem(**defaults)  # type: ignore[arg-type]


# --- band() thresholds ---


def test_band_green_at_exact_threshold() -> None:
    assert band(0.75) == "green"


def test_band_amber_just_below_green_threshold() -> None:
    assert band(0.7499) == "amber"


def test_band_amber_at_exact_threshold() -> None:
    assert band(0.40) == "amber"


def test_band_red_just_below_amber_threshold() -> None:
    assert band(0.3999) == "red"


def test_band_red_at_zero() -> None:
    assert band(0.0) == "red"


def test_band_green_at_one() -> None:
    assert band(1.0) == "green"


# --- Hard zero ---


def test_personalised_item_is_hard_zero_regardless_of_everything_else() -> None:
    # every other factor maxed out — still zero.
    item = _item(
        is_personalised=True,
        category_class="perishable",
        return_window_days=14,
        fulfilment_hours=1,
        restocking_cost_pct=0.0,
    )
    score, breakdown = reversibility_score_detailed([item], 1, _envelope(1_000_000))
    assert score == 0.0
    assert band(score) == "red"
    assert breakdown["hard_zero"] is True


def test_one_personalised_item_among_many_still_zeroes_the_whole_cart() -> None:
    clean = _item(category_class="perishable", return_window_days=14, fulfilment_hours=1)
    personalised = _item(is_personalised=True)
    score, _breakdown = reversibility_score_detailed([clean, personalised], 5000, _envelope())
    assert score == 0.0


# --- Empty cart ---


def test_empty_cart_scores_maximally_reversible() -> None:
    score, breakdown = reversibility_score_detailed([], 0, _envelope())
    assert score == 1.0
    assert band(score) == "green"
    assert breakdown["hard_zero"] is False


# --- Per-factor computation ---


def test_f_return_capped_at_one_for_long_return_windows() -> None:
    item = _item(return_window_days=365)
    _score, breakdown = reversibility_score_detailed([item], 1000, _envelope())
    assert breakdown["f_return"] == 1.0


def test_f_return_zero_for_zero_day_return_window() -> None:
    item = _item(return_window_days=0)
    _score, breakdown = reversibility_score_detailed([item], 1000, _envelope())
    assert breakdown["f_return"] == 0.0


def test_f_class_uses_category_class_lookup() -> None:
    perishable = _item(category_class="perishable")
    bespoke_but_not_personalised = _item(category_class="bespoke")
    _s1, b1 = reversibility_score_detailed([perishable], 1000, _envelope())
    _s2, b2 = reversibility_score_detailed([bespoke_but_not_personalised], 1000, _envelope())
    assert b1["f_class"] == pytest.approx(0.95)
    assert b2["f_class"] == pytest.approx(0.05)


def test_f_speed_higher_for_faster_fulfilment() -> None:
    fast = _item(fulfilment_hours=2)
    slow = _item(fulfilment_hours=336)
    _s1, b1 = reversibility_score_detailed([fast], 1000, _envelope())
    _s2, b2 = reversibility_score_detailed([slow], 1000, _envelope())
    assert b1["f_speed"] > b2["f_speed"]
    assert b2["f_speed"] == 0.0


def test_f_speed_capped_for_fulfilment_beyond_normalisation() -> None:
    item = _item(fulfilment_hours=1000)
    _score, breakdown = reversibility_score_detailed([item], 1000, _envelope())
    assert breakdown["f_speed"] == 0.0


def test_f_restock_higher_for_lower_restocking_cost() -> None:
    cheap = _item(restocking_cost_pct=0.0)
    expensive = _item(restocking_cost_pct=0.30)
    _s1, b1 = reversibility_score_detailed([cheap], 1000, _envelope())
    _s2, b2 = reversibility_score_detailed([expensive], 1000, _envelope())
    assert b1["f_restock"] == 1.0
    assert b2["f_restock"] == 0.0


def test_f_value_is_cart_level_not_per_item() -> None:
    # two items, but f_value depends only on cart total vs envelope ceiling
    item_a = _item()
    item_b = _item()
    _score, breakdown = reversibility_score_detailed(
        [item_a, item_b], total_paise=50_000, env=_envelope(ceiling_paise=100_000)
    )
    assert breakdown["f_value"] == pytest.approx(0.5)


def test_f_value_zero_when_cart_total_meets_or_exceeds_ceiling() -> None:
    item = _item()
    _score, breakdown = reversibility_score_detailed([item], 100_000, _envelope(100_000))
    assert breakdown["f_value"] == 0.0


# --- Multi-item MIN semantics ---


def test_multi_item_cart_takes_minimum_per_factor() -> None:
    reversible = _item(category_class="perishable", return_window_days=14, fulfilment_hours=1)
    less_reversible = _item(category_class="durable", return_window_days=1, fulfilment_hours=300)
    score, breakdown = reversibility_score_detailed(
        [reversible, less_reversible], 1000, _envelope()
    )
    # every per-item factor must reflect the LESS reversible item
    assert breakdown["f_class"] == pytest.approx(0.55)  # durable, not perishable's 0.95
    assert breakdown["f_return"] < 1.0
    assert score < 1.0


# --- Full weighted computation, worked by hand ---


def test_known_worked_example_matches_hand_calculation() -> None:
    # consumable, 14-day return, 24h fulfilment, no restocking cost,
    # cart is 10% of envelope ceiling.
    item = _item(
        category_class="consumable",
        return_window_days=14,
        fulfilment_hours=24,
        restocking_cost_pct=0.0,
    )
    score, breakdown = reversibility_score_detailed([item], 10_000, _envelope(100_000))

    expected_f_speed = 1 - 24 / 336
    expected_score = 0.35 * 1.0 + 0.25 * 0.90 + 0.15 * expected_f_speed + 0.10 * 1.0 + 0.15 * 0.9
    assert score == pytest.approx(expected_score, rel=1e-9)
    assert breakdown["f_speed"] == pytest.approx(expected_f_speed)
    assert band(score) == "green"


def test_bespoke_high_value_item_lands_in_red() -> None:
    item = _item(
        category_class="bespoke",
        return_window_days=0,
        fulfilment_hours=250,
        restocking_cost_pct=0.30,
    )
    score, _breakdown = reversibility_score_detailed([item], 80_000, _envelope(85_000))
    assert band(score) == "red"


def test_moderate_durable_item_lands_in_amber() -> None:
    item = _item(
        category_class="durable",
        return_window_days=10,
        fulfilment_hours=48,
        restocking_cost_pct=0.10,
    )
    score, _breakdown = reversibility_score_detailed([item], 40_000, _envelope(85_000))
    assert band(score) == "amber"
