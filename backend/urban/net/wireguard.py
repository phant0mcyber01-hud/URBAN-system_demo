"""The WireGuard tunnel between a junction unit and its district server: the parts that can be tested without a kernel.

Design and reasons: docs/wireguard-design.md. This module only does arithmetic and text; it never touches an
interface, a firewall or a socket. What it gives:

- keys: X25519 pairs in the text form of `wg genkey` / `wg pubkey` (base64, 44 characters), written 0600;
- addresses: a district gets a number 1..254, its tunnel network is 10.77.<number>.0/24, the district itself is .1 and
  its units get .2 ... .254; an address, once given to a unit, is that unit's for good, and collisions are refused;
- two messages signed by the gateway key (the centre's), checked by whoever applies them and kept on disk still signed:
  the PEER LIST a district applies (which unit has which key and address) and the LINK a unit applies (its address, the
  district's key and endpoint). A message is obeyed only if the gateway signature holds, it is for this district or
  unit, and its version is not older than what was applied;
- text for `wg syncconf` / `wg set` / `ip`, the handshake ages read from `wg show`, and the plan of which tunnel port
  stands for which camera of a cabinet.

    python -m urban.net.wireguard keygen FILE         makes FILE (private key, 0600, never overwritten), prints the public key
    python -m urban.net.wireguard pubkey FILE         the public key of a private key file
    python -m urban.net.wireguard district-conf --message FILE --gateway-pub FILE --district NAME --key FILE [--last-version N]
    python -m urban.net.wireguard unit-conf --message FILE --gateway-pub FILE --unit NAME --key FILE [--last-version N]
                                                      verify the signed message and print the `wg syncconf` text
"""
import argparse
import base64
import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from ..devices import crypto
from ..devices.enrolment import DEVICE, check_address, has_port

IFACE = "wg0"
PORT = 51820                  # UDP port the district listens on
KEEPALIVE = 25                # seconds; the unit speaks first and keeps a NAT or LTE mapping alive
MTU = 1420                    # WireGuard's default for a 1500-byte path
RELAY_PORT = 8443             # the district's relay (the centre's device listener) on the district's tunnel address
CAMERA_PORT_FIRST = 8551      # first TCP port on a unit's tunnel address that stands for a camera
CAMERA_PORT_LAST = 8599
DEFAULT_BASE = "10.77.0.0/16"  # 10.77.<district>.<unit>
MAX_DISTRICTS = 254
FIRST_UNIT, LAST_UNIT = 2, 254  # .1 is the district, .0 and .255 are not hosts
MAX_PEERS = LAST_UNIT - FIRST_UNIT + 1
PEERS_TYPE, LINK_TYPE = "wg-peers", "wg-link"
PEER_FIELDS = {"type", "district", "index", "version", "t", "plain_relay", "peers", "sig"}
LINK_FIELDS = {"type", "unit", "district", "index", "version", "t", "address", "server_public", "endpoint", "sig"}
HANDSHAKE_FRESH = 180         # seconds: a session is renewed every 2 minutes while traffic flows, and a keepalive of 25 s keeps it so
UNIT_DEVICE = re.compile(r"^(edge|light)-[A-Za-z0-9._-]{1,54}$")  # a district server is not a peer of a district
CAMERA_NAME = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


class WireGuardError(ValueError):
    """Anything wrong with a key, an address or a message, worded for the log."""


class AddressError(WireGuardError):
    """No address to give, or one that would collide."""


class MessageRefused(WireGuardError):
    """A signed message that is not to be obeyed: the text says why."""


# ---- keys ------------------------------------------------------------------------------------------------------

def check_key(text) -> str:
    """A WireGuard key as `wg` prints it: base64 of exactly 32 bytes, in its one canonical spelling. Returns it trimmed.
    The all-zero key is refused (it is no public key of anyone)."""
    if not isinstance(text, str):
        raise WireGuardError("a WireGuard key is a 44-character base64 text")
    text = text.strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError:
        raise WireGuardError("a WireGuard key is a 44-character base64 text")
    if len(raw) != 32 or base64.b64encode(raw).decode() != text:
        raise WireGuardError("a WireGuard key is 32 bytes in canonical base64 (44 characters ending in '=')")
    if raw == bytes(32):
        raise WireGuardError("the all-zero key is not a key")
    return text


