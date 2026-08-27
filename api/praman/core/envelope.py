"""The Intent Envelope — modelled on UPI Reserve Pay semantics (one-time
consent, a ceiling, instant revocability), not AP2's card-centric flow.
AP2 vocabulary ("envelope", "mandate") is kept in code so interop with that
ecosystem stays legible, but the actual semantics follow Indian rails.

`verify_cart_within_envelope` is the most important function in the repo
(CLAUDE.md §4): pure, no I/O, no exceptions for control flow, `now`
injected. It operates on the plain dataclasses below rather than the
SQLAlchemy `IntentEnvelope`/`CartMandate` rows directly, so it's trivially
unit-testable without a database — `envelope_from_row` / `cart_from_mandate`
do the ORM-to-dataclass conversion at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from praman.core.gate_types import GateResult, allow


@dataclass(frozen=True, slots=True)
class CartItem:
    sku: str
    category: str
    qty: int
    unit_price_paise: int

    @property
    def total_paise(self) -> int:
        return self.qty * self.unit_price_paise


@dataclass(frozen=True, slots=True)
class Cart:
    agent_did: str
    items: tuple[CartItem, ...]

    @property
    def total_paise(self) -> int:
        return sum(item.total_paise for item in self.items)


@dataclass(frozen=True, slots=True)
class Envelope:
    agent_did: str
    revoked_at: datetime | None
    valid_from: datetime
    valid_until: datetime
    allowed_categories: tuple[str, ...]
    max_single_txn_paise: int
    ceiling_paise: int
    spent_paise: int


def verify_cart_within_envelope(cart: Cart, env: Envelope, now: datetime) -> GateResult:
    """Ordered; first failure wins. See CLAUDE.md §4 for the exact rule
    order — R04's own internal ordering, checked before R05 onward in the
    full gate (Phase 5)."""
    if env.revoked_at is not None:
        return GateResult(
            decision="BLOCK",
            reason_code="ENVELOPE_REVOKED",
            detail=f"envelope was revoked at {env.revoked_at.isoformat()}.",
            remedy="Obtain a fresh, unrevoked envelope from the user.",
            rule_id="R04",
        )

    if not (env.valid_from <= now <= env.valid_until):
        return GateResult(
            decision="BLOCK",
            reason_code="ENVELOPE_EXPIRED",
            detail=f"envelope is valid {env.valid_from.isoformat()} to "
            f"{env.valid_until.isoformat()}; now is {now.isoformat()}.",
            remedy="Obtain a fresh envelope covering the current time.",
            rule_id="R04",
        )

    if cart.agent_did != env.agent_did:
        return GateResult(
            decision="BLOCK",
            reason_code="AGENT_MISMATCH",
            detail=f"cart.agent_did {cart.agent_did!r} != envelope.agent_did {env.agent_did!r}.",
            remedy="Use the envelope issued to this specific agent.",
            rule_id="R04",
        )

    disallowed = sorted({item.category for item in cart.items} - set(env.allowed_categories))
    if disallowed:
        return GateResult(
            decision="BLOCK",
            reason_code="CATEGORY_DENIED",
            detail=f"categories not allowed by this envelope: {disallowed}.",
            remedy="Remove disallowed items, or obtain an envelope covering these categories.",
            rule_id="R04",
        )

    if cart.total_paise > env.max_single_txn_paise:
        return GateResult(
            decision="BLOCK",
            reason_code="SINGLE_TXN_EXCEEDED",
            detail=f"cart total {cart.total_paise} exceeds max_single_txn_paise "
            f"{env.max_single_txn_paise}.",
            remedy="Reduce the cart total, or obtain an envelope with a higher per-transaction limit.",
            rule_id="R04",
        )

    if env.spent_paise + cart.total_paise > env.ceiling_paise:
        return GateResult(
            decision="BLOCK",
            reason_code="ENVELOPE_CEILING_EXCEEDED",
            detail=f"spent {env.spent_paise} + cart {cart.total_paise} exceeds ceiling "
            f"{env.ceiling_paise}.",
            remedy="Reduce the cart total, or obtain a fresh envelope with remaining headroom.",
            rule_id="R04",
        )

    return allow(detail="cart is within the envelope", rule_id="R04")
