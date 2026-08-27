"""The VLM/LLM extraction prompt. One prompt, shared by every raw-input
shape (scraped HTML, messy CSV text, a photographed price list) — the model
always returns the same JSON array shape; `extract.py` parses it into
`ExtractedProduct`, and `normalise.py` deterministically cleans up whatever
the model didn't standardize on its own (units, category vocabulary,
duplicates).
"""

from __future__ import annotations

EXTRACTION_SCHEMA_INSTRUCTIONS = """\
You are extracting a merchant's product catalog from raw source material for
PRAMAN, a system that lets AI shopping agents transact with small Indian
retailers. The source material may be a scraped HTML product listing, a
messy CSV/text price list (inconsistent units like "500g", "0.5 kg", "half
kilo"), or a photograph of a handwritten or printed price list.

Return ONLY a JSON array. Each element describes exactly one product, with
this exact shape:

{
  "name": string,                          // product name, keep any size/qty mentioned in the source
  "category": string,                      // free-text category, e.g. "pulses", "engraved jewellery"
  "category_class": one of
      ["perishable", "consumable", "digital", "durable", "service", "bespoke"],
  "unit_price_paise": integer,             // price in INR paise (rupees * 100), your best reading
  "stock": integer or null,                // units in stock if stated, else null
  "return_window_days": integer or null,   // typical for the category if not stated: perishable/consumable=0-2,
                                            // durable=7-15, bespoke/made-to-order=0
  "fulfilment_hours": integer or null,     // typical: perishable/consumable=2-24, durable=24-72, bespoke=72-336
  "is_personalised": boolean,              // true if engraved / made-to-order / custom-fit / non-returnable by nature
  "field_confidence": {                    // your own confidence 0.0-1.0 per field, reflecting how legible/
                                            // explicit the source was for that field (NOT a fixed constant —
                                            // score genuinely lower for illegible handwriting, ambiguous units,
                                            // or values you had to infer rather than read)
    "name": number, "category": number, "category_class": number,
    "unit_price_paise": number, "stock": number,
    "return_window_days": number, "fulfilment_hours": number, "is_personalised": number
  }
}

Confidence calibration — read this carefully, it matters:
- Confidence measures how sure you are about the VALUE you're reporting,
  not how many decorative options existed for it. A category-based default
  applied per well-established retail convention (e.g. perishable produce
  has a 0-2 day return window; nobody offers returns on custom engraving)
  is a RELIABLE inference, not a guess — score it 0.80-0.95, the same as
  something read directly off the page.
- Reserve LOW confidence (below 0.5) for genuine ambiguity in the source
  itself: illegible handwriting, a smudged price, a quantity that could be
  read two different ways, or a category you truly can't determine.
- Most well-formed line items in a clean, legible source should end up with
  every field at or above 0.75. Only a source that is genuinely hard to
  read should produce low-confidence fields.

Rules:
- If a field is illegible, ambiguous, or absent, still provide your best
  guess but give it low confidence (below 0.5) rather than omitting it.
- Never invent products that aren't in the source.
- Prices are always in Indian Rupees; convert to integer paise yourself.
- Do not normalise or deduplicate — that happens in a later deterministic
  step. Extract what you see, faithfully, one entry per line item.
"""


def build_prompt(source_hint: str) -> str:
    return f"{EXTRACTION_SCHEMA_INSTRUCTIONS}\nSource type: {source_hint}\n"
