"""`harness/injection_corpus.py` just needs to hold 15 distinct, non-empty
strings — `sessions.py` zips one per prompt-injection session, and
CLAUDE.md §8's table commits to exactly 15 of them."""

from __future__ import annotations

from harness.injection_corpus import INJECTION_STRINGS


def test_exactly_15_strings() -> None:
    assert len(INJECTION_STRINGS) == 15


def test_all_strings_distinct_and_nonempty() -> None:
    assert len(set(INJECTION_STRINGS)) == 15
    assert all(isinstance(s, str) and s.strip() for s in INJECTION_STRINGS)
