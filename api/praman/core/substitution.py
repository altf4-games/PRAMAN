"""Substitution, on R07 (out of stock). Two steps, in order:

1. A deterministic filter — same category, price within the envelope's
   remaining headroom, `return_window_days` no worse, reversibility band
   no worse, stock actually available. This alone decides what's even
   eligible; nothing past this point can introduce a candidate the filter
   rejected.
2. The LLM ranks the *filtered* set and writes one rationale line. Any LLM
   failure (bad response, timeout, provider error) falls back to
   cheapest-first — CLAUDE.md §0: the LLM is never load-bearing. It can
   only reorder an already-safe candidate list, never expand it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from praman.adapters.llm import LLMClient
from praman.core.envelope import Envelope
from praman.core.reversibility import ReversibilityItem, band, reversibility_score_detailed

logger = logging.getLogger(__name__)

_BAND_RANK = {"red": 0, "amber": 1, "green": 2}


@dataclass(frozen=True, slots=True)
class SubstitutionCandidate:
    product_id: str
    sku: str
    name: str
    category: str
    category_class: str
    unit_price_paise: int
    stock: int | None
    return_window_days: int
    fulfilment_hours: int
    restocking_cost_pct: float
    is_personalised: bool


def _item_band(candidate: SubstitutionCandidate, total_paise: int, env: Envelope) -> str:
    item = ReversibilityItem(
        category_class=candidate.category_class,
        is_personalised=candidate.is_personalised,
        return_window_days=candidate.return_window_days,
        fulfilment_hours=candidate.fulfilment_hours,
        restocking_cost_pct=candidate.restocking_cost_pct,
    )
    score, _breakdown = reversibility_score_detailed([item], total_paise, env)
    return band(score)


def deterministic_filter(
    original: SubstitutionCandidate,
    qty: int,
    candidates: list[SubstitutionCandidate],
    env: Envelope,
) -> list[SubstitutionCandidate]:
    """Every rule here is a hard requirement — a candidate that fails any
    one of them is not eligible, full stop; there is no "close enough"."""
    original_total = original.unit_price_paise * qty
    original_band = _item_band(original, original_total, env)
    headroom_paise = env.ceiling_paise - env.spent_paise

    eligible: list[SubstitutionCandidate] = []
    for candidate in candidates:
        if candidate.product_id == original.product_id:
            continue
        if candidate.category != original.category:
            continue
        if candidate.stock is None or candidate.stock < qty:
            continue
        candidate_total = candidate.unit_price_paise * qty
        if candidate_total > headroom_paise:
            continue
        if candidate.return_window_days < original.return_window_days:
            continue
        if _BAND_RANK[_item_band(candidate, candidate_total, env)] < _BAND_RANK[original_band]:
            continue
        eligible.append(candidate)
    return eligible


_RANKING_PROMPT = """\
A customer's original item is out of stock. Rank these substitute products
from best to worst replacement, considering how closely each matches the
original in name, purpose, and value. Return ONLY JSON:
{{"ranked_skus": ["sku-in-best-first-order", ...], "rationale": "one sentence explaining the top pick"}}

Original item: {original_name} ({original_category})

Candidates:
{candidates_block}
"""


def _cheapest_first(candidates: list[SubstitutionCandidate]) -> list[SubstitutionCandidate]:
    return sorted(candidates, key=lambda c: c.unit_price_paise)


async def rank_candidates(
    llm: LLMClient,
    original: SubstitutionCandidate,
    candidates: list[SubstitutionCandidate],
) -> tuple[list[SubstitutionCandidate], str]:
    if not candidates:
        return [], "No eligible substitutes found."

    candidates_block = "\n".join(
        f"- {c.sku}: {c.name}, ₹{c.unit_price_paise / 100:.2f}" for c in candidates
    )
    prompt = _RANKING_PROMPT.format(
        original_name=original.name,
        original_category=original.category,
        candidates_block=candidates_block,
    )

    try:
        raw = await llm.generate_json(prompt)
        parsed = json.loads(raw)
        ranked_skus = parsed["ranked_skus"]
        rationale = str(parsed.get("rationale", ""))
        order = {sku: i for i, sku in enumerate(ranked_skus)}
        ranked = sorted(candidates, key=lambda c: order.get(c.sku, len(order)))
        return ranked, rationale
    except Exception:
        logger.warning(
            "substitution: LLM ranking failed, falling back to cheapest-first", exc_info=True
        )
        fallback_rationale = "Ranked cheapest-first (substitution ranking unavailable)."
        return _cheapest_first(candidates), fallback_rationale
