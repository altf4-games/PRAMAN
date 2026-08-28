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


# f_unwind: the "returnless refund" branch (perishable/consumable/digital) —
# value-taper against UNWIND_FREE_CEILING_PAISE, independent of
# return_window_days entirely.


def test_f_unwind_near_one_for_a_cheap_unwind_free_item() -> None:
    item = _item(category_class="consumable", return_window_days=0)
    _score, breakdown = reversibility_score_detailed([item], 18_000, _envelope())  # ₹180
    assert breakdown["f_unwind"] == pytest.approx(0.82, abs=0.01)


def test_f_unwind_zero_for_an_unwind_free_item_at_or_above_the_ceiling() -> None:
    item = _item(category_class="perishable", return_window_days=0)
    _score, breakdown = reversibility_score_detailed([item], 100_000, _envelope(1_000_000))
    assert breakdown["f_unwind"] == 0.0


def test_f_unwind_ignores_return_window_for_unwind_free_categories() -> None:
    # A zero-day return window shouldn't matter at all here -- the whole
    # point of the returnless-refund branch is that a formal return never
    # happens for these categories at this value.
    zero_window = _item(category_class="consumable", return_window_days=0)
    long_window = _item(category_class="consumable", return_window_days=365)
    _s1, b1 = reversibility_score_detailed([zero_window], 5_000, _envelope())
    _s2, b2 = reversibility_score_detailed([long_window], 5_000, _envelope())
    assert b1["f_unwind"] == b2["f_unwind"]


# f_unwind: the traditional return-window branch (durable/service/bespoke).


def test_f_unwind_capped_at_one_for_long_return_windows_on_a_durable_item() -> None:
    item = _item(category_class="durable", return_window_days=365)
    _score, breakdown = reversibility_score_detailed([item], 1000, _envelope())
    assert breakdown["f_unwind"] == 1.0


def test_f_unwind_zero_for_zero_day_return_window_on_a_durable_item() -> None:
    item = _item(category_class="durable", return_window_days=0)
    _score, breakdown = reversibility_score_detailed([item], 1000, _envelope())
    assert breakdown["f_unwind"] == 0.0


def test_f_unwind_high_value_does_not_help_a_durable_item_with_a_short_window() -> None:
    # Unlike the unwind-free branch, value doesn't buy leniency here --
    # a durable good's reversibility genuinely depends on its return
    # window, not on being cheap.
    item = _item(category_class="durable", return_window_days=2)
    _score, breakdown = reversibility_score_detailed([item], 100, _envelope(1_000_000))
    assert breakdown["f_unwind"] == pytest.approx(2 / 14)


def test_f_unwind_one_non_unwind_free_item_routes_the_whole_cart_to_the_return_window_branch() -> (
    None
):
    cheap_consumable = _item(category_class="consumable", return_window_days=0)
    durable = _item(category_class="durable", return_window_days=7)
    _score, breakdown = reversibility_score_detailed(
        [cheap_consumable, durable], 5_000, _envelope()
    )
    # Not the near-1.0 the consumable alone would get under the
    # unwind-free branch -- the durable item's presence means the whole
    # cart falls back to return-window semantics, MIN'd across items.
    assert breakdown["f_unwind"] == pytest.approx(min(0 / 14, 7 / 14))


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
    assert breakdown["f_unwind"] < 1.0
    assert score < 1.0


# --- Full weighted computation, worked by hand ---


def test_known_worked_example_matches_hand_calculation() -> None:
    # consumable (unwind-free-eligible), 24h fulfilment, no restocking
    # cost, cart is ₹100 (10% of the unwind-free ceiling) and 10% of the
    # envelope ceiling.
    item = _item(
        category_class="consumable",
        return_window_days=14,
        fulfilment_hours=24,
        restocking_cost_pct=0.0,
    )
    score, breakdown = reversibility_score_detailed([item], 10_000, _envelope(100_000))

    expected_f_unwind = 1 - 10_000 / 100_000  # UNWIND_FREE_CEILING_PAISE
    expected_f_speed = 1 - 24 / 336
    expected_score = (
        0.35 * expected_f_unwind + 0.25 * 0.90 + 0.15 * expected_f_speed + 0.10 * 1.0 + 0.15 * 0.9
    )
    assert score == pytest.approx(expected_score, rel=1e-9)
    assert breakdown["f_unwind"] == pytest.approx(expected_f_unwind)
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
