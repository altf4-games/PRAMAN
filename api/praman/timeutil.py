"""Shared time helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def as_aware_utc(ts: datetime) -> datetime:
    """sqlite's `DateTime(timezone=True)` round-trips as a naive datetime
    (unlike Postgres, which preserves tzinfo) — normalize so comparisons
    never mix naive and aware values. Every value we write is already UTC,
    so this is a safe assumption, not a correctness gap.
    """
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