def new_private() -> str:
    """A private key like `wg genkey` makes it (random, clamped), as base64."""
    raw = bytearray(os.urandom(32))
    raw[0] &= 248
    raw[31] &= 127
    raw[31] |= 64
    return base64.b64encode(bytes(raw)).decode()


def public_of(private: str) -> str:
    """`wg pubkey`: the public key of a private key (X25519; the clamping is done by the scalar multiplication)."""
    raw = base64.b64decode(check_key(private), validate=True)
    public = X25519PrivateKey.from_private_bytes(raw).public_key()
    return base64.b64encode(public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def new_keypair() -> tuple[str, str]:
    """(private, public) as base64 text."""
    private = new_private()
    return private, public_of(private)


def fingerprint(public: str) -> str:
    """Short form of a public key to compare by eye or over the phone: XXXX-XXXX-XXXX-XXXX (as devices.crypto)."""
    return crypto.fingerprint(base64.b64decode(check_key(public), validate=True).hex())


def write_private(path: Path, private: str | None = None) -> str:
    """Make the key file (mode 0600, created that way, never overwritten) and return the public key. An existing file
    is a FileExistsError: a unit's tunnel key is made once; a new key is a deliberate act."""
    private = check_key(private) if private is not None else new_private()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(private + "\n")
    return public_of(private)


def read_private(path: Path) -> str:
    return check_key(Path(path).read_text())


# ---- addresses -------------------------------------------------------------------------------------------------

def check_index(value) -> int:
    """A district's tunnel number."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DISTRICTS:
        raise AddressError(f"a district's tunnel number is 1..{MAX_DISTRICTS}, not {value!r}")
    return value


@dataclass(frozen=True)
class Plan:
    """The addressing plan: base is a /16, a district with number d owns <base>.d.0/24."""
    base: ipaddress.IPv4Network = ipaddress.ip_network(DEFAULT_BASE)

    def __post_init__(self):
        if not isinstance(self.base, ipaddress.IPv4Network) or self.base.prefixlen != 16 or not self.base.is_private:
            raise AddressError("the tunnel base is a private IPv4 /16, e.g. 10.77.0.0/16")

    @classmethod
    def of(cls, base: str = DEFAULT_BASE) -> "Plan":
        try:
            return cls(ipaddress.ip_network(base, strict=True))
        except ValueError as err:
            raise AddressError(f"the tunnel base {base!r}: {err}")

    def network(self, index: int) -> ipaddress.IPv4Network:
        a, b, _, _ = self.base.network_address.packed
        return ipaddress.ip_network(f"{a}.{b}.{check_index(index)}.0/24")

    def server(self, index: int) -> str:
        """The district's own tunnel address."""
        return str(self.network(index).network_address + 1)

    def host(self, index: int, n: int) -> str:
        if isinstance(n, bool) or not isinstance(n, int) or not FIRST_UNIT <= n <= LAST_UNIT:
            raise AddressError(f"a unit's host number is {FIRST_UNIT}..{LAST_UNIT}")
        return str(self.network(index).network_address + n)

    def check_unit_ip(self, index: int, ip) -> str:
        """The address of a unit of this district: an IPv4 host of its network, not the district's, not a network or
        broadcast address."""
        try:
            addr = ipaddress.IPv4Address(str(ip).strip())
        except ValueError:
            raise AddressError(f"{ip!r} is not an IPv4 address")
        net = self.network(index)
        if addr not in net or not FIRST_UNIT <= int(addr) - int(net.network_address) <= LAST_UNIT:
            raise AddressError(f"{addr} is not a unit address of the district network {net} (units are .{FIRST_UNIT} to .{LAST_UNIT})")
        return str(addr)


def assign_index(district: str, assigned: Mapping[str, int]) -> int:
    """The tunnel number of a district: the one it has, else the lowest free. `assigned` is every district's number so far."""
    check_district(district)
    if district in assigned:
        return check_index(assigned[district])
    used = {check_index(i) for i in assigned.values()}
    for i in range(1, MAX_DISTRICTS + 1):
        if i not in used:
            return i
    raise AddressError(f"all {MAX_DISTRICTS} district numbers are taken")


def check_assignments(plan: Plan, index: int, assigned: Mapping[str, str]) -> dict[str, str]:
    """Every unit's address of one district is a unit address of its network and no two units share one."""
    seen: dict[str, str] = {}
    for unit, ip in assigned.items():
        ip = plan.check_unit_ip(index, ip)
        if ip in seen:
            raise AddressError(f"{unit} and {seen[ip]} both have {ip}")
        seen[ip] = unit
    return {unit: ip for ip, unit in seen.items()}


def assign_unit(plan: Plan, index: int, unit: str, assigned: Mapping[str, str], retired: Iterable[str] = ()) -> str:
    """The tunnel address of a unit: the one it already has (so it never moves), else the lowest that is neither in use nor
    retired (the address of a revoked unit is kept out of reach, so no other unit inherits what an old key knew).
    `assigned` is unit id -> address for this district. Refuses a collision in `assigned` itself."""
    if not UNIT_DEVICE.fullmatch(str(unit)):
        raise AddressError(f"{unit!r} is not a unit id (edge-<junction> or light-<cabinet>)")
    taken = check_assignments(plan, index, assigned)
    if unit in taken:
        return taken[unit]
    used = set(taken.values()) | {plan.check_unit_ip(index, ip) for ip in retired}
    for n in range(FIRST_UNIT, LAST_UNIT + 1):
        ip = plan.host(index, n)
        if ip not in used:
            return ip
    raise AddressError(f"the district network {plan.network(index)} has no free address left ({MAX_PEERS} units)")


# ---- the peer list a district applies and the link a unit applies ------------------------------------------------

@dataclass(frozen=True)
class Peer:
    unit: str
    public: str   # the unit's WireGuard public key
    ip: str       # its tunnel address

    def body(self) -> dict:
        return {"unit": self.unit, "public": self.public, "ip": self.ip}


@dataclass(frozen=True)
class PeerList:
    """What one district's WireGuard interface must hold: its units and nothing else. plain_relay says whether the
    relay still answers units that come NOT through the tunnel (the migration; the centre closes it at the end)."""
    district: str
    index: int
    version: int
    issued: int
    peers: tuple[Peer, ...]
    plain_relay: bool = True

    def body(self) -> dict:
        return {"type": PEERS_TYPE, "district": self.district, "index": self.index, "version": self.version,
                "t": self.issued, "plain_relay": self.plain_relay, "peers": [p.body() for p in self.peers]}

    def by_unit(self) -> dict[str, Peer]:
        return {p.unit: p for p in self.peers}


@dataclass(frozen=True)
class Link:
    """What one unit's WireGuard interface must hold: its address, the district's key and where to find the district."""
    unit: str
    district: str
    index: int
    version: int
    issued: int
    address: str
    server_public: str
    endpoint: str      # host:port of the district's UDP listener on the network between them

    def body(self) -> dict:
        return {"type": LINK_TYPE, "unit": self.unit, "district": self.district, "index": self.index,
                "version": self.version, "t": self.issued, "address": self.address,
                "server_public": self.server_public, "endpoint": self.endpoint}


def check_district(name) -> str:
    if not isinstance(name, str) or not DEVICE.fullmatch(name) or not name.startswith("district-"):
        raise WireGuardError(f"{name!r} is not a district id (district-<name>)")
    return name


def check_unit(name) -> str:
    if not isinstance(name, str) or not UNIT_DEVICE.fullmatch(name):
        raise WireGuardError(f"{name!r} is not a unit id (edge-<junction> or light-<cabinet>)")
    return name


def check_version(value, what="version") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WireGuardError(f"the {what} is a whole number, 0 or more")
    return value


def check_endpoint(plan: Plan, endpoint) -> str:
    """host:port of the district's WireGuard listener; the port defaults to 51820. It is an address on the network that
    carries the tunnel, never an address inside a tunnel network (that would try to carry the tunnel in itself)."""
    try:
        text = check_address(endpoint)
    except ValueError as err:
        raise WireGuardError(str(err))
    if not has_port(text):
        text = f"{text}:{PORT}"
    try:
        inside = ipaddress.ip_address(text.rpartition(":")[0].strip("[]")) in plan.base
    except ValueError:
        inside = False  # a host name
    if inside:
        raise WireGuardError(f"the endpoint {text} is inside the tunnel network {plan.base}: it must be the district's real address")
    return text


def make_peer_list(plan: Plan, district: str, index: int, version: int, issued: int, peers: Iterable[Peer | dict],
                   plain_relay: bool = True) -> PeerList:
    """A peer list, checked: units are units, keys are keys, addresses belong to this district, and no unit, key or
    address appears twice. Peers are put in the order of their unit ids, so the same set gives the same message."""
    district, index = check_district(district), check_index(index)
    if not isinstance(plain_relay, bool):
        raise WireGuardError("plain_relay is true or false")
    out: list[Peer] = []
    seen = {"unit": set(), "key": set(), "address": set()}
    for item in peers:
        try:
            p = item if isinstance(item, Peer) else Peer(item["unit"], item["public"], item["ip"])
        except (KeyError, TypeError):
            raise WireGuardError("a peer needs unit, public and ip")
        peer = Peer(check_unit(p.unit), check_key(p.public), plan.check_unit_ip(index, p.ip))
        for what, value in (("unit", peer.unit), ("key", peer.public), ("address", peer.ip)):
            if value in seen[what]:
                raise WireGuardError(f"two peers have the same {what}: {value}")
            seen[what].add(value)
        out.append(peer)
    if len(out) > MAX_PEERS:
        raise WireGuardError(f"more than {MAX_PEERS} peers")
    return PeerList(district, index, check_version(version), check_version(issued, "time"),
                    tuple(sorted(out, key=lambda p: p.unit)), plain_relay)


def make_link(plan: Plan, unit: str, district: str, index: int, version: int, issued: int, address: str,
              server_public: str, endpoint: str) -> Link:
    return Link(check_unit(unit), check_district(district), check_index(index), check_version(version),
                check_version(issued, "time"), plan.check_unit_ip(index, address), check_key(server_public),
                check_endpoint(plan, endpoint))


def sign_message(gateway_key, body: dict) -> dict:
    """The message with the gateway's signature (a key object or a devices.signer.Signer, as for every command)."""
    return {**body, "sig": crypto.sign(gateway_key, body)}


def _signed_body(msg, public, fields: set, kind: str) -> dict:
    """The body of a signed message, or MessageRefused. The signature is checked before anything else is read."""
    if not isinstance(msg, dict) or msg.get("type") != kind:
        raise MessageRefused(f"not a {kind} message")
    if public is None:
        raise MessageRefused("no gateway key yet")
    body = {k: v for k, v in msg.items() if k != "sig"}
    if not crypto.verify(public, body, str(msg.get("sig"))):
        raise MessageRefused("bad signature")
    if set(msg) != fields:
        raise MessageRefused(f"unexpected fields: {sorted(set(msg) ^ fields)}")
    return body


def parse_peer_list(msg, public, plan: Plan, district: str, last_version: int = 0) -> PeerList:
    """A signed peer list as `district` must obey it. `last_version` is the newest version it has applied: an older
    list is refused (a replay), the same version is accepted (it is the same list, applying it again changes nothing)."""
    body = _signed_body(msg, public, PEER_FIELDS, PEERS_TYPE)
    if body["district"] != district:
        raise MessageRefused(f"addressed to {body['district']}, this is {district}")
    try:
        lst = make_peer_list(plan, body["district"], body["index"], body["version"], body["t"], body["peers"], body["plain_relay"])
    except (WireGuardError, TypeError) as err:
        raise MessageRefused(f"malformed: {err}")
    if lst.version < last_version:
        raise MessageRefused(f"version {lst.version} is older than the {last_version} already applied")
    return lst


def parse_link(msg, public, plan: Plan, unit: str, last_version: int = 0) -> Link:
    """A signed link as `unit` must obey it (same rules as parse_peer_list)."""
    body = _signed_body(msg, public, LINK_FIELDS, LINK_TYPE)
    if body["unit"] != unit:
        raise MessageRefused(f"addressed to {body['unit']}, this is {unit}")
    try:
        link = make_link(plan, body["unit"], body["district"], body["index"], body["version"], body["t"],
                         body["address"], body["server_public"], body["endpoint"])
    except (WireGuardError, TypeError) as err:
        raise MessageRefused(f"malformed: {err}")
    if link.version < last_version:
        raise MessageRefused(f"version {link.version} is older than the {last_version} already applied")
    return link


# ---- what is handed to wg and ip -------------------------------------------------------------------------------

def district_conf(plan: Plan, lst: PeerList, private: str | None = None, port: int = PORT) -> str:
    """The district's interface and peers in the text `wg syncconf` reads (no Address line: that is `ip`'s). Each unit may
    send from, and be sent to, its own address and no other: that is how one unit cannot pose as another.
    The private key is left out unless given; the usual way is `wg set wg0 private-key FILE` once (district_up_commands),
    so the key is never in a file this module made."""
    lines = ["[Interface]"]
    if private is not None:
        lines.append(f"PrivateKey = {check_key(private)}")
    lines += [f"ListenPort = {int(port)}", ""]
    for p in lst.peers:
        lines += [f"# {p.unit}", "[Peer]", f"PublicKey = {p.public}", f"AllowedIPs = {p.ip}/32", ""]
    return "\n".join(lines)


def unit_conf(plan: Plan, link: Link, private: str | None = None, keepalive: int = KEEPALIVE) -> str:
    """The unit's interface in the text `wg syncconf` reads: one peer, the district, reachable at its endpoint, and only
    its own tunnel address as the way into it (a unit cannot address another unit)."""
    lines = ["[Interface]"]
    if private is not None:
        lines.append(f"PrivateKey = {check_key(private)}")
    lines += ["", f"# {link.district}", "[Peer]", f"PublicKey = {link.server_public}", f"Endpoint = {link.endpoint}",
              f"AllowedIPs = {plan.server(link.index)}/32", f"PersistentKeepalive = {int(keepalive)}", ""]
    return "\n".join(lines)


def set_commands(old: Iterable[Peer], new: Iterable[Peer], iface: str = IFACE) -> list[list[str]]:
    """The `wg set` argument lists that turn a district's peers `old` into `new` without touching the others (a session
    that stays the same is not interrupted). Removals first, so a key that moves to another address is not caught between."""
    old_by, new_by = {p.public: p for p in old}, {p.public: p for p in new}
    cmds = [["wg", "set", iface, "peer", key, "remove"] for key in sorted(old_by) if key not in new_by]
    for key in sorted(new_by):
        if old_by.get(key) != new_by[key]:
            cmds.append(["wg", "set", iface, "peer", key, "allowed-ips", f"{new_by[key].ip}/32"])
    return cmds


def district_up_commands(plan: Plan, index: int, keyfile: str, iface: str = IFACE, port: int = PORT) -> list[list[str]]:
    """Bring the district's interface up (the root script runs these, then `wg syncconf`)."""
    net = plan.network(index)
    return [["ip", "link", "add", "dev", iface, "type", "wireguard"],
            ["wg", "set", iface, "private-key", keyfile, "listen-port", str(port)],
            ["ip", "address", "add", f"{plan.server(index)}/{net.prefixlen}", "dev", iface],
            ["ip", "link", "set", "dev", iface, "mtu", str(MTU), "up"]]


def unit_up_commands(plan: Plan, link: Link, keyfile: str, iface: str = IFACE) -> list[list[str]]:
    """Bring a unit's interface up. The unit's address is a /32 and the only route is the one to the district's own address."""
    return [["ip", "link", "add", "dev", iface, "type", "wireguard"],
            ["wg", "set", iface, "private-key", keyfile],
            ["ip", "address", "add", f"{link.address}/32", "dev", iface],
            ["ip", "link", "set", "dev", iface, "mtu", str(MTU), "up"],
            ["ip", "route", "add", f"{plan.server(link.index)}/32", "dev", iface]]


def parse_latest_handshakes(text: str) -> dict[str, int]:
    """`wg show wg0 latest-handshakes` -> {public key: unix time of the last handshake, 0 = never}. Lines that are not
    `key<TAB>number` are ignored."""
    out = {}
    for line in str(text).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            try:
                out[check_key(parts[0])] = int(parts[1])
            except WireGuardError:
                continue
    return out


def peers_up(handshakes: Mapping[str, int], now: float, within: int = HANDSHAKE_FRESH) -> set[str]:
    """The keys whose last handshake is not older than `within` seconds. A handshake in the future (a clock that
    stepped) does not count as fresh proof."""
    return {key for key, t in handshakes.items() if t > 0 and 0 <= now - t <= within}


# ---- cameras behind a unit ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CameraForward:
    name: str
    host: str          # the camera in the cabinet's network
    port: int          # its RTSP port there
    tunnel_port: int   # the TCP port on the unit's tunnel address that stands for it

    def url(self, unit_ip: str, path: str = "/") -> str:
        """The address the district's vision opens for this camera (credentials are not in it: see docs/vision.md)."""
        return f"rtsp://{unit_ip}:{self.tunnel_port}{path if path.startswith('/') else '/' + path}"


def plan_camera_forwards(cameras: Iterable[Mapping], plan: Plan | None = None) -> list[CameraForward]:
    """Which tunnel port stands for which camera of a cabinet. A camera may name its `tunnel_port` (it should: the port
    is part of the district's camera address and must not move when another camera is added); the others get the
    lowest free one from 8551. Names and ports are unique; a camera is an IPv4 host of the cabinet's own network, never
    an address inside a tunnel network or on the loopback."""
    plan = plan or Plan.of()
    fixed: dict[int, tuple[str, str, int]] = {}
    floating: list[tuple[str, str, int]] = []
    names = set()
    for cam in cameras:
        try:
            name, host = str(cam["name"]), ipaddress.IPv4Address(str(cam["host"]).strip())
        except (KeyError, TypeError, ValueError):
            raise WireGuardError("a camera needs a name and an IPv4 host")
        if not CAMERA_NAME.fullmatch(name):
            raise WireGuardError(f"{name!r}: a camera name is letters, digits, . _ - (up to 40)")
        if name in names:
            raise WireGuardError(f"two cameras are called {name}")
        names.add(name)
        port = cam.get("port", 554)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise WireGuardError(f"{name}: the camera's port is 1..65535")
        if host in plan.base or host.is_loopback or host.is_unspecified or host.is_multicast:
            raise WireGuardError(f"{name}: {host} cannot be a camera of the cabinet's network")
        tp = cam.get("tunnel_port")
        if tp is None:
            floating.append((name, str(host), port))
            continue
        if isinstance(tp, bool) or not isinstance(tp, int) or not CAMERA_PORT_FIRST <= tp <= CAMERA_PORT_LAST:
            raise WireGuardError(f"{name}: the tunnel port is {CAMERA_PORT_FIRST}..{CAMERA_PORT_LAST}")
        if tp in fixed:
            raise WireGuardError(f"{name} and {fixed[tp][0]} both have tunnel port {tp}")
        fixed[tp] = (name, str(host), port)
    free = (p for p in range(CAMERA_PORT_FIRST, CAMERA_PORT_LAST + 1) if p not in fixed)
    out = [CameraForward(n, h, p, tp) for tp, (n, h, p) in fixed.items()]
    for n, h, p in floating:
        try:
            out.append(CameraForward(n, h, p, next(free)))
        except StopIteration:
            raise WireGuardError(f"more than {CAMERA_PORT_LAST - CAMERA_PORT_FIRST + 1} cameras behind one unit")
    return sorted(out, key=lambda c: c.tunnel_port)


# ---- command line (the root helpers use it) ----------------------------------------------------------------------

def _load_message(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise WireGuardError(f"cannot read {path}: {err}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m urban.net.wireguard", description="URBAN WireGuard tunnel helpers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("keygen", "pubkey"):
        sub.add_parser(name).add_argument("file")
    for name, who in (("district-conf", "district"), ("unit-conf", "unit")):
        p = sub.add_parser(name)
        p.add_argument("--message", required=True)
        p.add_argument("--gateway-pub", required=True, help="file with the gateway's public key (hex), as pki/gateway.pub")
        p.add_argument(f"--{who}", required=True)
        p.add_argument("--key", required=True, help="file with this machine's private key")
        p.add_argument("--last-version", type=int, default=0)
        p.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "keygen":
            print(write_private(Path(args.file)))
        elif args.cmd == "pubkey":
            print(public_of(read_private(Path(args.file))))
        else:
            plan = Plan.of(args.base)
            public = crypto.load_public(Path(args.gateway_pub).read_text())
            private = read_private(Path(args.key))
            msg = _load_message(args.message)
            if args.cmd == "district-conf":
                print(district_conf(plan, parse_peer_list(msg, public, plan, args.district, args.last_version), private))
            else:
                print(unit_conf(plan, parse_link(msg, public, plan, args.unit, args.last_version), private))
    except (WireGuardError, ValueError, OSError) as err:
        print(f"wireguard: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
