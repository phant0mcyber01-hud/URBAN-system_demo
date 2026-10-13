"""Device registry: which URBAN Edge units exist, where they are installed and whether they are trusted.

Also the demo provisioning: keys and certificates for the local setup. In a deployment a
device generates its key inside its secure element and only the public half is enrolled;
here everything is generated on one machine and kept under data/pki, which is not in git.
"""
import datetime
import ipaddress
import json
import secrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import crypto

ACTIVE, REVOKED = "active", "revoked"


def device_id(junction_id: str) -> str:
    return f"edge-{junction_id}"


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.devices: dict[str, dict] = {d["id"]: d for d in json.loads(path.read_text(encoding="utf-8"))}
        self._by_tls = {d["tls"]: d for d in self.devices.values()}

    def by_tls(self, tls: str) -> dict | None:
        return self._by_tls.get(tls)

    def authenticate(self, device: str, nonce: str, signature: str) -> dict | None:
        """The enrolled, non-revoked device that signed this challenge, or None."""
        rec = self.devices.get(device)
        if rec is None or rec["status"] != ACTIVE:
            return None
        ok = crypto.verify(crypto.load_public(rec["public_key"]), {"device": device, "nonce": nonce}, signature)
        return rec if ok else None

    def set_status(self, device: str, status: str) -> dict | None:
        rec = self.devices.get(device)
        if rec is not None and status in (ACTIVE, REVOKED):
            rec["status"] = status
            save(self.path, self.devices)
        return rec


def save(path: Path, devices: dict[str, dict]):
    path.write_text(json.dumps(list(devices.values()), ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- demo provisioning

def _write_tls(pki: Path):
    """A private CA and a server certificate for localhost: devices pin the CA."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "URBAN demo device CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "URBAN device gateway")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    (pki / "ca.pem").write_bytes(ca.public_bytes(pem))
    (pki / "server.pem").write_bytes(cert.public_bytes(pem))
    (pki / "server.key").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def provision(junctions: list[dict], pki: Path) -> bool:
    """Create whatever is missing: TLS material, the gateway signing key, one identity per junction.

    Returns True if anything was created. Existing devices keep their keys and status.
    """
    (pki / "edge").mkdir(parents=True, exist_ok=True)
    created = False
    if not (pki / "server.pem").exists():
        _write_tls(pki)
        created = True
    if not (pki / "gateway.key").exists():
        key = crypto.new_key()
        (pki / "gateway.key").write_text(crypto.private_hex(key))
        (pki / "gateway.pub").write_text(crypto.public_hex(key))
        created = True

    path = pki / "devices.json"
    devices = {d["id"]: d for d in json.loads(path.read_text(encoding="utf-8"))} if path.exists() else {}
    for j in junctions:
        ident = device_id(j["id"])
        if ident in devices and (pki / "edge" / f"{ident}.key").exists():
            rec = devices[ident]
            if not rec.get("serial") or not rec.get("enrolled"):  # enrolled before these fields existed
                rec.setdefault("serial", f"URB-{secrets.token_hex(3).upper()}")
                rec.setdefault("enrolled", datetime.date.today().isoformat())
                created = True
            continue
        key = crypto.new_key()
        (pki / "edge" / f"{ident}.key").write_text(crypto.private_hex(key))
        devices[ident] = {"id": ident, "junction": j["id"], "tls": j["tls"], "name": j["name"],
                          "public_key": crypto.public_hex(key), "status": ACTIVE,
                          "serial": f"URB-{secrets.token_hex(3).upper()}",  # what is printed on the box
                          "enrolled": datetime.date.today().isoformat()}
        created = True
    if created:
        save(path, devices)
    return created
