from __future__ import annotations

from praman.ingest.extract import ExtractedProduct
from praman.ingest.normalise import (
    apply_confidence_gate,
    canonical_quantity_suffix,
    dedupe_products,
    normalise_category,
    normalise_name_with_quantity,
    parse_quantity_to_grams,
)


def _product(**overrides: object) -> ExtractedProduct:
    defaults: dict[str, object] = {
        "name": "Toor Dal",
        "category": "pulses",
        "category_class": "consumable",
        "unit_price_paise": 18000,
        "field_confidence": {
            "name": 0.9,
            "category": 0.9,
            "category_class": 0.9,
            "unit_price_paise": 0.9,
            "is_personalised": 0.9,
        },
    }
    defaults.update(overrides)
    return ExtractedProduct(**defaults)


def test_parse_quantity_handles_numeric_kg_grams_and_word_forms() -> None:
    assert parse_quantity_to_grams("Toor Dal 500g") == 500
    assert parse_quantity_to_grams("Toor Dal 0.5 kg") == 500
    assert parse_quantity_to_grams("Toor Dal half kilo") == 500
    assert parse_quantity_to_grams("Basmati Rice 5kg") == 5000
    assert parse_quantity_to_grams("Loose Tea") is None


def test_canonical_quantity_suffix() -> None:
    assert canonical_quantity_suffix(500) == "500g"
    assert canonical_quantity_suffix(1000) == "1kg"
    assert canonical_quantity_suffix(5000) == "5kg"


def test_normalise_name_with_quantity_converges_different_unit_spellings() -> None:
    a = normalise_name_with_quantity("Toor Dal 500g")
    b = normalise_name_with_quantity("Toor Dal 0.5 kg")
    c = normalise_name_with_quantity("Toor Dal half kilo")
    assert a == b == c == "Toor Dal (500g)"


def test_normalise_name_leaves_no_stray_empty_parens() -> None:
    assert normalise_name_with_quantity("Chawal (basmati, 1 kg)") == "Chawal (basmati) (1kg)"


def test_normalise_name_without_quantity_is_unchanged() -> None:
    assert normalise_name_with_quantity("Silver Anklet Pair") == "Silver Anklet Pair"


def test_normalise_category_maps_known_aliases() -> None:
    assert normalise_category("Dal") == "pulses"
    assert normalise_category("PULSES") == "pulses"
    assert normalise_category("rings") == "rings"


def test_normalise_category_passes_through_unknown() -> None:
    assert normalise_category("artisanal ceramics") == "artisanal ceramics"


def test_confidence_gate_flags_low_confidence_field() -> None:
    product = _product(field_confidence={"name": 0.9, "unit_price_paise": 0.5})
    result = apply_confidence_gate(product)
    assert result.needs_review is True


def test_confidence_gate_passes_all_high_confidence() -> None:
    product = _product(field_confidence={"name": 0.9, "unit_price_paise": 0.95, "category": 0.85})
    result = apply_confidence_gate(product)
    assert result.needs_review is False


def test_confidence_gate_ignores_low_confidence_on_genuinely_null_nullable_field() -> None:
    # stock=None with confidence 0.0 means "the source simply doesn't say" —
    # not a shaky guess. It must not force review on its own.
    product = _product(
        stock=None,
        field_confidence={"name": 0.9, "unit_price_paise": 0.95, "stock": 0.0},
    )
    result = apply_confidence_gate(product)
    assert result.needs_review is False


def test_confidence_gate_still_flags_low_confidence_on_a_non_null_nullable_field() -> None:
    # A guessed (non-null) stock count that's actually shaky IS a risk.
    product = _product(
        stock=12,
        field_confidence={"name": 0.9, "unit_price_paise": 0.95, "stock": 0.2},
    )
    result = apply_confidence_gate(product)
    assert result.needs_review is True


def test_confidence_gate_with_no_confidence_data_defaults_to_review() -> None:
    product = _product(field_confidence={})
    result = apply_confidence_gate(product)
    assert result.needs_review is True


def test_dedupe_keeps_higher_confidence_duplicate() -> None:
    low = _product(name="Toor Dal 500g", field_confidence={"name": 0.5, "unit_price_paise": 0.5})
    high = _product(
        name="Toor Dal (500g)", field_confidence={"name": 0.95, "unit_price_paise": 0.95}
    )
    result = dedupe_products([low, high])
    assert len(result) == 1
    assert result[0].field_confidence["name"] == 0.95


def test_dedupe_leaves_distinct_products_alone() -> None:
    a = _product(name="Toor Dal 500g")
    b = _product(name="Moong Dal 500g")
    result = dedupe_products([a, b])
    assert len(result) == 2
