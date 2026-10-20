"""Signatures used between the platform and URBAN Edge devices.

Two kinds of device key, both first-class. Ed25519 is the default (a file on the unit, and the key of the gateway and
the root). A device with a TPM 2.0 keeps its key inside the chip, and the TPM cannot do Ed25519, so that key is ECDSA
P-256. The public key text tells the kind: 64 hex = Ed25519, 130 hex = the uncompressed SEC1 point of P-256 (04 || X ||
Y). A P-256 signature is ECDSA over SHA-256 of the same canonical bytes, sent as hex of the DER signature.

Two things are signed, independently of the TLS channel underneath:
- a device proves who it is by signing the challenge of the platform with its own key;
- every command is signed by the gateway key, so a device obeys only the real gateway even
  if the channel is terminated or tampered with somewhere in between.
"""
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

RAW = (serialization.Encoding.Raw, serialization.PublicFormat.Raw)
POINT = (serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
ED25519, P256 = "ed25519", "tpm-p256"  # kinds of device key (the Signer.kind of devices/signer.py)
ED25519_HEX, P256_HEX = 64, 130        # length of the public key text of each kind


def canonical(obj) -> bytes:
    """One byte representation for signing: sorted keys, no spaces."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_hex(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption()).hex()


def public_hex(key) -> str:
    """The public key text of an Ed25519 or P-256 key (private or public object)."""
    if isinstance(key, (ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey)):
        return p256_point_hex(key.public_key() if isinstance(key, ec.EllipticCurvePrivateKey) else key)
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(*RAW).hex()


def load_private(text: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(text.strip()))


def load_public(text: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(text.strip()))


def new_p256_key() -> ec.EllipticCurvePrivateKey:
    """A software P-256 key (tests, tools); a real device's P-256 key is made inside its TPM and never exists as bytes."""
    return ec.generate_private_key(ec.SECP256R1())


def p256_point_hex(public: ec.EllipticCurvePublicKey) -> str:
    return public.public_bytes(*POINT).hex()


def public_kind(text: str) -> str:
    """The kind of device key a public key text is, by its length: ED25519 or P256. ValueError for anything else."""
    text = str(text).strip().lower()
    if len(text) == ED25519_HEX and all(ch in "0123456789abcdef" for ch in text):
        return ED25519
    if len(text) == P256_HEX and text.startswith("04") and all(ch in "0123456789abcdef" for ch in text):
        return P256
    raise ValueError("a device's public key is 64 hex characters (Ed25519) or 130 starting with 04 (TPM, P-256)")


def load_device_public(text: str) -> Ed25519PublicKey | ec.EllipticCurvePublicKey:
    """The public key of a device of either kind. ValueError if the text is neither (wrong length, not hex, a point
    that is not on the curve)."""
    kind = public_kind(text)
    raw = bytes.fromhex(str(text).strip())
    if kind == ED25519:
        return Ed25519PublicKey.from_public_bytes(raw)
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)


def check_public(text: str) -> str:
    """The public key text normalised (trimmed, lower case) if it is a valid device key of either kind, else ValueError."""
    load_device_public(text)
    return str(text).strip().lower()


def fingerprint(public: str) -> str:
    """Short form of a public key (hex) to compare by eye or over the phone: XXXX-XXXX-XXXX-XXXX."""
    digest = hashlib.sha256(bytes.fromhex(public.strip())).hexdigest()[:16].upper()
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def sign(key, obj) -> str:
    """Sign as a key: an Ed25519 or P-256 key object, or a devices.signer.Signer (a file key or a key in a TPM)."""
    if isinstance(key, Ed25519PrivateKey):
        return key.sign(canonical(obj)).hex()
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return key.sign(canonical(obj), ec.ECDSA(hashes.SHA256())).hex()
    return key.sign(obj)  # a Signer


def verify(public, obj, signature: str) -> bool:
    """Is `signature` (hex) this key's signature of obj? `public` is an Ed25519 or P-256 public key object. A signature of
    the other kind, malformed hex or garbage is simply False."""
    try:
        data = canonical(obj)
        sig = bytes.fromhex(signature)
        if isinstance(public, Ed25519PublicKey):
            public.verify(sig, data)
        elif isinstance(public, ec.EllipticCurvePublicKey):
            public.verify(sig, data, ec.ECDSA(hashes.SHA256()))
        else:
            return False
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_text(public_text: str, obj, signature: str) -> bool:
    """verify() for a public key given as text (64 or 130 hex): False, never an exception, for a bad key or signature."""
    try:
        public = load_device_public(public_text)
    except (ValueError, TypeError):
        return False
    return verify(public, obj, signature)
