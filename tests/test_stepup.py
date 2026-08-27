from __future__ import annotations

from datetime import UTC, datetime, timedelta

from praman.config import MERCHANT_APPROVAL_TIMEOUT_S
from praman.core.stepup import (
    approval_deadline,
    generate_stepup_token,
    is_approval_expired,
    merchant_approval_message,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_generate_stepup_token_is_unique() -> None:
    a = generate_stepup_token()
    b = generate_stepup_token()
    assert a != b
    assert len(a) == 32


def test_approval_deadline() -> None:
    deadline = approval_deadline(NOW)
    assert deadline == NOW + timedelta(seconds=MERCHANT_APPROVAL_TIMEOUT_S)


def test_is_approval_expired_boundary() -> None:
    deadline = NOW + timedelta(seconds=60)
    assert not is_approval_expired(deadline, NOW)
    assert is_approval_expired(deadline, deadline)


def test_merchant_approval_message_contains_key_details() -> None:
    message = merchant_approval_message("Engraved Ring", 4200000, "This item cannot be returned.")
    assert "Engraved Ring" in message
    assert "42,000.00" in message
    assert "cannot be returned" in message
    assert "APPROVE" in message
    assert "DECLINE" in message
