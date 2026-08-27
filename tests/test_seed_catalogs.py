"""Acceptance (CLAUDE.md Phase 2): both seed catalogs load, ~40 SKUs each,
category-class diversity sufficient to eventually populate every
reversibility band (Phase 5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SEED_DIR = Path(__file__).resolve().parent.parent / "api" / "praman" / "seed"

CATALOG_FILES = ["catalog_grocery.json", "catalog_jewellery.json"]

REQUIRED_FIELDS = {
    "sku",
    "name",
    "category",
    "category_class",
    "unit_price_paise",
    "stock",
    "return_window_days",
    "fulfilment_hours",
    "restocking_cost_pct",
    "is_personalised",
    "field_confidence",
    "needs_review",
    "source",
}

VALID_CATEGORY_CLASSES = {"perishable", "consumable", "digital", "durable", "service", "bespoke"}


@pytest.mark.parametrize("filename", CATALOG_FILES)
def test_catalog_loads_and_has_required_fields(filename: str) -> None:
    data = json.loads((SEED_DIR / filename).read_text())
    assert len(data) >= 35  # "~40 SKUs" per catalog
    for item in data:
        assert REQUIRED_FIELDS.issubset(item.keys())
        assert item["category_class"] in VALID_CATEGORY_CLASSES
        assert isinstance(item["unit_price_paise"], int)
        assert item["unit_price_paise"] > 0


def test_grocery_catalog_is_perishable_or_consumable_and_not_personalised() -> None:
    data = json.loads((SEED_DIR / "catalog_grocery.json").read_text())
    for item in data:
        assert item["category_class"] in {"perishable", "consumable"}
        assert item["is_personalised"] is False


def test_jewellery_catalog_has_both_stock_and_bespoke_items() -> None:
    data = json.loads((SEED_DIR / "catalog_jewellery.json").read_text())
    classes = {item["category_class"] for item in data}
    assert "durable" in classes
    assert "bespoke" in classes

    bespoke_items = [item for item in data if item["category_class"] == "bespoke"]
    assert len(bespoke_items) >= 5
    for item in bespoke_items:
        assert item["is_personalised"] is True
        assert item["return_window_days"] == 0

    durable_items = [item for item in data if item["category_class"] == "durable"]
    assert len(durable_items) >= 5
    for item in durable_items:
        assert item["is_personalised"] is False


def test_catalogs_together_span_at_least_five_skus_of_each_represented_class() -> None:
    all_items = []
    for filename in CATALOG_FILES:
        all_items.extend(json.loads((SEED_DIR / filename).read_text()))

    from collections import Counter

    counts = Counter(item["category_class"] for item in all_items)
    for category_class, count in counts.items():
        assert count >= 5, f"{category_class} only has {count} SKUs, want >= 5"
