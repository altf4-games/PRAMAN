"""Shared error types. Every rejection in this system carries a
`reason_code`, `detail`, and `remedy` — this is a non-negotiable rule
(the design spec §0), not a convention to follow when convenient.
"""

from __future__ import annotations


class PramanError(Exception):
    """Base for all domain errors. `reason_code` is a stable machine-readable
    string (e.g. `ENVELOPE_REVOKED`); `detail` is human-readable context;
    `remedy` tells the caller what to do about it.
    """

    def __init__(self, reason_code: str, detail: str, remedy: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        self.remedy = remedy
        super().__init__(f"{reason_code}: {detail}")


class LedgerIntegrityError(PramanError):
    """Raised when the hash chain fails to verify."""


class SignatureError(PramanError):
    """Raised on Ed25519 / canonicalization signature failures."""
