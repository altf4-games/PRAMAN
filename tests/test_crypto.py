from __future__ import annotations

from praman.crypto.canonical import canonical_hash, canonicalize
from praman.crypto.did import did_from_public_key, public_key_from_did
from praman.crypto.keys import (
    decrypt_private_key,
    encrypt_private_key,
    generate_keypair,
    sign,
    verify,
)


def test_canonicalize_is_order_independent() -> None:
    a = canonicalize({"b": 1, "a": 2})
    b = canonicalize({"a": 2, "b": 1})
    assert a == b == b'{"a":2,"b":1}'


def test_canonical_hash_is_deterministic() -> None:
    assert canonical_hash({"x": 1}) == canonical_hash({"x": 1})
    assert canonical_hash({"x": 1}) != canonical_hash({"x": 2})


def test_keypair_sign_and_verify_roundtrip() -> None:
    priv, pub = generate_keypair()
    message = b"hello praman"
    sig = sign(priv, message)
    assert verify(pub, message, sig)


def test_verify_rejects_tampered_message() -> None:
    priv, pub = generate_keypair()
    sig = sign(priv, b"original")
    assert not verify(pub, b"tampered", sig)


def test_verify_rejects_wrong_key() -> None:
    priv, _pub = generate_keypair()
    _priv2, pub2 = generate_keypair()
    sig = sign(priv, b"hello")
    assert not verify(pub2, b"hello", sig)


def test_private_key_encryption_roundtrip() -> None:
    priv, _pub = generate_keypair()
    enc = encrypt_private_key(priv, "secret-1")
    assert enc != priv
    assert decrypt_private_key(enc, "secret-1") == priv


def test_did_roundtrip() -> None:
    _priv, pub = generate_keypair()
    did = did_from_public_key(pub)
    assert did.startswith("did:key:z")
    assert public_key_from_did(did) == pub
