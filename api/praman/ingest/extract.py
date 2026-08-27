"""Catalog extraction. This is the only place in the codebase that turns raw
merchant input (a photo, a scraped page, a CSV blob) into structured
product data — via a live call to whatever `LLMClient` `get_llm_client()`
resolves to. It does not normalise units or map category vocabulary; that's
`normalise.py`'s job, deterministically. See the design spec §0/§2: the LLM
extracts, it is never load-bearing in the money path, and its output is
never exposed to agents until it clears the confidence gate.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from praman.adapters.llm import LLMClient
from praman.ingest.prompts import build_prompt

CategoryClass = Literal["perishable", "consumable", "digital", "durable", "service", "bespoke"]


class ExtractedProduct(BaseModel):
    name: str
    category: str
    category_class: CategoryClass
    unit_price_paise: int
    stock: int | None = None
    return_window_days: int | None = None
    fulfilment_hours: int | None = None
    is_personalised: bool = False
    field_confidence: dict[str, float] = Field(default_factory=dict)
    needs_review: bool = False
    source: Literal["whatsapp", "vlm", "csv", "manual"] = "vlm"
    source_media_url: str | None = None


class IngestExtractionError(Exception):
    """Raised when the model's response can't be parsed as the expected
    schema. Callers should treat this as 'nothing extracted', not crash the
    whole ingest run — one bad source shouldn't take down the batch."""


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[\]}])")


def _extract_json_array_substring(text: str) -> str | None:
    """Finds the first top-level `[...]` in `text` by bracket-balanced
    scanning (string-aware, so brackets inside a JSON string value don't
    throw off the count). Returns None if no balanced array is found."""
    start = text.find("[")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        char = text[i]
        if escape_next:
            escape_next = False
            continue
        if char == "\\":
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _lenient_json_loads(raw_text: str) -> object:
    """Gemini's `response_mime_type="application/json"` usually produces
    clean JSON, but not always — a stray markdown fence, leading prose, or
    a trailing comma has been observed in practice. Try strict parsing
    first; only fall back to these repairs (all free, no extra API call)
    on failure, and only ever use the repaired result if it *also* parses
    cleanly — this never guesses at content, only strips known-safe noise.
    """
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    cleaned = _CODE_FENCE_RE.sub("", raw_text.strip())
    array_text = _extract_json_array_substring(cleaned) or cleaned
    array_text = _TRAILING_COMMA_RE.sub(r"\1", array_text)
    return json.loads(array_text)  # let this raise if repairs weren't enough


def _parse_model_response(raw_text: str) -> list[ExtractedProduct]:
    try:
        payload = _lenient_json_loads(raw_text)
    except json.JSONDecodeError as exc:
        raise IngestExtractionError(f"model response was not valid JSON: {exc}") from exc

    if not isinstance(payload, list):
        raise IngestExtractionError(f"expected a JSON array, got {type(payload).__name__}")

    products: list[ExtractedProduct] = []
    for i, item in enumerate(payload):
        try:
            products.append(ExtractedProduct(**item))
        except ValidationError as exc:
            raise IngestExtractionError(f"item {i} failed schema validation: {exc}") from exc
    return products


async def extract_from_text(
    llm: LLMClient, text: str, *, source: Literal["csv", "vlm"] = "vlm", source_hint: str = "text"
) -> list[ExtractedProduct]:
    prompt = build_prompt(source_hint) + f"\n---\n{text}\n---\n"
    raw = await llm.generate_json(prompt)
    products = _parse_model_response(raw)
    return [p.model_copy(update={"source": source}) for p in products]


async def extract_from_image(
    llm: LLMClient,
    image_bytes: bytes,
    *,
    mime_type: str = "image/png",
    source_hint: str = "photograph of a price list",
    source_media_url: str | None = None,
) -> list[ExtractedProduct]:
    prompt = build_prompt(source_hint)
    raw = await llm.generate_json(prompt, image_bytes=image_bytes, mime_type=mime_type)
    products = _parse_model_response(raw)
    return [
        p.model_copy(update={"source": "vlm", "source_media_url": source_media_url})
        for p in products
    ]
