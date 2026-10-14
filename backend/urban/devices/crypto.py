"""Signatures used between the platform and URBAN Edge devices (Ed25519).

Two things are signed, independently of the TLS channel underneath:
- a device proves who it is by signing the challenge of the platform with its own key;
- every command is signed by the gateway key, so a device obeys only the real gateway even
  if the channel is terminated or tampered with somewhere in between.
"""
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

RAW = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def canonical(obj) -> bytes:
    """One byte representation for signing: sorted keys, no spaces."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_hex(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption()).hex()


def public_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(*RAW).hex()


def load_private(text: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(text.strip()))


def load_public(text: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(text.strip()))


def fingerprint(public: str) -> str:
    """Short form of a public key (hex) to compare by eye or over the phone: XXXX-XXXX-XXXX-XXXX."""
    digest = hashlib.sha256(bytes.fromhex(public.strip())).hexdigest()[:16].upper()
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def sign(key: Ed25519PrivateKey, obj) -> str:
    return key.sign(canonical(obj)).hex()


def verify(public: Ed25519PublicKey, obj, signature: str) -> bool:
    try:
        public.verify(bytes.fromhex(signature), canonical(obj))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
