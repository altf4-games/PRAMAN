"""Deterministic normalisation. The VLM/LLM extracts; this module cleans up
after it — unit strings, category vocabulary, duplicates, and the
confidence gate that decides `needs_review`. Nothing here calls a model;
every function is a pure, testable transformation. This is the
anti-hallucination gate at the data layer (the design spec §2): a product that
fails the gate is never exposed to agents.
"""

from __future__ import annotations

import re

from praman.config import CONFIDENCE_THRESHOLD
from praman.ingest.extract import ExtractedProduct

# --- Quantity/unit normalisation ---
# Handles the messiness the spec calls out explicitly: "500g", "0.5 kg",
# "half kilo" all need to resolve to the same canonical quantity so
# duplicate listings of the same product are recognised as duplicates.

_WORD_FRACTIONS: dict[str, float] = {
    "half": 0.5,
    "quarter": 0.25,
    "one and a half": 1.5,
}

_KILO_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE)
_GRAM_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*g\b", re.IGNORECASE)
_WORD_KILO_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _WORD_FRACTIONS) + r")\s*(?:a\s+)?kilo\b",
    re.IGNORECASE,
)


def parse_quantity_to_grams(text: str) -> float | None:
    """Extracts a weight mentioned anywhere in `text` and returns it in
    grams, or None if no recognisable weight is present. Case-insensitive;
    handles numeric kilograms, numeric grams, and a few common word forms.
    """
    match = _WORD_KILO_PATTERN.search(text)
    if match:
        return _WORD_FRACTIONS[match.group(1).lower()] * 1000

    match = _KILO_PATTERN.search(text)
    if match:
        return float(match.group(1)) * 1000

    match = _GRAM_PATTERN.search(text)
    if match:
        return float(match.group(1))

    return None


def canonical_quantity_suffix(grams: float) -> str:
    """Renders a gram amount the way a canonical product name would show
    it: whole kilos as 'Xkg', everything else as 'Xg'."""
    if grams >= 1000 and grams % 1000 == 0:
        return f"{int(grams // 1000)}kg"
    if grams == int(grams):
        return f"{int(grams)}g"
    return f"{grams:g}g"


def normalise_name_with_quantity(name: str) -> str:
    """Rewrites any weight mention in `name` to a canonical suffix, e.g.
    'Toor Dal half kilo' and 'Toor Dal 0.5 kg' both become
    'Toor Dal (500g)'. Names with no recognisable weight are returned
    unchanged."""
    grams = parse_quantity_to_grams(name)
    if grams is None:
        return name.strip()

    stripped = _WORD_KILO_PATTERN.sub("", name)
    stripped = _KILO_PATTERN.sub("", stripped)
    stripped = _GRAM_PATTERN.sub("", stripped)
    stripped = re.sub(r",?\s*\)", ")", stripped)  # "(basmati, )" -> "(basmati)"
    stripped = re.sub(r"\(\s*\)", "", stripped)  # drop parens left fully empty
    base = re.sub(r"\s+", " ", stripped).strip(" -,")
    return f"{base} ({canonical_quantity_suffix(grams)})"


# --- Category normalisation ---
# Maps free-text categories the model might produce to a small controlled
# vocabulary. Unknown categories pass through unchanged (with their
# model-given category_class kept) rather than being coerced incorrectly.

_CATEGORY_ALIASES: dict[str, str] = {
    "pulse": "pulses",
    "pulses": "pulses",
    "dal": "pulses",
    "lentil": "pulses",
    "lentils": "pulses",
    "grain": "grains",
    "grains": "grains",
    "rice": "grains",
    "wheat": "grains",
    "atta": "grains",
    "flour": "grains",
    "spice": "spices",
    "spices": "spices",
    "masala": "spices",
    "oil": "cooking oil",
    "cooking oil": "cooking oil",
    "vegetable": "vegetables",
    "vegetables": "vegetables",
    "fruit": "fruits",
    "fruits": "fruits",
    "dairy": "dairy",
    "milk": "dairy",
    "snack": "snacks",
    "snacks": "snacks",
    "beverage": "beverages",
    "beverages": "beverages",
    "ring": "rings",
    "rings": "rings",
    "necklace": "necklaces",
    "necklaces": "necklaces",
    "chain": "chains",
    "chains": "chains",
    "earring": "earrings",
    "earrings": "earrings",
    "bangle": "bangles",
    "bangles": "bangles",
    "pendant": "pendants",
    "pendants": "pendants",
}


def normalise_category(raw_category: str) -> str:
    key = raw_category.strip().lower()
    return _CATEGORY_ALIASES.get(key, key)


# --- Confidence gate ---


_NULLABLE_FIELDS = frozenset({"stock", "return_window_days", "fulfilment_hours"})


def apply_confidence_gate(
    product: ExtractedProduct, threshold: float = CONFIDENCE_THRESHOLD
) -> ExtractedProduct:
    """Any field below `threshold` flags the whole product `needs_review` —
    except a nullable field (`stock`, `return_window_days`,
    `fulfilment_hours`) that is genuinely `None`. Many small merchants
    simply don't track exact stock counts; the model correctly reports low
    confidence for a value it has no basis to assert, and that "correctly
    absent" state carries no risk of being wrong the way a shaky price
    reading does. Gating on it would send every catalog to review
    regardless of how legible the source was.
    """
    relevant_confidences = [
        confidence
        for field_name, confidence in product.field_confidence.items()
        if not (field_name in _NULLABLE_FIELDS and getattr(product, field_name, None) is None)
    ]
    needs_review = (
        any(c < threshold for c in relevant_confidences) if relevant_confidences else True
    )
    return product.model_copy(update={"needs_review": needs_review})


# --- Dedupe ---


def _dedupe_key(product: ExtractedProduct) -> str:
    grams = parse_quantity_to_grams(product.name)
    base_name = normalise_name_with_quantity(product.name).lower()
    return f"{base_name}|{grams}"


def dedupe_products(products: list[ExtractedProduct]) -> list[ExtractedProduct]:
    """Merges near-duplicate entries (same normalised name + quantity),
    keeping whichever copy has the higher average field confidence."""

    def avg_confidence(p: ExtractedProduct) -> float:
        values = list(p.field_confidence.values())
        return sum(values) / len(values) if values else 0.0

    best_by_key: dict[str, ExtractedProduct] = {}
    for product in products:
        key = _dedupe_key(product)
        existing = best_by_key.get(key)
        if existing is None or avg_confidence(product) > avg_confidence(existing):
            best_by_key[key] = product
    return list(best_by_key.values())


def normalise_product(product: ExtractedProduct) -> ExtractedProduct:
    """The full deterministic pipeline for one product: canonicalise its
    name/quantity, normalise its category, then apply the confidence gate."""
    updated = product.model_copy(
        update={
            "name": normalise_name_with_quantity(product.name),
            "category": normalise_category(product.category),
        }
    )
    return apply_confidence_gate(updated)


def normalise_batch(products: list[ExtractedProduct]) -> list[ExtractedProduct]:
    return dedupe_products([normalise_product(p) for p in products])
