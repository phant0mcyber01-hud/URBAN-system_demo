"""Which gateway key a unit obeys, and how that key is replaced.

A root key, kept off the server like the CA key, signs a certificate for the gateway key:
{gateway: public key, epoch: n, issued: date}. Units are installed with the root's public key and
take the gateway key from the certificate the platform presents at session start. They remember
the highest epoch they have accepted and refuse anything lower, so after a rotation (a leaked
gateway key, a routine change) the old key is dead for every unit that has seen the new one,
and no one holding only the old key can talk a unit back to it.

    python -m urban.provision rotate-gateway [--root /media/usb/root.key]
"""
import datetime
import json
from pathlib import Path

from . import crypto


def issue(root_key, gateway_public: str, epoch: int) -> dict:
    body = {"gateway": gateway_public.strip().lower(), "epoch": int(epoch), "issued": datetime.date.today().isoformat()}
    return {**body, "sig": crypto.sign(root_key, body)}


def valid(root_public, cert) -> bool:
    if not isinstance(cert, dict) or not isinstance(cert.get("epoch"), int) or not isinstance(cert.get("gateway"), str):
        return False
    body = {k: cert.get(k) for k in ("gateway", "epoch", "issued")}
    if not crypto.verify(root_public, body, str(cert.get("sig"))):
        return False
    try:
        crypto.load_public(cert["gateway"])
    except ValueError:
        return False
    return True


def ensure(pki: Path) -> bool:
    """Root key and a certificate for the current gateway key, for folders made before rotation existed.
    Returns True if anything was created."""
    created = False
    if not (pki / "root.pub").exists():
        root = crypto.new_key()
        (pki / "root.key").write_text(crypto.private_hex(root))
        (pki / "root.pub").write_text(crypto.public_hex(root))
        created = True
    if not (pki / "gateway.cert.json").exists() and (pki / "root.key").exists():
        root = crypto.load_private((pki / "root.key").read_text())
        cert = issue(root, (pki / "gateway.pub").read_text(), 1)
        (pki / "gateway.cert.json").write_text(json.dumps(cert, indent=1))
        created = True
    return created


def rotate(pki: Path, root_key_path: Path | None = None) -> dict:
    """A new gateway key with the next epoch. Needs the root key (bring it back from where it is kept)."""
    root_path = Path(root_key_path or pki / "root.key")
    if not root_path.exists():
        raise FileNotFoundError(f"{root_path}: the root key is needed to rotate the gateway key")
    root = crypto.load_private(root_path.read_text())
    if crypto.public_hex(root) != (pki / "root.pub").read_text().strip():
        raise ValueError("this root key does not match root.pub")
    old = json.loads((pki / "gateway.cert.json").read_text())
    key = crypto.new_key()
    cert = issue(root, crypto.public_hex(key), old["epoch"] + 1)
    (pki / "gateway.key").write_text(crypto.private_hex(key))
    (pki / "gateway.pub").write_text(crypto.public_hex(key))
    (pki / "gateway.cert.json").write_text(json.dumps(cert, indent=1))
    return cert


class GatewayTrust:
    """A unit's view: the gateway key it obeys now and the epoch it will not go below."""

    def __init__(self, root_public=None, fallback_public=None, state: Path | None = None):
        """root_public: the installed root key (None: no rotation, obey fallback_public forever);
        fallback_public: gateway.pub, for units that have not seen a certificate yet;
        state: where the accepted certificate is kept across restarts."""
        self.root, self.state = root_public, state
        self.public, self.epoch = fallback_public, 0
        if root_public is not None and state is not None and state.exists():
            try:
                cert = json.loads(state.read_text())
            except (OSError, ValueError):
                cert = None
            if valid(root_public, cert):
                self.public, self.epoch = crypto.load_public(cert["gateway"]), cert["epoch"]

    def offer(self, cert) -> str | None:
        """A certificate from the platform. Returns None if fine (adopted or the same), else why it is refused."""
        if self.root is None:
            return None  # this unit predates rotation: it keeps its fixed gateway key
        if cert is None:
            return None if self.public is not None else "the platform presented no gateway certificate"
        if not valid(self.root, cert):
            return "gateway certificate not signed by the root key"
        if cert["epoch"] < self.epoch:
            return f"gateway key epoch {cert['epoch']} is older than {self.epoch}: refusing to go back"
        if cert["epoch"] > self.epoch or self.public is None:
            self.public, self.epoch = crypto.load_public(cert["gateway"]), cert["epoch"]
            if self.state is not None:
                self.state.write_text(json.dumps(cert))
        return None
