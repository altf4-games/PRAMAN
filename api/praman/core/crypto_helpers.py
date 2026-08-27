"""Small canonical-hashing helpers shared across core/ modules — kept
separate from `crypto/canonical.py` (the RFC 8785 primitive itself) so
each domain object's "what goes into the hash" decision lives next to the
thing it hashes, not buried in the generic crypto module.
"""

from __future__ import annotations

from praman.crypto.canonical import canonical_hash
from praman.models import CartMandate


def cart_mandate_hash(cart: CartMandate) -> str:
    """The hash embedded in a Razorpay order's `notes` (CLAUDE.md §6) —
    lets a dispute pack prove exactly which cart a given payment paid for,
    independent of Razorpay's own records."""
    return canonical_hash(
        {
            "cart_id": cart.cart_id,
            "envelope_id": cart.envelope_id,
            "agent_did": cart.agent_did,
            "items": cart.items,
            "total_paise": cart.total_paise,
            "band": cart.band,
        }
    )
