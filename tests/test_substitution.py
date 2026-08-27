from __future__ import annotations

import json
from datetime import UTC, datetime

from praman.adapters.llm import FakeLLMClient
from praman.core.envelope import Envelope
from praman.core.substitution import (
    SubstitutionCandidate,
    deterministic_filter,
    rank_candidates,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _envelope(ceiling: int = 1_000_000, spent: int = 0) -> Envelope:
    return Envelope(
        agent_did="did:key:zAgent",
        revoked_at=None,
        valid_from=NOW,
        valid_until=NOW,
        allowed_categories=("pulses",),
        max_single_txn_paise=ceiling,
        ceiling_paise=ceiling,
        spent_paise=spent,
    )


def _original(**overrides: object) -> SubstitutionCandidate:
    defaults: dict[str, object] = {
        "product_id": "orig",
        "sku": "toor-dal-1kg",
        "name": "Toor Dal 1kg",
        "category": "pulses",
        "category_class": "consumable",
        "unit_price_paise": 18000,
        "stock": 0,
        "return_window_days": 2,
        "fulfilment_hours": 24,
        "restocking_cost_pct": 0.0,
        "is_personalised": False,
    }
    defaults.update(overrides)
    return SubstitutionCandidate(**defaults)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> SubstitutionCandidate:
    defaults: dict[str, object] = {
        "product_id": "cand-1",
        "sku": "moong-dal-1kg",
        "name": "Moong Dal 1kg",
        "category": "pulses",
        "category_class": "consumable",
        "unit_price_paise": 17500,
        "stock": 10,
        "return_window_days": 2,
        "fulfilment_hours": 24,
        "restocking_cost_pct": 0.0,
        "is_personalised": False,
    }
    defaults.update(overrides)
    return SubstitutionCandidate(**defaults)  # type: ignore[arg-type]


def test_filter_accepts_a_valid_candidate() -> None:
    original = _original()
    candidate = _candidate()
    result = deterministic_filter(original, 1, [candidate], _envelope())
    assert candidate in result


def test_filter_excludes_wrong_category() -> None:
    original = _original()
    candidate = _candidate(category="spices")
    result = deterministic_filter(original, 1, [candidate], _envelope())
    assert candidate not in result


def test_filter_excludes_insufficient_stock() -> None:
    original = _original()
    candidate = _candidate(stock=0)
    result = deterministic_filter(original, 5, [candidate], _envelope())
    assert candidate not in result


def test_filter_excludes_over_headroom() -> None:
    original = _original()
    candidate = _candidate(unit_price_paise=999_999)
    result = deterministic_filter(original, 1, [candidate], _envelope(ceiling=500_000, spent=0))
    assert candidate not in result


def test_filter_excludes_shorter_return_window() -> None:
    original = _original(return_window_days=10)
    candidate = _candidate(return_window_days=2)
    result = deterministic_filter(original, 1, [candidate], _envelope())
    assert candidate not in result


def test_filter_excludes_worse_band() -> None:
    original = _original(category_class="consumable")
    worse = _candidate(category_class="bespoke", is_personalised=True)
    result = deterministic_filter(original, 1, [worse], _envelope())
    assert worse not in result


def test_filter_excludes_the_original_itself() -> None:
    original = _original()
    same_as_original = _candidate(product_id="orig")
    result = deterministic_filter(original, 1, [same_as_original], _envelope())
    assert same_as_original not in result


async def test_rank_candidates_empty_returns_empty() -> None:
    llm = FakeLLMClient()
    ranked, rationale = await rank_candidates(llm, _original(), [])
    assert ranked == []
    assert rationale


async def test_rank_candidates_uses_llm_order() -> None:
    llm = FakeLLMClient()
    c1 = _candidate(product_id="c1", sku="c1-sku", unit_price_paise=20000)
    c2 = _candidate(product_id="c2", sku="c2-sku", unit_price_paise=15000)
    llm.enqueue(
        json.dumps({"ranked_skus": ["c2-sku", "c1-sku"], "rationale": "c2 is closer in price"})
    )

    ranked, rationale = await rank_candidates(llm, _original(), [c1, c2])

    assert [c.sku for c in ranked] == ["c2-sku", "c1-sku"]
    assert rationale == "c2 is closer in price"


async def test_rank_candidates_falls_back_to_cheapest_first_on_llm_failure() -> None:
    llm = FakeLLMClient()
    llm.enqueue("not valid json")
    c1 = _candidate(product_id="c1", sku="c1-sku", unit_price_paise=20000)
    c2 = _candidate(product_id="c2", sku="c2-sku", unit_price_paise=15000)

    ranked, rationale = await rank_candidates(llm, _original(), [c1, c2])

    assert [c.sku for c in ranked] == ["c2-sku", "c1-sku"]  # cheapest first
    assert "unavailable" in rationale.lower()


async def test_rank_candidates_falls_back_when_llm_raises() -> None:
    class _RaisingLLM:
        async def generate_json(self, prompt: str, **kwargs: object) -> str:
            raise RuntimeError("provider down")

    c1 = _candidate(product_id="c1", sku="c1-sku", unit_price_paise=20000)
    ranked, rationale = await rank_candidates(_RaisingLLM(), _original(), [c1])  # type: ignore[arg-type]

    assert [c.sku for c in ranked] == ["c1-sku"]
    assert "unavailable" in rationale.lower()
