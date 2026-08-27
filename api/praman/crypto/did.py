"""DID generation. Merchants and agents identify by a `did:key` — the
`did:key` method (https://w3c-ccg.github.io/did-method-key/) encodes the
Ed25519 public key directly in the identifier, so resolution needs no
registry lookup. This is what `AgentRegistry.resolve()` (Phase 4) will
eventually verify against a real registry (NPCI's UAP, once it ships);
locally it's just a deterministic function of the public key.
"""

from __future__ import annotations

# multicodec prefix for Ed25519 public keys (0xed01), varint-encoded.
_ED25519_MULTICODEC_PREFIX = bytes([0xED, 0x01])


def _base58_encode(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(data, "big")
    encoded = ""
    while n > 0:
        n, rem = divmod(n, 58)
        encoded = alphabet[rem] + encoded
    n_pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * n_pad + encoded


def did_from_public_key(public_key_hex: str) -> str:
    """did:key:z<base58btc(multicodec-prefix || raw pubkey bytes)>"""
    raw = bytes.fromhex(public_key_hex)
    multicodec = _ED25519_MULTICODEC_PREFIX + raw
    return f"did:key:z{_base58_encode(multicodec)}"


def public_key_from_did(did: str) -> str:
    """Inverse of `did_from_public_key` — extracts the hex public key."""
    if not did.startswith("did:key:z"):
        raise ValueError(f"not a did:key identifier: {did}")
    encoded = did.removeprefix("did:key:z")
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = 0
    for char in encoded:
        n = n * 58 + alphabet.index(char)
    n_pad = len(encoded) - len(encoded.lstrip("1"))
    raw_len = (n.bit_length() + 7) // 8
    data = n.to_bytes(raw_len, "big") if n > 0 else b""
    data = b"\x00" * n_pad + data
    multicodec = data[len(_ED25519_MULTICODEC_PREFIX) :]
    return multicodec.hex()
