"""Device registry: which URBAN Edge units exist, where they are installed and whether they are trusted.

In a deployment a unit generates its own key (python -m urban.edge --keygen; inside its TPM when
it has one: ECDSA P-256, else an Ed25519 file) and only the public half is enrolled here (python -m urban.provision enroll).
The demo instead generates every unit's key on one machine under data/pki, which is not in git.
The registry file is re-read when another process changes it, so enrolment works while the
platform runs.

Units and district servers also keep their WireGuard tunnel here (urban/net/wireguard.py, docs/wireguard-design.md),
in optional fields that old records simply lack ("not yet"):
- district: wg_index (its tunnel number, given once), wg_public (its accepted tunnel key), wg_pending (a different
  key it reported, waiting for an admin), wg_mode ("allow" or "only"; absent is "allow"), wg_retired (unit addresses
  of this district that are never given again);
- unit: wg_ip (10.77.<district>.<n>, given once), wg_public, wg_pending.
The numbers are given lazily and kept: a district's when its own key is accepted or a unit of it needs an address,
a unit's when it has an accepted key and an active district. Only retirement takes a unit's address away.
"""
import datetime
import ipaddress
import json
import math
import os
import secrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ..net import wireguard
from . import crypto, trust
from .enrolment import CABINET, DEVICE, LIGHT_DEVICE, SERIAL, SITE, WATER_DEVICE, check_address

ACTIVE, REVOKED = "active", "revoked"
UNIT, DISTRICT, LIGHTING, WATER = "unit", "district", "lighting", "water"  # kinds of record: a junction's unit, a
# district server units connect through, the unit in a street-lighting cabinet (the lighting module,
# urban/modules/lighting), or the unit in a pump station (the water module, urban/modules/water: it only reads)
WG_MODES = ("allow", "only")  # a district's relay: still open to units outside the tunnel, or only through it


def device_id(junction_id: str) -> str:
    return f"edge-{junction_id}"


