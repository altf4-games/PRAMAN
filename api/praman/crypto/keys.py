"""Ed25519 keypair generation, signing, and verification.

Keys are stored as raw bytes, hex-encoded for transport/storage. Private
keys at rest (`Merchant.private_key_enc`) are Fernet-encrypted with a
server-side secret — never stored or transmitted in the clear.
"""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from praman.errors import SignatureError


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key_hex, public_key_hex)."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    priv_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes.hex(), pub_bytes.hex()


def sign(private_key_hex: str, message: bytes) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private_key.sign(message).hex()


def verify(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError):
        # InvalidSignature: well-formed key/signature, verification failed.
        # ValueError: malformed hex or wrong-length key/signature bytes.
        return False


def verify_or_raise(public_key_hex: str, message: bytes, signature_hex: str) -> None:
    if not verify(public_key_hex, message, signature_hex):
        raise SignatureError(
            reason_code="AGENT_SIG_INVALID",
            detail="Ed25519 signature did not verify against the provided public key.",
            remedy="Re-sign the request with the correct private key and retry.",
        )


def derive_fernet_key(app_secret: str) -> bytes:
    """Derive a Fernet-compatible key from an arbitrary secret string."""
    import base64
    import hashlib

    digest = hashlib.sha256(app_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_private_key(private_key_hex: str, app_secret: str) -> str:
    f = Fernet(derive_fernet_key(app_secret))
    return f.encrypt(private_key_hex.encode()).decode()


def decrypt_private_key(private_key_enc: str, app_secret: str) -> str:
    f = Fernet(derive_fernet_key(app_secret))
    try:
        return f.decrypt(private_key_enc.encode()).decode()
    except InvalidToken as exc:
        raise SignatureError(
            reason_code="KEY_DECRYPT_FAILED",
            detail="Could not decrypt stored private key with the configured app secret.",
            remedy="Verify APP_SECRET matches the value used at key-generation time.",
        ) from exc
