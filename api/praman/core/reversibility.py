"""The Reversibility Ladder (CLAUDE.md §5): autonomy scales inversely with
how hard a purchase is to undo. `reversibility_score_detailed` is
deterministic, explainable, and pure — the same five weighted factors,
computed the same way, every time. It is never tuned after looking at
results; the harness's 60 hand labels (`harness/labels.json`) are what
"correct" is measured against, not the other way around.

HARD ZERO: any personalised item makes the whole cart irreversible,
full stop — an engraved ring can't un-engrave itself no matter how cheap
or how reversible the rest of the cart is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from praman.config import (
    BAND_AMBER_THRESHOLD,
    BAND_GREEN_THRESHOLD,
    CATEGORY_CLASS_SCORES,
    FULFILMENT_NORMALISATION_HOURS,
    RESTOCKING_COST_NORMALISATION_PCT,
    RETURN_WINDOW_NORMALISATION_DAYS,
    REVERSIBILITY_WEIGHT_CLASS,
    REVERSIBILITY_WEIGHT_RESTOCK,
    REVERSIBILITY_WEIGHT_RETURN,
    REVERSIBILITY_WEIGHT_SPEED,
    REVERSIBILITY_WEIGHT_VALUE,
)
from praman.core.envelope import Envelope

Band = Literal["green", "amber", "red"]


@dataclass(frozen=True, slots=True)
class ReversibilityItem:
    category_class: str
    is_personalised: bool
    return_window_days: int
    fulfilment_hours: int
    restocking_cost_pct: float


def band(score: float) -> Band:
    if score >= BAND_GREEN_THRESHOLD:
        return "green"
    if score >= BAND_AMBER_THRESHOLD:
        return "amber"
    return "red"


def reversibility_score_detailed(
    items: list[ReversibilityItem], total_paise: int, env: Envelope
) -> tuple[float, dict[str, float]]:
    """Returns (score, per_factor_breakdown). Multi-item carts take the
    MINIMUM per factor across items — a cart is only as reversible as its
    least reversible item. `f_value` is the one cart-level exception: it
    depends on the cart's total against the envelope's ceiling, not on any
    single item.
    """
    if not items:
        # No items to be irreversible about. A degenerate case in
        # practice (the gate never scores an empty cart), but a defined,
        # maximally-reversible default beats a crash on min() of nothing.
        breakdown = {
            "f_return": 1.0,
            "f_class": 1.0,
            "f_speed": 1.0,
            "f_restock": 1.0,
            "f_value": 1.0,
            "hard_zero": False,
        }
        return 1.0, breakdown

    if any(item.is_personalised for item in items):
        breakdown = {
            "f_return": 0.0,
            "f_class": 0.0,
            "f_speed": 0.0,
            "f_restock": 0.0,
            "f_value": 0.0,
            "hard_zero": True,
        }
        return 0.0, breakdown

    f_return = min(item.return_window_days / RETURN_WINDOW_NORMALISATION_DAYS for item in items)
    f_return = min(f_return, 1.0)

    f_class = min(CATEGORY_CLASS_SCORES[item.category_class] for item in items)

    f_speed = min(
        1 - min(item.fulfilment_hours / FULFILMENT_NORMALISATION_HOURS, 1.0) for item in items
    )

    f_restock = min(
        1 - min(item.restocking_cost_pct / RESTOCKING_COST_NORMALISATION_PCT, 1.0) for item in items
    )

    f_value = 1 - min(total_paise / env.ceiling_paise, 1.0) if env.ceiling_paise > 0 else 0.0

    score = (
        REVERSIBILITY_WEIGHT_RETURN * f_return
        + REVERSIBILITY_WEIGHT_CLASS * f_class
        + REVERSIBILITY_WEIGHT_SPEED * f_speed
        + REVERSIBILITY_WEIGHT_RESTOCK * f_restock
        + REVERSIBILITY_WEIGHT_VALUE * f_value
    )

    breakdown = {
        "f_return": f_return,
        "f_class": f_class,
        "f_speed": f_speed,
        "f_restock": f_restock,
        "f_value": f_value,
        "hard_zero": False,
    }
    return score, breakdown
