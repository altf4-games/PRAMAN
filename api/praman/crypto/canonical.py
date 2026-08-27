"""RFC 8785 (JCS) canonicalization, and the sha256-over-canonical-JSON
primitive used throughout the ledger and every signed structure (quotes,
envelopes, cart mandates).
"""

from __future__ import annotations

import hashlib
from typing import Any

import rfc8785


def canonicalize(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON bytes per RFC 8785. Two dicts with the same keys
    and values — regardless of insertion order — canonicalize identically,
    which is exactly the property signatures and hash chaining depend on.
    """
    return rfc8785.dumps(payload)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(payload: dict[str, Any]) -> str:
    """sha256(jcs(payload)) as a hex string — the building block for
    `payload_hash` in the ledger and for everything we sign."""
    return sha256_hex(canonicalize(payload))