def kind_of(rec: dict) -> str:
    return rec.get("kind") or UNIT


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.devices: dict[str, dict] = {}
        self._by_tls: dict[str, dict] = {}
        self._stamp = None
        self._changed: set[str] = set()  # changes picked up inside a write, still to be reported by refresh()
        self.refresh()

    def _load(self):
        self.devices = {d["id"]: d for d in json.loads(self.path.read_text(encoding="utf-8"))}
        self._by_tls = {d["tls"]: d for d in self.devices.values() if d.get("tls")}

    def refresh(self) -> set[str]:
        """Re-read the file if another process changed it. Returns the ids whose key or status changed,
        or that appeared or disappeared: their open sessions must not continue on the old trust."""
        self._reload()
        changed, self._changed = self._changed, set()
        return changed

    def _reload(self):
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return
        if stamp == self._stamp:
            return
        before = {i: (d["public_key"], d["status"]) for i, d in self.devices.items()}
        try:
            self._load()
        except (OSError, ValueError):
            return  # half-written: next time
        self._stamp = stamp
        after = {i: (d["public_key"], d["status"]) for i, d in self.devices.items()}
        self._changed |= {i for i in before.keys() | after.keys() if before.get(i) != after.get(i)}

    def _save(self):
        save(self.path, self.devices)
        st = self.path.stat()
        self._stamp = (st.st_mtime_ns, st.st_size)
        self._by_tls = {d["tls"]: d for d in self.devices.values() if d.get("tls")}

    def _usable_district(self, district: str) -> None:
        rec = self.devices.get(district, {})
        if kind_of(rec) != DISTRICT:
            raise ValueError(f"{district} is not an enrolled district server")
        if rec["status"] == REVOKED:
            raise ValueError(f"{district} is revoked: restore it first, or route the unit directly")

    def _retire(self, rec: dict):
        """A unit leaves its district (moved, routed directly, removed): its tunnel address is never given again there,
        so no other unit inherits what that address was trusted with."""
        ip = rec.pop("wg_ip", None)
        old = self.devices.get(rec.get("district") or "")
        if ip and old is not None and kind_of(old) == DISTRICT and ip not in old.get("wg_retired", []):
            old["wg_retired"] = [*old.get("wg_retired", []), ip]

    def _check_key(self, device: str, public: str):
        """One key, one device: the same key under two ids would let one box act as two."""
        for other in self.devices.values():
            if other["id"] != device and other["public_key"] == public.strip().lower():
                raise ValueError(f"this key is already enrolled as {other['id']}")

    def by_tls(self, tls: str) -> dict | None:
        return self._by_tls.get(tls)

    def districts(self) -> list[dict]:
        return [d for d in self.devices.values() if kind_of(d) == DISTRICT]

    def enroll_district(self, device: str, address: str, public: str, serial: str = "") -> dict:
        """Enrol a district server: its public key and the address its junction units connect to (host or
        host:port). A known district gets the new key or address."""
        crypto.check_public(public)  # Ed25519 (64 hex) or a TPM's P-256 (130 hex)
        if not device.startswith("district-") or not DEVICE.fullmatch(device):
            raise ValueError("district id must look like district-<name> (letters, digits, . _ -)")
        address = check_address(address)
        self._reload()  # another process (the CLI) may have written since we read the file
        self._check_key(device, public)
        rec = self.devices.get(device, {})
        if rec.get("public_key") != public.strip().lower():
            rec.pop("first_seen", None)
            _forget_tunnel_key(rec)
        rec.update({"id": device, "kind": DISTRICT, "junction": None, "tls": None, "name": "", "address": address,
                    "public_key": public.strip().lower(), "status": ACTIVE,
                    "serial": serial or rec.get("serial") or f"URB-{secrets.token_hex(3).upper()}",
                    "enrolled": datetime.date.today().isoformat()})
        self.devices[device] = rec
        self._save()
        return rec

    def enroll_lighting(self, device: str, cabinet: str, public: str, serial: str = "",
                        lon: float | None = None, lat: float | None = None) -> dict:
        """Enrol the unit of a street-lighting cabinet: id light-<name>, the cabinet it controls (one active unit per
        cabinet) and optionally where the cabinet stands (the unit's astronomical schedule needs it). A lighting
        record is nobody's junction and goes through no district server. A known device gets the new key."""
        crypto.check_public(public)
        if not isinstance(device, str) or not LIGHT_DEVICE.fullmatch(device):
            raise ValueError("lighting unit id must look like light-<cabinet> (letters, digits, . _ -)")
        if not isinstance(cabinet, str) or not CABINET.fullmatch(cabinet):
            raise ValueError("cabinet is letters, digits, . _ - (up to 40), e.g. C-01")
        if not isinstance(serial, str) or not SERIAL.fullmatch(serial):
            raise ValueError("serial: letters, digits, . _ - (up to 40)")
        for name, value, limit in (("lon", lon, 180), ("lat", lat, 90)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or abs(value) > limit):
                raise ValueError(f"{name} must be a number within +-{limit}")
        if (lon is None) != (lat is None):
            raise ValueError("give both --lon and --lat, or neither")
        self._reload()
        self._check_key(device, public)
        for other in self.devices.values():
            if (other["id"] != device and kind_of(other) == LIGHTING and other.get("cabinet") == cabinet
                    and other["status"] == ACTIVE):
                raise ValueError(f"cabinet {cabinet} already has unit {other['id']}; revoke and remove it first")
        rec = self.devices.get(device, {})
        if rec and kind_of(rec) != LIGHTING:
            raise ValueError(f"{device} is already enrolled as a {kind_of(rec)}")
        if rec.get("public_key") != public.strip().lower():
            rec.pop("first_seen", None)
        rec.update({"id": device, "kind": LIGHTING, "cabinet": cabinet, "lon": lon, "lat": lat, "junction": None,
                    "tls": None, "name": "", "public_key": public.strip().lower(), "status": ACTIVE,
                    "serial": serial or rec.get("serial") or f"URB-{secrets.token_hex(3).upper()}",
                    "enrolled": datetime.date.today().isoformat()})
        self.devices[device] = rec
        self._save()
        return rec

    def enroll_water(self, device: str, site: str, public: str, serial: str = "", lon: float | None = None,
                     lat: float | None = None, p_min: float | None = None, p_max: float | None = None) -> dict:
        """Enrol the unit of a pump station: id pump-<name>, the station it reads (one active unit per station),
        optionally where it stands and the pressure limits (bar) the centre raises alerts against. The limits are the
        centre's, set here by the administrator, never the station's. A water record is nobody's junction, goes
        through no district server and has no tunnel. A known device gets the new key."""
        crypto.check_public(public)
        if not isinstance(device, str) or not WATER_DEVICE.fullmatch(device):
            raise ValueError("pump station unit id must look like pump-<site> (letters, digits, . _ -)")
        if not isinstance(site, str) or not SITE.fullmatch(site):
            raise ValueError("site (the station) is letters, digits, . _ - (up to 40), e.g. S-01")
        if not isinstance(serial, str) or not SERIAL.fullmatch(serial):
            raise ValueError("serial: letters, digits, . _ - (up to 40)")
        for name, value, lo, hi in (("lon", lon, -180, 180), ("lat", lat, -90, 90), ("p_min", p_min, -1, 100),
                                    ("p_max", p_max, -1, 100)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not lo <= value <= hi):
                raise ValueError(f"{name} must be a number from {lo} to {hi}")
        if (lon is None) != (lat is None):
            raise ValueError("give both --lon and --lat, or neither")
        if p_min is not None and p_max is not None and not p_min < p_max:
            raise ValueError("p_min must be below p_max")
        self._reload()
        self._check_key(device, public)
        for other in self.devices.values():
            if (other["id"] != device and kind_of(other) == WATER and other.get("site") == site
                    and other["status"] == ACTIVE):
                raise ValueError(f"site {site} already has unit {other['id']}; revoke and remove it first")
        rec = self.devices.get(device, {})
        if rec and kind_of(rec) != WATER:
            raise ValueError(f"{device} is already enrolled as a {kind_of(rec)}")
        if rec.get("public_key") != public.strip().lower():
            rec.pop("first_seen", None)
        rec.update({"id": device, "kind": WATER, "site": site, "lon": lon, "lat": lat,
                    "p_min": float(p_min) if p_min is not None else None,
                    "p_max": float(p_max) if p_max is not None else None, "junction": None,
                    "tls": None, "name": "", "public_key": public.strip().lower(), "status": ACTIVE,
                    "serial": serial or rec.get("serial") or f"URB-{secrets.token_hex(3).upper()}",
                    "enrolled": datetime.date.today().isoformat()})
        self.devices[device] = rec
        self._save()
        return rec

    def assign(self, device: str, district: str | None) -> dict | None:
        """Route a unit through a district server (its next kit points there), or directly to the centre (None)."""
        self._reload()
        rec = self.devices.get(device)
        if rec is None or kind_of(rec) != UNIT:
            return None
        if district is not None:
            self._usable_district(district)
        if rec.get("district") != district:
            self._retire(rec)
        if district is None:
            rec.pop("district", None)
        else:
            rec["district"] = district
        self._save()
        return rec

    def enroll(self, device: str, junction: dict, public: str, serial: str = "", district: str | None = None) -> dict:
        """Enrol a unit's public key for a junction ({id, tls, name}); a known unit gets the new key (replaced box).
        The private key never comes here. district: the district server it connects through, if any."""
        crypto.check_public(public)  # raises ValueError if it is neither an Ed25519 nor a P-256 public key
        if not device.startswith("edge-") or not DEVICE.fullmatch(device):
            raise ValueError("device id must look like edge-<junction> (letters, digits, . _ -)")
        self._reload()
        if district is not None:
            self._usable_district(district)
        self._check_key(device, public)
        other = self._by_tls.get(junction["tls"])
        if other is not None and other["id"] != device:
            raise ValueError(f"junction {junction['id']} already has unit {other['id']}; revoke and remove it first")
        rec = self.devices.get(device, {})
        if rec.get("public_key") != public.strip().lower():
            rec.pop("first_seen", None)  # a new key is a new box: it has not been seen yet
            _forget_tunnel_key(rec)
        if rec.get("district") != district:
            self._retire(rec)
        rec.update({"id": device, "kind": UNIT, "junction": junction["id"], "tls": junction["tls"],
                    "name": junction.get("name", ""), "public_key": public.strip().lower(), "status": ACTIVE,
                    "serial": serial or rec.get("serial") or f"URB-{secrets.token_hex(3).upper()}",
                    "enrolled": datetime.date.today().isoformat()})
        if district is None:  # routing is set exactly as given: a replaced box may go direct
            rec.pop("district", None)
        else:
            rec["district"] = district
        self.devices[device] = rec
        self._save()
        return rec

    def authenticate(self, device: str, nonce: str, signature: str) -> dict | None:
        """The enrolled, non-revoked device that signed this challenge, or None."""
        rec = self.devices.get(device)
        if rec is None or rec["status"] != ACTIVE:
            return None
        ok = crypto.verify_text(rec["public_key"], {"device": device, "nonce": nonce}, signature)  # either kind of key
        return rec if ok else None

    def mark_seen(self, device: str):
        """The first session of a unit with its current key: installed and talking. Written once per key."""
        self._reload()
        rec = self.devices.get(device)
        if rec is not None and not rec.get("first_seen"):
            rec["first_seen"] = datetime.datetime.now().isoformat(timespec="seconds")
            self._save()

    def remove(self, device: str) -> dict | None:
        """Forget a unit for good. Only a revoked one: removing must never be the first step."""
        self._reload()
        rec = self.devices.get(device)
        if rec is None:
            return None
        if rec["status"] != REVOKED:
            raise ValueError("revoke the unit first")
        del self.devices[device]
        if kind_of(rec) == DISTRICT:  # its units connect to the centre directly from their next kit on
            for other in self.devices.values():
                if other.get("district") == device:
                    other.pop("district", None)
                    other.pop("wg_ip", None)  # the district and its tunnel network are gone
        else:
            self._retire(rec)
        self._save()
        return rec

    def set_status(self, device: str, status: str) -> dict | None:
        self._reload()
        rec = self.devices.get(device)
        if rec is not None and status in (ACTIVE, REVOKED):
            rec["status"] = status
            self._save()
        return rec

    # ---- the WireGuard tunnel: keys come from health reports, numbers are given once (the module docstring)
    def _tunnel_key_holder(self, device: str, public: str) -> str | None:
        """Another record that holds this tunnel key, accepted or waiting: one tunnel key, one record."""
        for other in self.devices.values():
            if other["id"] != device and public in (other.get("wg_public"), other.get("wg_pending")):
                return other["id"]
        return None

    def wg_report(self, device: str, public: str, may_pend: bool = True) -> tuple[str, str | None]:
        """A tunnel key a unit or a district server reported in its health (authenticated by its session). What
        happened, as (what, detail):
        ("accepted", None)  it had none: its first key is taken as it is (trust on first use);
        ("same", None)      its accepted key; a different one that waited for an admin is dropped;
        ("waiting", None)   the key that already waits for an admin;
        ("pending", old)    a different key, kept waiting for an admin (old: the accepted one);
        ("ignored", None)   a different key while may_pend is False (the hub lets one key wait per session);
        ("in use", other)   another record holds this key: it is not stored;
        ("none", None)      no such unit or district server."""
        self._reload()
        rec = self.devices.get(device)
        if rec is None or kind_of(rec) not in (UNIT, DISTRICT):
            return "none", None
        if public == rec.get("wg_public"):
            if rec.pop("wg_pending", None) is not None:
                self._save()
            return "same", None
        if (other := self._tunnel_key_holder(device, public)) is not None:
            return "in use", other
        if not rec.get("wg_public"):
            rec["wg_public"] = public
            self._save()
            return "accepted", None
        if public == rec.get("wg_pending"):
            return "waiting", None
        if not may_pend:
            return "ignored", None
        rec["wg_pending"] = public
        self._save()
        return "pending", rec["wg_public"]

    def wg_confirm(self, device: str) -> tuple[dict, str | None] | None:
        """An admin confirms the tunnel key that waits: it becomes the accepted one. (record, the key it replaces), or
        None when no key waits. ValueError if another record holds that key by now."""
        self._reload()
        rec = self.devices.get(device)
        if rec is None or not rec.get("wg_pending"):
            return None
        if (other := self._tunnel_key_holder(device, rec["wg_pending"])) is not None:
            raise ValueError(f"this tunnel key is in use by {other}")
        old = rec.get("wg_public")
        rec["wg_public"] = rec.pop("wg_pending")
        self._save()
        return rec, old

    def wg_set_mode(self, device: str, mode: str) -> tuple[dict, str] | None:
        """Whether a district's relay still answers units outside the tunnel ("allow") or only through it ("only").
        (record, the mode it had), or None for an unknown device; ValueError for a unit or a word that is not a mode."""
        self._reload()
        rec = self.devices.get(device)
        if rec is None:
            return None
        if kind_of(rec) != DISTRICT:
            raise ValueError("not a district")
        if mode not in WG_MODES:
            raise ValueError("mode must be allow or only")
        was = rec.get("wg_mode", "allow")
        rec["wg_mode"] = mode
        self._save()
        return rec, was

    def wg_number(self, device: str) -> list[tuple[str, str]]:
        """Give this unit or district server the tunnel numbers it is due now, and keep them: a district its number
        once its own key is accepted, a unit its address once it has an accepted key and an active district (which
        then gets its number too, if it has none). Returns [(device, "<id> -> <number>")] for each number given now."""
        self._reload()
        rec = self.devices.get(device)
        given = []
        if rec is None or rec["status"] != ACTIVE or not rec.get("wg_public"):
            return given
        try:
            if kind_of(rec) == DISTRICT and "wg_index" not in rec:
                given.append(self._give_index(rec))
            elif kind_of(rec) == UNIT and not rec.get("wg_ip"):
                district = self.devices.get(rec.get("district") or "")
                if district is not None and kind_of(district) == DISTRICT and district["status"] == ACTIVE:
                    if "wg_index" not in district:
                        given.append(self._give_index(district))
                    taken = {d["id"]: d["wg_ip"] for d in self.devices.values()
                             if d.get("district") == district["id"] and d.get("wg_ip")}  # revoked units keep theirs
                    rec["wg_ip"] = wireguard.assign_unit(wireguard.Plan.of(), district["wg_index"], device, taken,
                                                         district.get("wg_retired", []))
                    given.append((device, f"{device} -> {rec['wg_ip']}"))
        except wireguard.WireGuardError as err:  # a damaged number in the file, or a full network: nothing more is given
            print(f"registry: no tunnel number for {device}: {err}", flush=True)
        if given:
            self._save()
        return given

    def _give_index(self, district: dict) -> tuple[str, str]:
        numbers = {d["id"]: d["wg_index"] for d in self.devices.values() if kind_of(d) == DISTRICT and "wg_index" in d}
        district["wg_index"] = wireguard.assign_index(district["id"], numbers)
        return district["id"], f"{district['id']} -> {district['wg_index']}"


