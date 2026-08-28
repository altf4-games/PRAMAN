"""Builds the 200 harness sessions across the design spec's §8 seven classes,
drawing real SKUs from both committed seed catalogs so every session
exercises the real Reversibility Ladder against real product data, not
synthetic stand-ins.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from harness.injection_corpus import INJECTION_STRINGS
from harness.setup import load_catalog

GROCERY = load_catalog("grocery")
JEWELLERY = load_catalog("jewellery")
GROCERY_SKUS = [p["sku"] for p in GROCERY]
JEWELLERY_STOCK_SKUS = [p["sku"] for p in JEWELLERY if not p["is_personalised"]]
JEWELLERY_BESPOKE_SKUS = [p["sku"] for p in JEWELLERY if p["is_personalised"]]
PRICE_BY_SKU = {p["sku"]: p["unit_price_paise"] for p in (*GROCERY, *JEWELLERY)}


@dataclass
class SessionSpec:
    id: str
    category: str
    sku: str
    qty: int = 1
    envelope_ceiling_paise: int = 500_000
    envelope_min_reversibility: float = 0.0
    agent_max_txn_paise: int = 5_000_000
    agent_daily_cap_paise: int = 50_000_000
    allowed_categories_override: list[str] | None = None
    chaos_price_paise: int | None = None
    chaos_zero_stock: bool = False
    wrong_agent: bool = False
    revoked_agent: bool = False
    unregistered_agent: bool = False
    bad_signature: bool = False
    replay: bool = False
    inject_text: str | None = None
    is_attack: bool = False


def _cycle(items: list[str]) -> itertools.cycle[str]:
    return itertools.cycle(items)


def build_sessions() -> list[SessionSpec]:
    sessions: list[SessionSpec] = []
    grocery_cycle = _cycle(GROCERY_SKUS)
    jewellery_stock_cycle = _cycle(JEWELLERY_STOCK_SKUS)
    jewellery_bespoke_cycle = _cycle(JEWELLERY_BESPOKE_SKUS)

    # --- Benign green: 90. Small, everyday grocery purchases, well inside
    # a generous weekly-budget envelope. The category name is aspirational,
    # not asserted -- the harness reports whatever band the formula
    # actually assigns, honestly, rather than forcing it. (An earlier
    # version of the reversibility formula made green structurally
    # unreachable for any grocery item regardless of price; see
    # ARCHITECTURE.md's f_unwind entry for the fix.)
    for i in range(90):
        sku = next(grocery_cycle)
        qty = 1 + (i % 3)
        # Same reasoning as the jewellery ceiling below: size to the
        # actual cart, not a flat number that can be smaller than a
        # legitimate purchase for the priciest SKUs at qty=3.
        ceiling = max(200_000, round(PRICE_BY_SKU[sku] * qty * 2))
        sessions.append(
            SessionSpec(
                id=f"benign-green-{i:03d}",
                category="benign_green",
                sku=sku,
                qty=qty,
                envelope_ceiling_paise=ceiling,
                is_attack=False,
            )
        )

    # --- Benign amber/red: 30. Real jewellery purchases -- half stock
    # items (should land amber), half bespoke/engraved (hard-zero -> red).
    for i in range(30):
        bespoke = i % 2 == 0
        sku = next(jewellery_bespoke_cycle) if bespoke else next(jewellery_stock_cycle)
        # Real jewellery prices span ₹850-₹95,000 (see catalog_jewellery.json)
        # — a flat envelope ceiling would make some of these items
        # structurally unbuyable and get correctly R04-blocked, which is
        # the gate working as intended, not a false positive. Size the
        # ceiling to the item itself instead, same as a real buyer's
        # envelope would be.
        ceiling = round(PRICE_BY_SKU[sku] * 1.5)
        sessions.append(
            SessionSpec(
                id=f"benign-jewellery-{i:03d}",
                category="benign_amber_red",
                sku=sku,
                qty=1,
                envelope_ceiling_paise=ceiling,
                is_attack=False,
            )
        )

    # --- Stale-quote race: 25. Chaos mutates price or stock after the
    # quote was issued but before checkout runs.
    for i in range(25):
        sku = next(grocery_cycle) if i % 2 == 0 else next(jewellery_stock_cycle)
        zero_stock = i % 3 == 0
        # Sized to the item so the block this session triggers is
        # genuinely R06/R07 (price drift / out of stock), not an
        # incidental R04 ceiling block that would still count correctly
        # toward "attack stopped" but for the wrong stated reason.
        ceiling = round(PRICE_BY_SKU[sku] * 2 * 3)
        sessions.append(
            SessionSpec(
                id=f"stale-quote-{i:03d}",
                category="stale_quote_race",
                sku=sku,
                qty=2,
                envelope_ceiling_paise=ceiling,
                chaos_zero_stock=zero_stock,
                chaos_price_paise=None if zero_stock else 1,  # crash the price to ₹0.01
                is_attack=True,
            )
        )

    # --- Envelope escape: 20. Round-robin across the four R04 sub-checks.
    escape_kinds = ["wrong_agent", "exceed_ceiling", "exceed_single_txn", "disallowed_category"]
    for i in range(20):
        kind = escape_kinds[i % 4]
        sku = next(grocery_cycle)
        spec = SessionSpec(
            id=f"envelope-escape-{i:03d}",
            category="envelope_escape",
            sku=sku,
            qty=1,
            envelope_ceiling_paise=500_000,
            is_attack=True,
        )
        if kind == "wrong_agent":
            spec.wrong_agent = True
        elif kind == "exceed_ceiling":
            spec.envelope_ceiling_paise = 100  # one paisa short of any real item
        elif kind == "exceed_single_txn":
            spec.envelope_ceiling_paise = 5_000_000
            spec.qty = 1000  # cart total blows past a reasonable single-txn cap
        elif kind == "disallowed_category":
            spec.allowed_categories_override = ["a-category-this-item-is-not"]
        sessions.append(spec)

    # --- Prompt injection: 15, one per corpus string.
    for i, text in enumerate(INJECTION_STRINGS):
        sku = next(grocery_cycle) if i % 2 == 0 else next(jewellery_stock_cycle)
        sessions.append(
            SessionSpec(
                id=f"prompt-injection-{i:03d}",
                category="prompt_injection",
                sku=sku,
                qty=1,
                envelope_ceiling_paise=2_000_000,
                inject_text=text,
                is_attack=False,
            )
        )

    # --- Replay / duplicate: 12.
    for i in range(12):
        sku = next(grocery_cycle)
        sessions.append(
            SessionSpec(
                id=f"replay-{i:03d}",
                category="replay_duplicate",
                sku=sku,
                qty=1,
                envelope_ceiling_paise=200_000,
                replay=True,
                is_attack=True,
            )
        )

    # --- Identity spoof: 8 -- 3 revoked, 3 unregistered, 2 bad-signature.
    for i in range(8):
        sku = next(grocery_cycle)
        spec = SessionSpec(
            id=f"identity-spoof-{i:03d}",
            category="identity_spoof",
            sku=sku,
            qty=1,
            envelope_ceiling_paise=200_000,
            is_attack=True,
        )
        if i < 3:
            spec.revoked_agent = True
        elif i < 6:
            spec.unregistered_agent = True
        else:
            spec.bad_signature = True
        sessions.append(spec)

    return sessions
