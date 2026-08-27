from __future__ import annotations

import json

import pytest
from praman.adapters.llm import FakeLLMClient
from praman.ingest.extract import (
    IngestExtractionError,
    _extract_json_array_substring,
    extract_from_image,
    extract_from_text,
)

_VALID_ITEM = {
    "name": "Toor Dal",
    "category": "pulses",
    "category_class": "consumable",
    "unit_price_paise": 18000,
    "stock": None,
    "return_window_days": 1,
    "fulfilment_hours": 24,
    "is_personalised": False,
    "field_confidence": {
        "name": 0.95,
        "category": 0.9,
        "category_class": 0.9,
        "unit_price_paise": 0.95,
        "stock": 0.0,
        "return_window_days": 0.8,
        "fulfilment_hours": 0.8,
        "is_personalised": 0.9,
    },
}


async def test_extract_from_text_parses_valid_model_response() -> None:
    llm = FakeLLMClient()
    llm.enqueue(json.dumps([_VALID_ITEM]))

    products = await extract_from_text(llm, "Toor Dal,1kg,180", source="csv")

    assert len(products) == 1
    assert products[0].name == "Toor Dal"
    assert products[0].unit_price_paise == 18000
    assert products[0].source == "csv"


async def test_extract_from_text_sends_the_source_text_to_the_model() -> None:
    llm = FakeLLMClient()
    llm.enqueue(json.dumps([_VALID_ITEM]))

    await extract_from_text(llm, "Toor Dal,1kg,180", source="csv")

    prompt, image_bytes, _mime_type = llm.calls[0]
    assert "Toor Dal,1kg,180" in prompt
    assert image_bytes is None


async def test_extract_from_image_attaches_bytes_and_mime_type() -> None:
    llm = FakeLLMClient()
    llm.enqueue(json.dumps([_VALID_ITEM]))

    products = await extract_from_image(llm, b"fake-png-bytes", mime_type="image/png")

    assert products[0].source == "vlm"
    _prompt, image_bytes, mime_type = llm.calls[0]
    assert image_bytes == b"fake-png-bytes"
    assert mime_type == "image/png"


async def test_extract_raises_on_non_json_response() -> None:
    llm = FakeLLMClient()
    llm.enqueue("not json at all")

    with pytest.raises(IngestExtractionError):
        await extract_from_text(llm, "some text")


async def test_extract_raises_on_non_array_response() -> None:
    llm = FakeLLMClient()
    llm.enqueue(json.dumps({"not": "a list"}))

    with pytest.raises(IngestExtractionError):
        await extract_from_text(llm, "some text")


async def test_extract_raises_on_schema_violation() -> None:
    llm = FakeLLMClient()
    bad_item = dict(_VALID_ITEM)
    bad_item["category_class"] = "not-a-real-class"
    llm.enqueue(json.dumps([bad_item]))

    with pytest.raises(IngestExtractionError):
        await extract_from_text(llm, "some text")


async def test_fake_llm_client_raises_when_queue_empty() -> None:
    llm = FakeLLMClient()
    with pytest.raises(RuntimeError):
        await llm.generate_json("prompt")


# --- Lenient parsing: real Gemini responses have been observed to wrap
# valid JSON in a markdown fence, add leading prose, or leave a trailing
# comma despite response_mime_type="application/json". These should be
# repaired for free rather than failing the whole file. ---


async def test_extract_recovers_from_markdown_code_fence() -> None:
    llm = FakeLLMClient()
    llm.enqueue(f"```json\n{json.dumps([_VALID_ITEM])}\n```")

    products = await extract_from_text(llm, "some text")

    assert len(products) == 1
    assert products[0].name == "Toor Dal"


async def test_extract_recovers_from_leading_prose_before_array() -> None:
    llm = FakeLLMClient()
    llm.enqueue(f"Here is the extracted catalog:\n{json.dumps([_VALID_ITEM])}")

    products = await extract_from_text(llm, "some text")

    assert len(products) == 1


async def test_extract_recovers_from_trailing_comma() -> None:
    llm = FakeLLMClient()
    # json.dumps never produces a trailing comma — build it by hand.
    raw = "[" + json.dumps(_VALID_ITEM) + ",]"
    llm.enqueue(raw)

    products = await extract_from_text(llm, "some text")

    assert len(products) == 1


def test_extract_json_array_substring_ignores_brackets_inside_strings() -> None:
    text = '[{"name": "Rice [Basmati]", "note": "a]b"}]'
    assert _extract_json_array_substring(text) == text


def test_extract_json_array_substring_ignores_escaped_quotes() -> None:
    text = r'[{"name": "5\" ring"}]'
    assert _extract_json_array_substring(text) == text


def test_extract_json_array_substring_stops_at_matching_close_bracket() -> None:
    text = '[{"a": 1}] some trailing prose the model added'
    assert _extract_json_array_substring(text) == '[{"a": 1}]'


def test_extract_json_array_substring_returns_none_when_no_array_present() -> None:
    assert _extract_json_array_substring("no brackets here") is None


async def test_extract_still_raises_on_genuinely_broken_json() -> None:
    llm = FakeLLMClient()
    llm.enqueue('[{"name": "Toor Dal", "unit_price_paise": }]')  # missing value, unrecoverable

    with pytest.raises(IngestExtractionError):
        await extract_from_text(llm, "some text")