def _forget_tunnel_key(rec: dict):
    """A new device key is a new box: the tunnel key the old box reported is no longer trusted, and the new box's first
    one is accepted by itself. Its tunnel address and its district's number stay."""
    rec.pop("wg_public", None)
    rec.pop("wg_pending", None)


def save(path: Path, devices: dict[str, dict]):
    """Whole file or nothing: a reader (the CLI, a second process) never sees a half-written registry."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(list(devices.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


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
    if not (pki / "audit.key").exists():  # signs checkpoints of the audit log that leave the server
        key = crypto.new_key()
        (pki / "audit.key").write_text(crypto.private_hex(key))
        (pki / "audit.pub").write_text(crypto.public_hex(key))
        created = True
    if not (pki / "gateway.key").exists():
        key = crypto.new_key()
        (pki / "gateway.key").write_text(crypto.private_hex(key))
        (pki / "gateway.pub").write_text(crypto.public_hex(key))
        created = True
    created |= trust.ensure(pki)  # root key and the gateway certificate, see devices/trust.py

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
    # the demo twin also has a few street-lighting cabinets (City OS module lighting), next to its first junctions
    placed = [j for j in junctions if isinstance(j.get("lon"), (int, float)) and isinstance(j.get("lat"), (int, float))]
    for n, j in enumerate(placed[:DEMO_CABINETS], start=1):
        cabinet = f"C-{n:02d}"
        ident = f"light-{cabinet}"
        if ident in devices and (pki / "edge" / f"{ident}.key").exists():
            continue
        key = crypto.new_key()
        (pki / "edge" / f"{ident}.key").write_text(crypto.private_hex(key))
        devices[ident] = {"id": ident, "kind": "lighting", "cabinet": cabinet,
                          "lon": round(j["lon"] + 0.0006, 6), "lat": round(j["lat"] + 0.0004, 6),
                          "junction": None, "tls": None, "name": "", "public_key": crypto.public_hex(key),
                          "status": ACTIVE, "serial": f"LGT-{secrets.token_hex(3).upper()}",
                          "enrolled": datetime.date.today().isoformat()}
        created = True
    # and a few pump stations (City OS module water, read only), next to its first junctions too
    for n, j in enumerate(placed[:DEMO_STATIONS], start=1):
        site = f"S-{n:02d}"
        ident = f"pump-{site}"
        if ident in devices and (pki / "edge" / f"{ident}.key").exists():
            continue
        key = crypto.new_key()
        (pki / "edge" / f"{ident}.key").write_text(crypto.private_hex(key))
        devices[ident] = {"id": ident, "kind": WATER, "site": site,
                          "lon": round(j["lon"] - 0.0009, 6), "lat": round(j["lat"] - 0.0005, 6),
                          "p_min": 2.0, "p_max": 6.0, "junction": None, "tls": None, "name": "",
                          "public_key": crypto.public_hex(key), "status": ACTIVE,
                          "serial": f"WTR-{secrets.token_hex(3).upper()}", "enrolled": datetime.date.today().isoformat()}
        created = True
    if created:
        save(path, devices)
    return created


DEMO_CABINETS = 4  # street-lighting cabinets the demo twin gets
DEMO_STATIONS = 3  # pump stations the demo twin gets (the water module runs them emulated, one with a pump fault)
