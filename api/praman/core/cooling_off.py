"""Cooling-off: an amber-band order is authorized and paid immediately but
dispatch is withheld until `cooling_off_until` — a buyer's one-tap WhatsApp
undo triggers a refund and cancellation any time before then; an
APScheduler sweep (`scheduler.py`) dispatches automatically once it
elapses.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from praman.config import COOLING_OFF_WINDOW_DEMO_MODE_S, COOLING_OFF_WINDOW_S


def cooling_off_window_seconds(*, demo_mode: bool) -> int:
    return COOLING_OFF_WINDOW_DEMO_MODE_S if demo_mode else COOLING_OFF_WINDOW_S


def compute_cooling_off_until(now: datetime, *, demo_mode: bool) -> datetime:
    return now + timedelta(seconds=cooling_off_window_seconds(demo_mode=demo_mode))


def is_cooling_off_expired(cooling_off_until: datetime, now: datetime) -> bool:
    return now >= cooling_off_until


def buyer_undo_message(
    item_summary: str, amount_paise: int, merchant_name: str, window_minutes: int
) -> str:
    return (
        f"Your assistant ordered *{item_summary} — ₹{amount_paise / 100:,.2f}* from "
        f"{merchant_name}. Tap to cancel within {window_minutes} minutes.\n"
        "Reply CANCEL to cancel this order."
    )
