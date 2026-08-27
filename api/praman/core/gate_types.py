"""Shared result type for every gate-adjacent decision — the envelope
check (this phase), quote verification (this phase), and the full R01-R12
policy gate (Phase 5) all return this same shape. One type now avoids a
circular-import shuffle later when gate.py composes envelope.py and
quotes.py's results into its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Decision = Literal["ALLOW", "BLOCK", "HOLD", "ESCALATE", "SUBSTITUTE"]


@dataclass(frozen=True, slots=True)
class GateResult:
    """Every rejection carries a reason_code, detail, and remedy —
    the design spec's non-negotiable rule, enforced by this being the only
    result shape any gate-adjacent function returns."""

    decision: Decision
    reason_code: str
    detail: str
    remedy: str
    rule_id: str | None = None


def allow(detail: str = "ok", rule_id: str | None = None) -> GateResult:
    return GateResult(decision="ALLOW", reason_code="OK", detail=detail, remedy="", rule_id=rule_id)
