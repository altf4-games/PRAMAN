"""Tests for `harness/sessions.py`'s session-building logic — the exact
category counts from CLAUDE.md §8's table, and the envelope-sizing fix
(sessions.py's own comments explain why a flat ceiling was a harness bug,
not a gate finding) that a regression here would silently reintroduce."""

from __future__ import annotations

from collections import Counter

from harness.sessions import PRICE_BY_SKU, build_sessions


def test_build_sessions_produces_200_sessions() -> None:
    assert len(build_sessions()) == 200


def test_category_counts_match_claude_md_table() -> None:
    counts = Counter(s.category for s in build_sessions())
    assert counts == {
        "benign_green": 90,
        "benign_amber_red": 30,
        "stale_quote_race": 25,
        "envelope_escape": 20,
        "prompt_injection": 15,
        "replay_duplicate": 12,
        "identity_spoof": 8,
    }


def test_every_session_id_is_unique() -> None:
    ids = [s.id for s in build_sessions()]
    assert len(ids) == len(set(ids))


def test_benign_and_stale_quote_envelopes_never_smaller_than_the_cart_itself() -> None:
    """The bug this guards: an earlier version of the harness used a flat
    envelope ceiling for every session, which made some legitimately-priced
    jewellery items structurally unbuyable and produced false positives
    that were really just a harness sizing bug, not the gate over-blocking.
    Every benign/stale-quote session's ceiling must be able to afford its
    own cart at the *original* (pre-chaos) price."""
    for spec in build_sessions():
        if spec.category not in ("benign_green", "benign_amber_red", "stale_quote_race"):
            continue
        cart_total = PRICE_BY_SKU[spec.sku] * spec.qty
        assert spec.envelope_ceiling_paise >= cart_total, (
            f"{spec.id}: ceiling {spec.envelope_ceiling_paise} paise can't cover "
            f"cart total {cart_total} paise for {spec.sku}"
        )


def test_envelope_escape_sessions_are_all_attacks() -> None:
    specs = [s for s in build_sessions() if s.category == "envelope_escape"]
    assert len(specs) == 20
    assert all(s.is_attack for s in specs)
    # round-robins across all four sub-checks
    kinds = Counter()
    for s in specs:
        if s.wrong_agent:
            kinds["wrong_agent"] += 1
        elif s.envelope_ceiling_paise == 100:
            kinds["exceed_ceiling"] += 1
        elif s.qty == 1000:
            kinds["exceed_single_txn"] += 1
        elif s.allowed_categories_override:
            kinds["disallowed_category"] += 1
    assert sum(kinds.values()) == 20
    assert len(kinds) == 4


def test_identity_spoof_sessions_split_across_three_techniques() -> None:
    specs = [s for s in build_sessions() if s.category == "identity_spoof"]
    assert len(specs) == 8
    assert sum(s.revoked_agent for s in specs) == 3
    assert sum(s.unregistered_agent for s in specs) == 3
    assert sum(s.bad_signature for s in specs) == 2
    assert all(s.is_attack for s in specs)


def test_replay_sessions_flagged_replay_and_attack() -> None:
    specs = [s for s in build_sessions() if s.category == "replay_duplicate"]
    assert len(specs) == 12
    assert all(s.replay and s.is_attack for s in specs)


def test_prompt_injection_sessions_carry_distinct_inject_text() -> None:
    specs = [s for s in build_sessions() if s.category == "prompt_injection"]
    assert len(specs) == 15
    assert all(s.inject_text for s in specs)
    assert len({s.inject_text for s in specs}) == 15
    # not scored as attack/benign — this category is graded as an
    # invariance check, not a block-or-allow outcome
    assert all(not s.is_attack for s in specs)
