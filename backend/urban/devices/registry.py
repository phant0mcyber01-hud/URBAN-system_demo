"""Device registry: which URBAN Edge units exist, where they are installed and whether they are trusted.

In a deployment a unit generates its own key (python -m urban.edge --keygen; later inside its
secure element) and only the public half is enrolled here (python -m urban.provision enroll).
The demo instead generates every unit's key on one machine under data/pki, which is not in git.
The registry file is re-read when another process changes it, so enrolment works while the
platform runs.
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
        self.devices: dict[str, dict] = {}
        self._by_tls: dict[str, dict] = {}
        self._stamp = None
        self.refresh()

    def _load(self):
        self.devices = {d["id"]: d for d in json.loads(self.path.read_text(encoding="utf-8"))}
        self._by_tls = {d["tls"]: d for d in self.devices.values()}

    def refresh(self) -> set[str]:
        """Re-read the file if another process changed it. Returns the ids whose key or status changed,
        or that appeared or disappeared: their open sessions must not continue on the old trust."""
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return set()
        if stamp == self._stamp:
            return set()
        before = {i: (d["public_key"], d["status"]) for i, d in self.devices.items()}
        try:
            self._load()
        except (OSError, ValueError):
            return set()  # half-written: next time
        self._stamp = stamp
        after = {i: (d["public_key"], d["status"]) for i, d in self.devices.items()}
        return {i for i in before.keys() | after.keys() if before.get(i) != after.get(i)}

    def _save(self):
        save(self.path, self.devices)
        st = self.path.stat()
        self._stamp = (st.st_mtime_ns, st.st_size)
        self._by_tls = {d["tls"]: d for d in self.devices.values()}

    def by_tls(self, tls: str) -> dict | None:
        return self._by_tls.get(tls)

    def enroll(self, device: str, junction: dict, public: str, serial: str = "") -> dict:
        """Enrol a unit's public key for a junction ({id, tls, name}); a known unit gets the new key (replaced box).
        The private key never comes here."""
        crypto.load_public(public)  # raises ValueError if it is not an Ed25519 public key
        if not device.startswith("edge-") or len(device) > 60:
            raise ValueError("device id must look like edge-<junction>")
        other = self._by_tls.get(junction["tls"])
        if other is not None and other["id"] != device:
            raise ValueError(f"junction {junction['id']} already has unit {other['id']}; revoke and remove it first")
        rec = self.devices.get(device, {})
        rec.update({"id": device, "junction": junction["id"], "tls": junction["tls"], "name": junction.get("name", ""),
                    "public_key": public.strip().lower(), "status": ACTIVE,
                    "serial": serial or rec.get("serial") or f"URB-{secrets.token_hex(3).upper()}",
                    "enrolled": datetime.date.today().isoformat()})
        self.devices[device] = rec
        self._save()
        return rec

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
            self._save()
        return rec


def save(path: Path, devices: dict[str, dict]):
    path.write_text(json.dumps(list(devices.values()), ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- demo provisioning

def server_names(pki: Path) -> set[str]:
    """Host names on the device listener's certificate."""
    cert = x509.load_pem_x509_certificate((pki / "server.pem").read_bytes())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return set(san.get_values_for_type(x509.DNSName))


def _write_tls(pki: Path, host: str | None = None):
    """A private CA (kept, if one exists) and a server certificate for localhost and `host`: devices pin the CA.
    The CA key stays in ca.key so the server certificate can be reissued for a new host name; in a
    deployment it is moved off the server once the certificate is issued."""
    now = datetime.datetime.now(datetime.timezone.utc)
    pem = serialization.Encoding.PEM
    if (pki / "ca.key").exists() and (pki / "ca.pem").exists():
        ca_key = serialization.load_pem_private_key((pki / "ca.key").read_bytes(), None)
        ca_name = x509.load_pem_x509_certificate((pki / "ca.pem").read_bytes()).subject
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "URBAN device CA")])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
              .not_valid_after(now + datetime.timedelta(days=3650))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .sign(ca_key, hashes.SHA256()))
        (pki / "ca.pem").write_bytes(ca.public_bytes(pem))
        (pki / "ca.key").write_bytes(ca_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    names = [x509.DNSName(n) for n in dict.fromkeys(["localhost", *([host] if host else [])])]
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "URBAN device gateway")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([*names, x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    (pki / "server.pem").write_bytes(cert.public_bytes(pem))
    (pki / "server.key").write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def provision(junctions: list[dict], pki: Path, host: str | None = None, demo_devices: bool = True) -> bool:
    """Create whatever is missing: TLS material, the gateway signing key, one identity per junction.
    host: the name devices connect to; a server certificate that does not cover it is reissued.
    demo_devices: also generate every unit's private key here (demo only). Without it the registry only
    holds what was enrolled; real units bring their own keys.

    Returns True if anything was created. Existing devices keep their keys and status.
    """
    (pki / "edge").mkdir(parents=True, exist_ok=True)
    created = False
    if not (pki / "server.pem").exists():
        _write_tls(pki, host)
        created = True
    elif host and host not in server_names(pki):
        if (pki / "ca.key").exists():
            _write_tls(pki, host)
            created = True
        else:
            print(f"pki: the server certificate does not cover {host!r} and ca.key is not here to reissue it; "
                  f"bring ca.key back or start a new pki folder (devices then need the new ca.pem)", flush=True)
    if not (pki / "gateway.key").exists():
        key = crypto.new_key()
        (pki / "gateway.key").write_text(crypto.private_hex(key))
        (pki / "gateway.pub").write_text(crypto.public_hex(key))
        created = True

    path = pki / "devices.json"
    devices = {d["id"]: d for d in json.loads(path.read_text(encoding="utf-8"))} if path.exists() else {}
    if not demo_devices:
        if not path.exists():
            save(path, devices)
            created = True
        return created
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
