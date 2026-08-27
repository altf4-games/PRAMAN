from __future__ import annotations

from datetime import UTC, datetime, timedelta

from praman.config import COOLING_OFF_WINDOW_DEMO_MODE_S, COOLING_OFF_WINDOW_S
from praman.core.cooling_off import (
    buyer_undo_message,
    compute_cooling_off_until,
    cooling_off_window_seconds,
    is_cooling_off_expired,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_window_seconds_demo_vs_real() -> None:
    assert cooling_off_window_seconds(demo_mode=True) == COOLING_OFF_WINDOW_DEMO_MODE_S
    assert cooling_off_window_seconds(demo_mode=False) == COOLING_OFF_WINDOW_S


def test_compute_cooling_off_until() -> None:
    until = compute_cooling_off_until(NOW, demo_mode=False)
    assert until == NOW + timedelta(seconds=COOLING_OFF_WINDOW_S)


def test_is_cooling_off_expired_boundary() -> None:
    until = NOW + timedelta(seconds=60)
    assert not is_cooling_off_expired(until, NOW)
    assert is_cooling_off_expired(until, until)
    assert is_cooling_off_expired(until, until + timedelta(seconds=1))


def test_buyer_undo_message_contains_key_details() -> None:
    message = buyer_undo_message("Silver Chain", 640000, "Sharma Jewellers", 30)
    assert "Silver Chain" in message
    assert "6,400.00" in message
    assert "Sharma Jewellers" in message
    assert "30 minutes" in message
