"""Step-up: when the gate ESCALATEs (R08 or R11), a merchant gets a
WhatsApp Approve/Decline. `stepup_token` binds that WhatsApp reply back to
the specific order it approves — CLAUDE.md §8's threat model entry
"WhatsApp approval spoofing": a Twilio-signature-verified message from the
merchant's own registered number, containing this single-use token, is
what authorizes the re-run — not the message text alone.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from praman.config import MERCHANT_APPROVAL_TIMEOUT_S


def generate_stepup_token() -> str:
    return uuid.uuid4().hex


def approval_deadline(now: datetime) -> datetime:
    return now + timedelta(seconds=MERCHANT_APPROVAL_TIMEOUT_S)


def is_approval_expired(deadline: datetime, now: datetime) -> bool:
    return now >= deadline


def merchant_approval_message(item_summary: str, amount_paise: int, reason: str) -> str:
    return (
        f"An AI agent wants to buy *{item_summary} — ₹{amount_paise / 100:,.2f}* "
        f"for a customer.\n{reason}\nReply APPROVE or DECLINE."
    )
