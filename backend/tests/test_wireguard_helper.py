"""The tunnel's root helper (urban/net/wghelper.py, `urban-wg`) with a pretend kernel behind its commands: it takes
the inbox's message as a hostile file, checks it against its own pinned anchor, orders messages by (gateway key epoch,
version), binds a unit's link to the district its kit names, drops the link when a new kit points elsewhere, and keeps
wg0, the district's peers, the unit's route, camera forwards and forwarding in line with the applied message. The
status is written whatever happens, nothing a child prints is shown, and a run with nothing to do stays light."""
import base64
import datetime
import json
import os
import runpy
import shutil
import subprocess
import sys
import textwrap
from contextlib import nullcontext
from pathlib import Path

import pytest

from urban.devices import crypto, trust
from urban.net import wireguard as wg
from urban.net.wghelper import CONSOLE, Helper, Paths, camera_rules, fingerprint, main

PLAN = wg.Plan.of()
NOW = 1_800_000_000             # 2027-01-15
BUILT = "2026-10-01T00:00:00Z"  # the build: before NOW
UNIT, DISTRICT = "edge-J-01", "district-1"
ENDPOINT = "192.168.77.1:51820"
NOISE = "NOISE-FROM-A-CHILD"
STATUS_KEYS = {"role", "public", "state", "applied", "epoch", "district", "index", "address", "server", "proven",
               "handshake", "peers", "peers_up", "mode", "reason", "t"}
BACKEND = Path(__file__).resolve().parents[1]


class Kernel:
    """The commands the helper runs, answered from a pretend kernel: wg0 (address, admin state, peers with their
    allowed IPs, endpoint and last handshake), the routes `ip route get` finds and the camera table. Every command is
    recorded. A command that starts with one of `fail` exits 2 and prints NOISE, which must never be shown."""

    def __init__(self, conf: Path):
        self.conf = conf
        self.calls = []
        self.wg0 = None
        self.routes = {"192.168.77.1": "192.168.77.1 dev up0 src 192.168.77.2 uid 0 \\    cache "}
        self.table = None
        self.fail = []

    def __call__(self, argv):
        assert all(isinstance(a, str) for a in argv), argv
        self.calls.append(argv)
        for prefix in self.fail:
            if argv[:len(prefix)] == prefix:
                return 2, NOISE
        w = self.wg0
        if argv == [CONSOLE]:
            return 0, NOISE
        if argv == ["ip", "-o", "link", "show", "dev", "wg0"]:
            if w is None:
                return 1, ""
            flags = "POINTOPOINT,NOARP" + (",UP,LOWER_UP" if w["up"] else "")
            return 0, f"7: wg0: <{flags}> mtu 1420 qdisc noqueue state UNKNOWN mode DEFAULT group default\\    link/none "
        if argv == ["ip", "link", "add", "dev", "wg0", "type", "wireguard"]:
            if w is not None:
                return 2, NOISE
            self.wg0 = {"up": False, "addr": [], "peers": {}, "routes": set(), "key": None, "port": None}
            (self.conf / "wg0").mkdir(parents=True, exist_ok=True)
            return 0, ""
        if argv == ["ip", "link", "del", "dev", "wg0"]:
            if w is None:
                return 1, NOISE
            self.wg0 = None
            shutil.rmtree(self.conf / "wg0", ignore_errors=True)
            return 0, ""
        if argv[:4] == ["ip", "-4", "-o", "route"] and argv[4] == "get":
            route = self.routes.get(argv[5])
            return (0, route) if route else (2, NOISE)
        if argv == ["nft", "list", "table", "ip", "urban_wg"]:
            return (0, self.table) if self.table else (1, NOISE)
        if argv[:2] == ["nft", "-f"]:
            self.table = Path(argv[2]).read_text()
            return 0, ""
        if argv == ["nft", "delete", "table", "ip", "urban_wg"]:
            self.table = None
            return 0, ""
        assert w is not None, f"{argv} without wg0"
        if argv[:3] == ["ip", "address", "add"] and argv[4:] == ["dev", "wg0"]:
            w["addr"].append(argv[3])
            return 0, ""
        if argv == ["ip", "link", "set", "dev", "wg0", "mtu", "1420", "up"]:
            w["up"] = True
            return 0, ""
        if argv[:2] == ["ip", "route"] and argv[2] in ("add", "replace") and argv[4:] == ["dev", "wg0"]:
            w["routes"].add(argv[3])
            return 0, ""
        if argv[:3] == ["wg", "show", "wg0"]:
            what = argv[3]
            if what == "peers":
                return 0, "".join(f"{k}\n" for k in w["peers"])
            if what == "allowed-ips":
                return 0, "".join(f"{k}\t{' '.join(p['ips']) or '(none)'}\n" for k, p in w["peers"].items())
            if what == "latest-handshakes":
                return 0, "".join(f"{k}\t{p['hs']}\n" for k, p in w["peers"].items())
        if argv[:3] == ["wg", "set", "wg0"]:
            args, i, key = argv[3:], 0, None
            while i < len(args):
                word = args[i]
                if word == "private-key":
                    w["key"] = Path(args[i + 1]).read_text().strip()
                elif word == "listen-port":
                    w["port"] = int(args[i + 1])
                elif word == "peer":
                    key = args[i + 1]
                    w["peers"].setdefault(key, {"ips": [], "hs": 0, "endpoint": None, "keepalive": 0})
                elif word == "remove":
                    w["peers"].pop(key)
                    i += 1
                    continue
                elif word == "allowed-ips":
                    w["peers"][key]["ips"] = args[i + 1].split(",")
                elif word == "endpoint":
                    w["peers"][key]["endpoint"] = args[i + 1]
                elif word == "persistent-keepalive":
                    w["peers"][key]["keepalive"] = int(args[i + 1])
                else:
                    raise AssertionError(f"wg set {word}?")
                i += 2
            return 0, ""
        raise AssertionError(f"unexpected command {argv}")

    def since(self, n):
        return self.calls[n:]


class World:
    """One machine (a unit edge-J-01 or the district district-1) in a temporary folder: unit.conf, the kit (root.pub,
    the service's gateway certificate, the env file with PLATFORM=192.168.77.1:8443), its tunnel key, the release file."""

    def __init__(self, root: Path, role: str = "unit"):
        self.paths = p = Paths(data=root / "data", run=root / "run", release=root / "urban-os-release", conf=root / "conf")
        for d in (p.pki / "edge", p.trust, p.inbox, p.run / "nft", p.conf):
            d.mkdir(parents=True, exist_ok=True)
        p.release.write_text(f"VERSION=1\nBUILT={BUILT}\nCOMPATIBLE=urban-edge-x86_64\n")
        self.role, self.name = role, (UNIT if role == "unit" else DISTRICT)
        (p.etc / "unit.conf").write_text(f"UNIT={self.name}\nSERIAL=URB-1\nROLE={role}\nHOST=\nCENTER=\n")
        self.private, self.public = wg.new_keypair()
        (p.wg / "key").write_text(self.private + "\n")
        (p.wg / "key.pub").write_text(self.public + "\n")
        self.root, self.gateway = crypto.new_key(), crypto.new_key()
        self.cert = trust.issue(self.root, crypto.public_hex(self.gateway), 1)
        (p.pki / "root.pub").write_text(crypto.public_hex(self.root) + "\n")
        self.cert_file = p.pki / "edge" / f"{self.name}.gateway.json"
        self.cert_file.write_text(json.dumps(self.cert))
        self.env = p.etc / f"edge-{self.name}.env"
        self.env.write_text("# the kit\nPLATFORM=192.168.77.1:8443\nSERVER_NAME=urban-center\n")
        self.server = wg.new_keypair()[1]  # the district's tunnel key, as a unit's link names it
        self.kernel = Kernel(p.conf)
        self.now = NOW
        self.lines = []
        self.locked = []

    def helper(self, say=None):
        def lock(path):
            self.locked.append(path)
            return nullcontext()
        return Helper(self.paths, self.kernel, lambda: self.now, lock, say or self.lines.append)

    def main(self, *argv):
        return main(list(argv) or ["run"], self.helper())

    def link(self, version=1, key=None, **fields):
        f = {"unit": UNIT, "district": DISTRICT, "index": 1, "address": "10.77.1.2", "server_public": self.server,
             "endpoint": ENDPOINT, **fields}
        body = wg.make_link(PLAN, f["unit"], f["district"], f["index"], version, NOW, f["address"], f["server_public"],
                            f["endpoint"]).body()
        return wg.sign_message(key or self.gateway, body)

    def peer_list(self, peers, version=1, plain=True, key=None):
        return wg.sign_message(key or self.gateway, wg.make_peer_list(PLAN, DISTRICT, 1, version, NOW, peers, plain).body())

    def send(self, msg):
        (self.paths.inbox / "message.json").write_text(json.dumps(msg))

    def status(self):
        return json.loads(self.paths.status.read_text())

    def text(self, path):
        return path.read_text().strip() if path.exists() else None

    def said(self, start=0):
        return self.lines[start:]

    def rotate(self, epoch):
        """A new gateway key and its certificate, as the service keeps it after the centre presented it."""
        self.gateway = crypto.new_key()
        self.cert = trust.issue(self.root, crypto.public_hex(self.gateway), epoch)
        self.cert_file.write_text(json.dumps(self.cert))


@pytest.fixture
def unit(tmp_path):
    w = World(tmp_path, "unit")
    assert w.main("pin", "--replace") == 0
    return w


@pytest.fixture
def district(tmp_path):
    w = World(tmp_path, "district")
    assert w.main("pin", "--replace") == 0
    return w


def up_unit(w, version=1, handshake=True):
    """A unit with its link applied, wg0 up and (handshake=True) a fresh handshake with the district."""
    w.send(w.link(version))
    assert w.main() == 0
    if handshake:
        w.kernel.wg0["peers"][w.server]["hs"] = w.now - 5
        assert w.main() == 0
    return w


def assert_never_shown(w):
    """No child output and no private key anywhere the helper writes or says something."""
    shown = "\n".join(w.lines) + "".join(p.read_text() for p in (w.paths.status, w.paths.screen, w.paths.memory)
                                          if p.exists())
    assert NOISE not in shown
    assert w.private not in shown
    assert all(w.private not in " ".join(c) for c in w.kernel.calls)
    for c in w.kernel.calls:
        assert not (c[:2] == ["wg", "show"] and c[3:4] in (["dump"], ["private-key"])), c
        assert c[:2] not in (["wg", "showconf"], ["wg", "syncconf"]), c
        assert not (c[:2] == ["wg", "show"] and c[3] not in ("latest-handshakes", "peers", "allowed-ips", "listen-port",
                                                            "transfer", "endpoints")), c


# ---- a run with nothing to do ------------------------------------------------------------------------------------

def test_a_run_with_nothing_to_do_imports_no_tunnel_code_and_only_writes_the_status(tmp_path):
    script = textwrap.dedent("""
        import json, sys
        from contextlib import nullcontext
        from pathlib import Path
        from urban.net import wghelper
        root = Path(sys.argv[1])
        paths = wghelper.Paths(data=root / "data", run=root / "run", release=root / "rel", conf=root / "conf")
        for d in (paths.etc, paths.wg, paths.inbox, paths.run):
            d.mkdir(parents=True)
        (paths.etc / "unit.conf").write_text("UNIT=edge-J-01\\nROLE=unit\\n")
        (paths.wg / "key.pub").write_text(sys.argv[2] + "\\n")
        calls = []
        def runner(argv):
            calls.append(argv)
            return (1, "") if argv[:3] == ["ip", "-o", "link"] else (0, "")
        h = wghelper.Helper(paths, runner, lambda: 1800000000, lambda p: nullcontext(), lambda line: calls.append(line))
        code = h.run()
        loaded = sorted(m for m in sys.modules if m.startswith(("urban.net.wireguard", "urban.devices", "cryptography")))
        print(json.dumps({"code": code, "calls": calls, "loaded": loaded,
                          "status": json.loads(paths.status.read_text()), "screen": paths.screen.read_text()}))
    """)
    public = wg.new_keypair()[1]
    r = subprocess.run([sys.executable, "-c", script, str(tmp_path), public], cwd=BACKEND, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["code"] == 0 and out["loaded"] == []
    assert out["calls"] == [["ip", "-o", "link", "show", "dev", "wg0"], [CONSOLE]]
    st = out["status"]
    assert set(st) == STATUS_KEYS
    assert st["state"] == "none" and st["public"] == public and st["applied"] == 0 and st["mode"] == "off"
    assert out["screen"] == f"none: no tunnel from the centre yet key {wg.fingerprint(public)}\n"


def test_the_helper_reads_the_fingerprint_as_wireguard_does():
    for _ in range(5):
        public = wg.new_keypair()[1]
        assert fingerprint(public) == wg.fingerprint(public) == fingerprint(f" {public}\n")
    assert fingerprint("") == fingerprint("not a key") == fingerprint(base64.b64encode(b"x" * 31).decode()) == "none"


# ---- a unit's link: inbox, check, interface ------------------------------------------------------------------------

def test_a_link_from_the_inbox_is_checked_kept_and_brought_up(unit):
    w, p = unit, unit.paths
    msg = w.link(7)
    w.send(msg)
    start = len(w.kernel.calls)
    assert w.main() == 0
    # taken out of the inbox (renamed there, read, removed), kept as it was with the certificate that verified it
    assert sorted(os.listdir(p.inbox)) == []
    assert json.loads(p.applied.read_text()) == {"msg": msg, "cert": w.cert, "epoch": 1}
    assert w.text(p.trust / "floor") == "1 7" and w.text(p.trust / "epoch") == "1"
    key = str(p.key)
    made = [c for c in w.kernel.since(start) if c[:2] in (["ip", "link"], ["ip", "address"], ["ip", "route"], ["wg", "set"])
            and c[2:3] != ["show"]]
    assert made == [["ip", "link", "add", "dev", "wg0", "type", "wireguard"],
                    ["wg", "set", "wg0", "private-key", key],
                    ["ip", "address", "add", "10.77.1.2/32", "dev", "wg0"],
                    ["ip", "link", "set", "dev", "wg0", "mtu", "1420", "up"],
                    ["ip", "route", "add", "10.77.1.1/32", "dev", "wg0"],
                    ["wg", "set", "wg0", "private-key", key, "peer", w.server, "endpoint", ENDPOINT,
                     "allowed-ips", "10.77.1.1/32", "persistent-keepalive", "25"],
                    ["ip", "route", "replace", "10.77.1.1/32", "dev", "wg0"]]
    assert w.kernel.wg0["key"] == w.private and list(w.kernel.wg0["peers"]) == [w.server]
    assert w.said() == ["URBAN-WG applied unit version=7 address=10.77.1.2 endpoint=192.168.77.1:51820",
                        "URBAN-WG up unit address=10.77.1.2"]
    st = w.status()
    assert set(st) == STATUS_KEYS
    assert st == {**st, "role": "unit", "public": w.public, "state": "down", "applied": 7, "epoch": 1,
                  "district": DISTRICT, "index": 1, "address": "10.77.1.2", "server": "10.77.1.1", "proven": False,
                  "handshake": 0, "peers": 1, "peers_up": 0, "mode": "off", "reason": "", "t": NOW}
    # a fresh handshake with the district proves the link: the service then dials the centre through 10.77.1.1
    w.kernel.wg0["peers"][w.server]["hs"] = NOW - 12
    assert w.main() == 0
    assert w.said(2) == ["URBAN-WG proven district=district-1 server=10.77.1.1"]
    assert w.text(p.proven) == "district-1 1 10.77.1.2"
    st = w.status()
    assert st["state"] == "up" and st["proven"] is True and st["handshake"] == NOW - 12 and st["peers_up"] == 1
    assert p.screen.read_text() == (f"up 10.77.1.2 -> district-1 (10.77.1.1), handshake 12 s ago, proven "
                                    f"key {wg.fingerprint(w.public)}\n")
    # nothing changed: nothing said, nothing set
    start = len(w.kernel.calls)
    assert w.main() == 0
    assert w.said(3) == []
    assert all(c[:2] != ["wg", "set"] and c[:3] != ["ip", "link", "add"] for c in w.kernel.since(start))
    # the same message again (the centre's session restarted) is no news
    w.send(msg)
    assert w.main() == 0 and w.said(3) == []
    if os.name != "nt":
        assert oct(p.status.stat().st_mode & 0o777) == "0o644" and oct(p.screen.stat().st_mode & 0o777) == "0o644"
        for f in (p.applied, p.proven, p.trust / "floor", p.trust / "epoch", p.memory):
            assert oct(f.stat().st_mode & 0o777) == "0o600", f
    assert w.locked and set(w.locked) == {p.lock}
    assert_never_shown(w)


@pytest.mark.parametrize("case", ["directory", "too large", "not json", "not an object", "not utf-8", "nan",
                                  "hard link", "deeply nested"])
def test_the_inbox_is_read_as_a_hostile_file(unit, case):
    w, p = unit, unit.paths
    message = p.inbox / "message.json"
    (p.inbox / "taken.json").mkdir()  # left over from an earlier run: removed first, whatever it is
    (p.inbox / "taken.json" / "x").write_text("x")
    good = json.dumps(w.link(3))
    if case == "directory":
        message.mkdir()
        (message / "inner.json").write_text(good)
    elif case == "too large":
        message.write_text(good + " " * 65536)
    elif case == "not json":
        message.write_text(good[:-1])
    elif case == "not an object":
        message.write_text(json.dumps([w.link(3)]))
    elif case == "not utf-8":
        message.write_bytes(good.encode()[:-1] + b"\xff}")
    elif case == "nan":
        message.write_text(good[:-1] + ', "x": NaN}')
    elif case == "hard link":
        (p.inbox / "mine.json").write_text(good)
        os.link(p.inbox / "mine.json", message)
    else:
        message.write_text("[" * 30000 + "]" * 30000)
    assert w.main() == 1
    assert w.said() == ["URBAN-WG refused bad file"]
    assert not os.path.lexists(message) and not os.path.lexists(p.inbox / "taken.json")
    assert not p.applied.exists() and w.kernel.wg0 is None
    assert w.status()["reason"] == "bad file" and w.status()["state"] == "none"
    assert "(last message refused: bad file)" in p.screen.read_text()


def test_a_message_is_refused_in_fixed_words(unit):
    w = unit
    stranger = crypto.new_key()
    other = dict(w.link(4))
    extra = wg.sign_message(w.gateway, {**{k: v for k, v in w.link(4).items() if k != "sig"}, "note": "x"})
    cases = [(w.link(4, key=stranger), "bad signature"),
             (w.link(4, unit="edge-J-02"), "not addressed here"),
             (extra, "malformed"),
             (w.peer_list([], 4), "malformed"),
             (w.link(4, endpoint="192.168.77.9:51820"), "wrong endpoint"),
             (w.link(4, endpoint="district.example:51820"), "wrong endpoint")]
    for msg, reason in cases:
        w.lines.clear()
        w.send(msg)
        assert w.main() == 1, reason
        assert w.said() == [f"URBAN-WG refused {reason}"]
        assert w.status()["reason"] == reason and not w.paths.applied.exists() and w.kernel.wg0 is None
        w.now += 61  # the same reason twice in a row is said again only after a minute
    other["version"] = 5  # tampered after signing
    w.send(other)
    assert w.main() == 1 and w.status()["reason"] == "bad signature"
    # the same refusal again within the minute is not said again
    w.send(w.link(4, key=stranger))
    n = len(w.lines)
    assert w.main() == 1 and w.said(n) == []
    # a good message clears the reason
    w.send(w.link(4))
    assert w.main() == 0 and w.status()["reason"] == ""
    assert_never_shown(w)


def test_the_anchor_is_the_helpers_own(tmp_path):
    w = World(tmp_path, "unit")
    # nothing pinned: nothing is obeyed, whatever the service's folder holds
    w.send(w.link(1))
    assert w.main() == 1 and w.said() == ["URBAN-WG refused no trust anchor"]
    assert w.main("pin", "--replace") == 0
    assert w.text(w.paths.trust / "root.pub") == crypto.public_hex(w.root)
    # the service could rewrite its kit's root.pub and its certificate: neither moves the pinned anchor
    fake_root = crypto.new_key()
    (w.paths.pki / "root.pub").write_text(crypto.public_hex(fake_root))
    w.cert_file.write_text(json.dumps(trust.issue(fake_root, crypto.public_hex(w.gateway), 5)))
    w.send(w.link(1))
    assert w.main() == 1 and w.said(1) == ["URBAN-WG refused bad gateway certificate"]
    w.cert_file.unlink()
    w.send(w.link(1))
    w.now += 61
    assert w.main() == 1 and w.said(2) == ["URBAN-WG refused bad gateway certificate"]
    # the genuine certificate: obeyed
    w.cert_file.write_text(json.dumps(w.cert))
    w.send(w.link(1))
    assert w.main() == 0 and json.loads(w.paths.applied.read_text())["cert"] == w.cert


def test_a_kit_without_a_root_key_pins_its_gateway_key(tmp_path):
    w = World(tmp_path, "unit")
    (w.paths.pki / "root.pub").unlink()
    (w.paths.pki / "gateway.pub").write_text(crypto.public_hex(w.gateway) + "\n")
    assert w.main("pin", "--replace") == 0
    assert not (w.paths.trust / "root.pub").exists()
    assert w.text(w.paths.trust / "gateway.pub") == crypto.public_hex(w.gateway)
    w.send(w.link(9))
    assert w.main() == 0
    assert json.loads(w.paths.applied.read_text())["cert"] is None
    assert w.text(w.paths.trust / "floor") == "0 9" and w.status()["epoch"] == 0


def test_messages_are_ordered_by_gateway_epoch_then_version(unit):
    w = unit
    w.send(w.link(100))
    assert w.main() == 0
    w.send(w.link(50, address="10.77.1.3"))
    assert w.main() == 1 and w.said(2)[-1] == "URBAN-WG refused older version"
    assert w.status()["applied"] == 100
    old_gateway, old_cert = w.gateway, w.cert
    # after a rotation the genuine message wins whatever its version
    w.rotate(2)
    w.send(w.link(5, address="10.77.1.3"))
    assert w.main() == 0
    assert w.status()["applied"] == 5 and w.status()["epoch"] == 2 and w.status()["address"] == "10.77.1.3"
    assert w.text(w.paths.trust / "floor") == "2 5" and w.text(w.paths.trust / "epoch") == "2"
    # and nothing under the old key comes back, not even with a huge version
    w.cert_file.write_text(json.dumps(old_cert))
    w.send(w.link(10 ** 14, key=old_gateway))
    assert w.main() == 1 and w.said()[-1] == "URBAN-WG refused older gateway key"
    assert w.status()["applied"] == 5


def test_the_epoch_floor_is_raised_at_boot_from_a_newer_certificate(unit):
    w = unit
    w.send(w.link(3))
    assert w.main() == 0
    old_gateway, old_cert = w.gateway, w.cert
    w.rotate(4)
    w.paths.verified.unlink()
    assert w.main("boot") == 0
    assert w.text(w.paths.trust / "epoch") == "4"
    # the applied message under the older key stays valid (it is checked against its own certificate)
    assert w.status()["applied"] == 3 and "URBAN-WG refused stored message" not in " ".join(w.lines)
    w.cert_file.write_text(json.dumps(old_cert))
    w.send(w.link(9, key=old_gateway))
    assert w.main() == 1 and w.said()[-1] == "URBAN-WG refused older gateway key"


# ---- boot ----------------------------------------------------------------------------------------------------------

def test_boot_pins_a_unit_set_up_before_the_tunnel_and_writes_the_status_without_touching_the_kernel(tmp_path):
    w = World(tmp_path, "unit")
    assert w.main("boot") == 0
    p = w.paths
    assert w.text(p.trust / "root.pub") == crypto.public_hex(w.root)
    assert w.text(p.trust / "epoch") == "1"  # raised from the service's certificate, which the pinned root signed
    assert p.verified.exists() and w.status()["state"] == "none" and w.status()["public"] == w.public
    assert w.kernel.calls == [] and w.lines == []


def test_boot_checks_the_stored_message_again(unit):
    w, p = unit, unit.paths
    up_unit(w)
    floor = w.text(p.trust / "floor")
    rec = json.loads(p.applied.read_text())
    rec["msg"]["address"] = "10.77.1.9"  # changed on the disk: the signature no longer holds
    p.applied.write_text(json.dumps(rec))
    w.kernel = Kernel(p.conf)  # a new boot: no wg0
    p.verified.unlink()
    assert w.main("boot") == 0
    assert w.said()[-1] == "URBAN-WG refused stored message: bad signature"
    assert not p.applied.exists() and not p.proven.exists() and w.text(p.trust / "floor") == floor
    assert w.kernel.calls == [] and w.status()["state"] == "none" and w.status()["reason"] == "bad signature"


def test_the_first_run_of_a_boot_checks_the_stored_message_when_boot_did_not(unit):
    w, p = unit, unit.paths
    up_unit(w)
    rec = json.loads(p.applied.read_text())
    good = json.dumps(rec)
    rec["msg"]["sig"] = "00" * 64
    p.applied.write_text(json.dumps(rec))
    # in the same boot (already checked) the signature is not checked again
    assert w.main() == 0 and w.kernel.wg0 is not None and w.status()["applied"] == 1
    p.verified.unlink()
    assert w.main() == 0
    assert w.said()[-1] == "URBAN-WG refused stored message: bad signature"
    assert w.kernel.wg0 is None and not p.applied.exists() and p.verified.exists()
    assert ["ip", "link", "del", "dev", "wg0"] in w.kernel.calls
    p.applied.write_text(good)  # control: the genuine record passes the same check
    p.verified.unlink()
    n = len(w.lines)
    assert w.main() == 0 and not any("refused" in line for line in w.said(n)) and w.kernel.wg0 is not None


def test_boot_writes_the_district_relay_mode_before_its_services_start(district):
    w = district
    w.send(w.peer_list([], 2, plain=False))
    assert w.main() == 0
    w.paths.relay_mode.unlink()
    w.kernel = Kernel(w.paths.conf)
    w.paths.verified.unlink()
    assert w.main("boot") == 0
    assert w.text(w.paths.relay_mode) == "only" and w.status()["mode"] == "only"
    assert w.status()["state"] == "down" and w.kernel.calls == []


def test_a_new_kit_for_another_place_drops_the_link(unit):
    w, p = unit, unit.paths
    up_unit(w)
    floor = w.text(p.trust / "floor")
    # a kit for the same district, a missing or an unreadable env file: nothing changes
    for change in ("same", "missing", "unreadable"):
        if change == "same":
            w.env.write_text("PLATFORM=192.168.77.1:8443\n")
        elif change == "missing":
            w.env.unlink()
        else:
            w.env.mkdir()
        assert w.main("boot") == 0 and p.applied.exists(), change
        if change == "unreadable":
            w.env.rmdir()
    # a kit for another district
    w.env.write_text("PLATFORM=10.0.0.1:8443\nPLATFORM=192.168.88.1:8443\n")
    assert w.main("boot") == 0
    assert w.said()[-1] == "URBAN-WG dropped the link: the kit points elsewhere"
    assert not p.applied.exists() and not p.proven.exists() and w.text(p.trust / "floor") == floor
    assert w.status()["state"] == "none" and w.status()["proven"] is False
    # the next run takes the interface down
    assert w.main() == 0 and w.kernel.wg0 is None


def test_a_kit_for_the_centre_directly_drops_the_link_at_once(unit):
    w, p = unit, unit.paths
    up_unit(w)
    w.env.write_text("PLATFORM=urban-center:8443\n")
    assert w.main("pin", "--replace") == 0
    assert w.said()[-1] == "URBAN-WG dropped the link: the kit points elsewhere"
    assert w.kernel.wg0 is None and not p.applied.exists() and w.status()["state"] == "none"


# ---- pin -------------------------------------------------------------------------------------------------------------

def test_pin_keeps_the_link_for_the_same_kit_and_starts_over_for_another_anchor(unit):
    w, p = unit, unit.paths
    up_unit(w)
    applied, floor = p.applied.read_text(), w.text(p.trust / "floor")
    # a kit without any valid key: refused, nothing changed
    (p.pki / "root.pub").write_text("not a key")
    assert w.main("pin", "--replace") == 1
    assert p.applied.read_text() == applied and p.proven.exists()
    # the same anchor again (a new kit for the same district): the link stays, proven is earned again, and only by a
    # handshake after the kit (the one from before it is still fresh)
    (p.pki / "root.pub").write_text(crypto.public_hex(w.root) + "\n")
    assert w.main("pin", "--replace") == 0
    assert p.applied.read_text() == applied and w.text(p.trust / "floor") == floor and not p.proven.exists()
    assert w.kernel.wg0 is not None and w.status()["proven"] is False
    assert w.main() == 0 and not p.proven.exists() and w.status()["proven"] is False
    w.now += 20
    w.kernel.wg0["peers"][w.server]["hs"] = w.now - 2
    n = len(w.lines)
    assert w.main() == 0 and w.status()["proven"] is True
    assert w.said(n) == ["URBAN-WG proven district=district-1 server=10.77.1.1"]
    # without --replace nothing pinned is ever replaced
    other = crypto.new_key()
    (p.pki / "root.pub").write_text(crypto.public_hex(other) + "\n")
    assert w.main("pin") == 0 and w.text(p.trust / "root.pub") == crypto.public_hex(w.root)
    # another anchor: everything signed under the old one goes
    assert w.main("pin", "--replace") == 0
    assert w.text(p.trust / "root.pub") == crypto.public_hex(other)
    assert w.text(p.trust / "epoch") == "0" and w.text(p.trust / "floor") == "0 0"
    assert not p.applied.exists() and w.kernel.wg0 is None and w.status()["state"] == "none"
    assert sorted(os.listdir(p.trust)) == ["epoch", "floor", "root.pub"] and sorted(os.listdir(p.wg)) == [
        "key", "key.pub", "trust"]


def test_a_pin_cut_short_leaves_an_anchor_with_its_own_floor_or_none(unit):
    w, p = unit, unit.paths
    up_unit(w)
    # cut short after the old anchor was moved aside: no anchor, nothing obeyed; the next boot pins the kit's
    other = crypto.new_key()
    (p.pki / "root.pub").write_text(crypto.public_hex(other) + "\n")
    p.applied.unlink()
    (p.wg / "trust.new").mkdir()
    (p.wg / "trust.new" / "root.pub").write_text("half made")
    os.replace(p.trust, p.wg / "trust.old")
    p.trust.mkdir()  # urban-prepare makes the folder at every boot
    w.send(w.link(5))
    assert w.main() == 1 and w.said()[-1] == "URBAN-WG refused no trust anchor"
    p.verified.unlink()
    assert w.main("boot") == 0
    assert w.text(p.trust / "root.pub") == crypto.public_hex(other) and w.text(p.trust / "floor") == "0 0"
    assert sorted(os.listdir(p.wg)) == ["key", "key.pub", "trust"]


def test_pin_on_a_district_with_another_anchor_removes_the_relay_mode(district):
    w = district
    w.send(w.peer_list([], 1))
    assert w.main() == 0 and w.text(w.paths.relay_mode) == "allow"
    (w.paths.pki / "root.pub").write_text(crypto.public_hex(crypto.new_key()))
    assert w.main("pin", "--replace") == 0
    assert not w.paths.relay_mode.exists() and w.kernel.wg0 is None and w.status()["mode"] == "off"


# ---- converge --------------------------------------------------------------------------------------------------------

def test_a_failing_command_takes_the_half_made_interface_away_and_shows_nothing_of_the_child(unit):
    w = unit
    w.kernel.fail = [["ip", "address", "add"]]
    w.send(w.link(1))
    assert w.main() == 3
    assert w.said() == ["URBAN-WG applied unit version=1 address=10.77.1.2 endpoint=192.168.77.1:51820",
                        "URBAN-WG command failed: ip address exit 2"]
    failed = w.kernel.calls.index(["ip", "address", "add", "10.77.1.2/32", "dev", "wg0"])
    assert w.kernel.wg0 is None and ["ip", "link", "del", "dev", "wg0"] in w.kernel.since(failed)
    assert w.status()["state"] == "down" and w.status()["applied"] == 1  # the status is written all the same
    assert w.main() == 3 and len(w.lines) == 2  # the same failure is said once
    w.kernel.fail = []
    assert w.main() == 0 and w.kernel.wg0["up"] and w.said()[-1] == "URBAN-WG up unit address=10.77.1.2"
    assert_never_shown(w)


def test_an_error_of_the_helper_is_said_without_any_text_from_outside(unit):
    w = unit
    up_unit(w)

    def broken(argv):
        if argv[:3] == ["wg", "show", "wg0"]:
            raise RuntimeError(f"text from outside {w.private}")
        return w.kernel(argv)
    h = Helper(w.paths, broken, lambda: w.now, lambda p: nullcontext(), w.lines.append)
    w.paths.status.unlink()
    assert main(["run"], h) == 4
    assert w.lines[-1].startswith("URBAN-WG internal error: RuntimeError at ")
    assert "outside" not in w.lines[-1]
    assert w.paths.status.exists()  # written in the finally


def test_the_district_takes_its_peers_from_the_kernel_and_changes_only_what_differs(district):
    w, p = district, district.paths
    a, b, c = (wg.Peer(f"edge-J-0{i}", wg.new_keypair()[1], f"10.77.1.{i + 1}") for i in (1, 2, 3))
    seen = []

    def say(line):  # what the relay would read at the moment each line is said
        seen.append((line, w.text(p.relay_mode)))
    w.lines = []
    h = w.helper(say=lambda line: (say(line), w.lines.append(line)))
    w.send(w.peer_list([a, b], 3))
    assert main(["run"], h) == 0
    assert seen == [("URBAN-WG applied district version=3 peers=2 mode=allow", None),
                    ("URBAN-WG up district address=10.77.1.1 port=51820", "allow")]
    key = str(p.key)
    assert [c_ for c_ in w.kernel.calls if c_[:3] in (["ip", "link", "add"], ["ip", "address", "add"])
            or c_[:4] == ["wg", "set", "wg0", "private-key"]] == [
        ["ip", "link", "add", "dev", "wg0", "type", "wireguard"],
        ["wg", "set", "wg0", "private-key", key, "listen-port", "51820"],
        ["ip", "address", "add", "10.77.1.1/24", "dev", "wg0"]]
    assert {k: v["ips"] for k, v in w.kernel.wg0["peers"].items()} == {a.public: ["10.77.1.2/32"], b.public: ["10.77.1.3/32"]}
    st = w.status()
    assert st == {**st, "role": "district", "state": "up", "applied": 3, "district": DISTRICT, "index": 1,
                  "address": "10.77.1.1", "server": "10.77.1.1", "peers": 2, "peers_up": 0, "mode": "allow",
                  "proven": False, "handshake": 0}
    assert p.screen.read_text().startswith("up 10.77.1.1, mode allow, 0 of 2 unit(s) with a fresh handshake key ")
    # the kernel drifted: b got another address, c was added by hand; a is untouched
    w.kernel.wg0["peers"][b.public]["ips"] = ["10.77.1.9/32"]
    w.kernel.wg0["peers"][c.public] = {"ips": ["10.77.1.4/32"], "hs": 0, "endpoint": None, "keepalive": 0}
    w.kernel.wg0["peers"][a.public]["hs"] = NOW - 30
    start = len(w.kernel.calls)
    assert main(["run"], h) == 0
    assert [x for x in w.kernel.since(start) if x[:2] == ["wg", "set"]] == [
        ["wg", "set", "wg0", "peer", c.public, "remove"],
        ["wg", "set", "wg0", "peer", b.public, "allowed-ips", "10.77.1.3/32"]]
    assert w.status()["peers_up"] == 1
    # a new list without b: b goes, a keeps its session; mode only
    w.send(w.peer_list([a], 4, plain=False))
    start = len(w.kernel.calls)
    assert main(["run"], h) == 0
    assert [x for x in w.kernel.since(start) if x[:2] == ["wg", "set"]] == [["wg", "set", "wg0", "peer", b.public, "remove"]]
    assert seen[-1] == ("URBAN-WG applied district version=4 peers=1 mode=only", "allow")
    assert w.text(p.relay_mode) == "only" and w.status()["mode"] == "only"
    assert_never_shown(w)


def test_a_district_peer_silent_for_ten_minutes_is_removed_and_added_again(district):
    w = district
    a, b, c = (wg.Peer(f"edge-J-0{i}", wg.new_keypair()[1], f"10.77.1.{i + 1}") for i in (1, 2, 3))
    w.send(w.peer_list([a, b, c], 1))
    assert w.main() == 0
    w.kernel.wg0["peers"][a.public]["hs"] = NOW - 601
    w.kernel.wg0["peers"][b.public]["hs"] = NOW - 100
    start = len(w.kernel.calls)
    assert w.main() == 0
    assert [x for x in w.kernel.since(start) if x[:2] == ["wg", "set"]] == [
        ["wg", "set", "wg0", "peer", a.public, "remove"],
        ["wg", "set", "wg0", "peer", a.public, "allowed-ips", "10.77.1.2/32"]]
    assert w.said()[-1] == f"URBAN-WG reset peer {wg.fingerprint(a.public)}: no handshake for 600 s"
    assert w.kernel.wg0["peers"][a.public]["hs"] == 0 and w.kernel.wg0["peers"][b.public]["hs"] == NOW - 100
    n = len(w.lines)
    assert w.main() == 0 and w.said(n) == []  # once per silence


def test_the_unit_keeps_its_one_peer_and_its_route_but_never_switches_wg0_on(unit):
    w = unit
    up_unit(w)
    # switched off by hand: it stays off, and its route is not put back while it is
    w.kernel.wg0["up"] = False
    w.kernel.wg0["routes"].clear()
    start = len(w.kernel.calls)
    assert w.main() == 0
    assert not any(c[:3] == ["ip", "link", "set"] or c[:2] == ["ip", "route"] for c in w.kernel.since(start))
    assert w.status()["state"] == "down" and w.kernel.wg0["routes"] == set()
    w.kernel.wg0["up"] = True
    assert w.main() == 0 and w.kernel.wg0["routes"] == {"10.77.1.1/32"}
    # a stray peer goes and the district's comes back
    stray = wg.new_keypair()[1]
    w.kernel.wg0["peers"] = {stray: {"ips": ["0.0.0.0/0"], "hs": 0, "endpoint": None, "keepalive": 0}}
    assert w.main() == 0
    assert list(w.kernel.wg0["peers"]) == [w.server]
    assert w.kernel.wg0["peers"][w.server] == {"ips": ["10.77.1.1/32"], "hs": 0, "endpoint": ENDPOINT, "keepalive": 25}


def test_another_link_rebuilds_wg0(unit):
    w = unit
    up_unit(w)
    w.send(w.link(2, address="10.77.1.3"))
    start = len(w.kernel.calls)
    assert w.main() == 0
    calls = w.kernel.since(start)
    assert calls.index(["ip", "link", "del", "dev", "wg0"]) < calls.index(["ip", "link", "add", "dev", "wg0", "type", "wireguard"])
    assert w.kernel.wg0["addr"] == ["10.77.1.3/32"]
    assert w.said()[-2:] == ["URBAN-WG applied unit version=2 address=10.77.1.3 endpoint=192.168.77.1:51820",
                             "URBAN-WG up unit address=10.77.1.3"]
    assert w.status()["proven"] is False  # proven was for the old address


def test_cameras_are_forwarded_only_from_the_cabinet_network(unit):
    w, p = unit, unit.paths
    up_unit(w)
    for dev in ("cab0", "up0", "ens3"):
        (p.conf / dev).mkdir()
    w.kernel.routes.update({
        "192.168.50.11": "192.168.50.11 dev cab0 src 192.168.50.1 uid 0 \\    cache ",
        "192.168.50.12": "192.168.50.12 via 10.0.2.2 dev ens3 src 10.0.2.15 uid 0 \\    cache ",
        "192.168.77.5": "192.168.77.5 dev up0 src 192.168.77.2 uid 0 \\    cache ",
        "172.16.0.5": "172.16.0.5 dev wg0 src 10.77.1.2 uid 0 \\    cache ",
        "192.168.50.255": "broadcast 192.168.50.255 dev cab0 src 192.168.50.1 uid 0 \\    cache "})
    cams = [{"name": "cam1", "host": "192.168.50.11", "port": 554, "tunnel_port": 8551},
            {"name": "cam2", "host": "192.168.50.12", "tunnel_port": 8552},
            {"name": "cam3", "host": "192.168.77.5", "tunnel_port": 8553},
            {"name": "cam4", "host": "172.16.0.5", "tunnel_port": 8554},
            {"name": "cam5", "host": "192.168.50.255", "tunnel_port": 8555},
            {"name": "cam6", "host": "192.168.50.13", "tunnel_port": 8556},
            {"name": "bad1", "host": "not an address"},
            {"name": "cam1", "host": "192.168.50.14"}]
    (p.etc / "wg-cameras.json").write_text(json.dumps(cams))
    n = len(w.lines)
    assert w.main() == 0
    assert w.said(n) == ["URBAN-WG camera refused #8: bad entry",
                         "URBAN-WG camera refused bad1: bad entry",
                         "URBAN-WG camera refused cam2: no route",
                         "URBAN-WG camera refused cam3: on the uplink",
                         "URBAN-WG camera refused cam4: on the tunnel",
                         "URBAN-WG camera refused cam5: no route",
                         "URBAN-WG camera refused cam6: no route",
                         "URBAN-WG cameras 1 forwarded"]
    want = ('table ip urban_wg\n'
            'delete table ip urban_wg\n'
            'table ip urban_wg {\n'
            '    chain prerouting {\n'
            '        type nat hook prerouting priority dstnat; policy accept;\n'
            '        iifname "wg0" ip saddr 10.77.1.1 tcp dport 8551 dnat to 192.168.50.11:554\n'
            '    }\n'
            '    chain postrouting {\n'
            '        type nat hook postrouting priority srcnat; policy accept;\n'
            '        ct status dnat ip daddr 192.168.50.11 tcp dport 554 masquerade\n'
            '    }\n'
            '    chain forward {\n'
            '        type filter hook forward priority -150; policy accept;\n'
            '        oifname "wg0" tcp flags syn tcp option maxseg size set rt mtu\n'
            '    }\n'
            '}\n')
    assert p.cameras_nft.read_text() == want and w.kernel.table == want
    if os.name != "nt":
        assert oct(p.cameras_nft.stat().st_mode & 0o777) == "0o600"
    # forwarding on wg0 and the camera's device only; never the uplink, never conf/all or conf/default
    assert (p.conf / "wg0" / "forwarding").read_text() == "1" and (p.conf / "cab0" / "forwarding").read_text() == "1"
    assert not (p.conf / "up0" / "forwarding").exists() and not (p.conf / "ens3" / "forwarding").exists()
    assert not (p.conf / "all").exists() and not (p.conf / "default").exists()
    # unchanged: not loaded again, nothing said
    n, start = len(w.lines), len(w.kernel.calls)
    assert w.main() == 0
    assert w.said(n) == [] and not any(c[:2] == ["nft", "-f"] for c in w.kernel.since(start))
    # a firewall reload flushed the table: loaded again from the same file
    w.kernel.table = None
    assert w.main() == 0 and w.kernel.table == want and w.said(n) == []
    # a camera table that does not load is tried again at the next run
    w.kernel.table = None
    w.kernel.fail = [["nft", "-f"]]
    assert w.main() == 3 and not p.cameras_nft.exists()
    w.kernel.fail = []
    assert w.main() == 0 and w.kernel.table == want
    # the list goes: the table, its file and the camera device's forwarding go too
    (p.etc / "wg-cameras.json").unlink()
    n = len(w.lines)
    assert w.main() == 0
    assert w.said(n) == ["URBAN-WG cameras 0 forwarded"]
    assert w.kernel.table is None and not p.cameras_nft.exists()
    assert (p.conf / "cab0" / "forwarding").read_text() == "0" and (p.conf / "wg0" / "forwarding").read_text() == "1"
    assert_never_shown(w)


def test_without_a_route_to_the_district_no_camera_is_forwarded(unit):
    w, p = unit, unit.paths
    up_unit(w)
    (p.conf / "cab0").mkdir()
    del w.kernel.routes["192.168.77.1"]
    w.kernel.routes["192.168.50.11"] = "192.168.50.11 dev cab0 src 192.168.50.1 uid 0 \\    cache "
    (p.etc / "wg-cameras.json").write_text(json.dumps([{"name": "cam1", "host": "192.168.50.11", "tunnel_port": 8551}]))
    n = len(w.lines)
    assert w.main() == 0
    assert w.said(n) == ["URBAN-WG camera refused cam1: no route"] and w.kernel.table is None


def test_the_camera_table_names_each_camera_once():
    cams = wg.plan_camera_forwards([{"name": "a", "host": "192.168.50.12", "tunnel_port": 8552},
                                    {"name": "b", "host": "192.168.50.11", "port": 8554},
                                    {"name": "c", "host": "192.168.50.12"}])
    text = camera_rules("10.77.3.1", cams)
    assert text.count("dnat to") == 3 and text.count("masquerade") == 2
    assert 'iifname "wg0" ip saddr 10.77.3.1 tcp dport 8551 dnat to 192.168.50.11:8554' in text
    assert text.index("ip daddr 192.168.50.11 tcp dport 8554") < text.index("ip daddr 192.168.50.12 tcp dport 554")


# ---- the clock rule --------------------------------------------------------------------------------------------------

def test_wg0_waits_for_a_clock_that_is_not_before_the_build(unit):
    w, p = unit, unit.paths
    w.send(w.link(1))
    w.now = 1_700_000_000  # 2023: before the build
    assert w.main() == 0
    assert w.said() == ["URBAN-WG applied unit version=1 address=10.77.1.2 endpoint=192.168.77.1:51820",
                        f"URBAN-WG clock waiting: the system time is before the build ({BUILT})"]
    assert w.kernel.wg0 is None and w.status()["state"] == "clock"
    assert p.screen.read_text().startswith("clock: waiting for the time (system time before the build date) key ")
    assert w.main() == 0 and len(w.lines) == 2  # said once
    w.now = NOW  # the time is right again: the tunnel comes up by itself
    assert w.main() == 0 and w.said()[-1] == "URBAN-WG up unit address=10.77.1.2"


def test_a_build_machine_clock_hours_ahead_does_not_hold_the_tunnel(unit):
    # BUILT stamped 12 hours ahead (a build machine in the wrong time zone): the unit's right clock still counts as set
    w, p = unit, unit.paths
    built = datetime.datetime.fromtimestamp(NOW + 12 * 3600, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.release.write_text(f"VERSION=1\nBUILT={built}\nCOMPATIBLE=urban-edge-x86_64\n")
    w.send(w.link(1))
    w.now = NOW
    assert w.main() == 0 and w.said()[-1] == "URBAN-WG up unit address=10.77.1.2" and w.kernel.wg0 is not None
    assert w.status()["state"] != "clock"
    # eight days before it is a clock gone wrong, not a build machine's error: held
    p.release.write_text(f"VERSION=1\nBUILT={built}\n")
    w.kernel.wg0 = None
    w.now = NOW - 8 * 86400
    assert w.main() == 0 and w.kernel.wg0 is None and w.status()["state"] == "clock"


@pytest.mark.parametrize("release", ["VERSION=1\n", "VERSION=1\nBUILT=yesterday\n", "BUILT=2026-10-01 00:00:00\n", None])
def test_without_a_readable_build_date_the_clock_rule_holds(unit, release):
    w = unit
    if release is None:
        w.paths.release.unlink()
    else:
        w.paths.release.write_text(release)
    w.now = 1_000_000_000
    w.send(w.link(1))
    assert w.main() == 0 and w.kernel.wg0 is not None
    assert not any("clock" in line for line in w.lines)


def test_the_clock_state_keeps_proven_so_the_unit_does_not_fall_back_to_the_plain_path(unit):
    w, p = unit, unit.paths
    up_unit(w)
    assert w.status()["proven"] is True
    w.kernel = Kernel(p.conf)  # powered off and on with the clock a year back
    p.verified.unlink()
    w.now = NOW - 400 * 86400
    assert w.main("boot") == 0
    assert w.status()["state"] == "clock" and w.status()["proven"] is True and w.status()["server"] == "10.77.1.1"
    assert w.main() == 0 and w.kernel.wg0 is None and w.status()["state"] == "clock"


# ---- machines without a tunnel, the command line -------------------------------------------------------------------

@pytest.mark.parametrize("conf", ["UNIT=light-C-01\nROLE=unit\nMODULE=lighting\n", "UNIT=center\nROLE=district,center\n",
                                  "UNIT=center\nROLE=center\n", "UNIT=edge-J-01\n", None])
def test_a_machine_without_a_tunnel_gets_nothing(tmp_path, conf):
    w = World(tmp_path, "unit")
    if conf is None:
        (w.paths.etc / "unit.conf").unlink()
    else:
        (w.paths.etc / "unit.conf").write_text(conf)
    w.send(w.link(1))
    for argv in (["boot"], ["run"], ["pin", "--replace"], ["pin"]):
        assert w.main(*argv) == 0
    assert w.kernel.calls == [] and w.lines == [] and w.locked == []
    assert not w.paths.status.exists() and not (w.paths.trust / "root.pub").exists()


def test_the_command_line(unit, capsys):
    w = unit
    for argv in ([], ["bogus"], ["pin", "--force"], ["run", "now"]):
        assert main(argv, w.helper()) == 2
    assert "usage: urban-wg" in capsys.readouterr().err
    assert main(["status"], w.helper()) == 0
    assert json.loads(capsys.readouterr().out) == w.status()


# ---- what reads the helper's files: the district's relay; and the firewall pieces urban-prepare writes -----------------

ROOTFS = BACKEND.parent / "os" / "rootfs"


def test_the_relay_takes_its_mode_from_the_helper_and_keeps_its_rules_for_off_and_allow(tmp_path):
    relay = runpy.run_path(str(ROOTFS / "usr" / "local" / "bin" / "urban-relay"), run_name="urban_relay")
    rules, read_mode = relay["rules"], relay["read_mode"]
    today = ("\ntable ip urban_relay\ndelete table ip urban_relay\ntable ip urban_relay {\n"
             "    set units { type ipv4_addr; flags dynamic, timeout; timeout 30s; }\n"
             "    counter sessions { }\n    counter up { }\n    counter down { }\n"
             "    chain prerouting {\n        type nat hook prerouting priority dstnat; policy accept;\n"
             "        ip saddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } tcp dport 8443 dnat to 10.0.2.2:28443\n"
             "    }\n"
             "    chain postrouting {\n        type nat hook postrouting priority srcnat; policy accept;\n"
             "        ip daddr 10.0.2.2 tcp dport 28443 ct status dnat masquerade\n    }\n"
             "    chain forward {\n        type filter hook forward priority -10; policy accept;\n"
             "        ip daddr 10.0.2.2 tcp dport 28443 ct status dnat ct state new counter name sessions\n"
             "        ip daddr 10.0.2.2 tcp dport 28443 ct status dnat update @units { ip saddr } counter name up\n"
             "        ip saddr 10.0.2.2 tcp sport 28443 ct status dnat counter name down\n    }\n}\n")
    # off and allow: exactly the rules from before the tunnel, so a switch between them rebuilds nothing
    assert rules("10.0.2.2", 28443) == rules("10.0.2.2", 28443, "off") == rules("10.0.2.2", 28443, "allow") == today
    only = rules("10.0.2.2", 28443, "only").splitlines()
    assert 'iifname "wg0" tcp dport 8443 dnat to 10.0.2.2:28443' in [line.strip() for line in only]
    assert not any("ip saddr {" in line for line in only)
    forward = only.index("    chain forward {")
    assert only[forward + 2].strip() == 'ip daddr 10.0.2.2 tcp dport 28443 ct status dnat iifname != "wg0" counter drop'
    assert [line for line in only if "dnat" not in line] == [line for line in today.splitlines() if "dnat" not in line]
    # the mode file: allow or only, anything else (none, a word of an intruder's, a directory) is off
    mode = tmp_path / "relay-mode"
    read_mode.__globals__["MODE"] = str(mode)
    assert read_mode() == "off"
    for text, want in (("allow\n", "allow"), ("only", "only"), ("ONLY\n", "off"), ("only please\n", "off"), ("", "off")):
        mode.write_text(text)
        assert read_mode() == want, text
    mode.unlink()
    mode.mkdir()
    assert read_mode() == "off"


def test_the_firewall_pieces_are_exactly_the_rules_the_checks_find_by_their_comments():
    prepare = (ROOTFS / "usr" / "local" / "bin" / "urban-prepare").read_text(encoding="utf-8")

    def fragment(name):
        start = prepare.index(f"{name}() {{\n  cat <<'EOF'\n") + len(f"{name}() {{\n  cat <<'EOF'\n")
        return prepare[start:prepare.index("\nEOF\n", start)].splitlines()
    tunnel_only = 'counter drop comment "urban-wg: tunnel addresses only through the tunnel"'
    assert fragment("wg_nft_unit") == [
        f'insert rule inet urban output ip daddr 10.77.0.0/16 oifname != {{ "wg0", "lo" }} {tunnel_only}',
        'insert rule inet urban output icmp type destination-unreachable icmp code port-unreachable counter drop '
        'comment "urban-wg: closed ports stay silent"',
        'add rule inet urban output udp dport 51820 accept comment "urban-wg: WireGuard to the district"',
        f'insert rule inet urban forward ip daddr 10.77.0.0/16 oifname != "wg0" {tunnel_only}',
        'add rule inet urban forward iifname "wg0" ct status dnat accept comment "urban-wg: the district to the cabinet cameras"',
        'add rule inet urban forward oifname "wg0" ct state established,related accept comment "urban-wg: the cameras\' answers"']
    assert fragment("wg_nft_district") == [
        'insert rule inet urban input iifname "wg0" meta l4proto != icmp ct state new counter drop '
        'comment "urban-wg: nothing new from the tunnel into the district itself"',
        'add rule inet urban input udp dport 51820 accept comment "urban-wg: WireGuard from the units"',
        'add rule inet urban output udp sport 51820 accept comment "urban-wg: WireGuard to the units"',
        f'insert rule inet urban output ip daddr 10.77.0.0/16 oifname != {{ "wg0", "lo" }} {tunnel_only}',
        'insert rule inet urban output icmp type destination-unreachable icmp code port-unreachable counter drop '
        'comment "urban-wg: closed ports stay silent"',
        'add chain inet urban wg_vision',
        'add rule inet urban wg_vision oifname "lo" return',
        'add rule inet urban wg_vision oifname "wg0" ip daddr 10.77.0.0/16 tcp dport 8551-8599 return',
        'add rule inet urban wg_vision udp sport 51820 return comment "urban-wg: WireGuard\'s own packets (the kernel '
        'keeps the sender\'s socket on them)"',
        'add rule inet urban wg_vision counter drop comment "urban-wg: the district\'s vision reads cameras only through the tunnel"',
        'insert rule inet urban output meta skuid 991 jump wg_vision comment "urban-wg: the district\'s vision"',
        f'insert rule inet urban forward ip daddr 10.77.0.0/16 oifname != "wg0" {tunnel_only}',
        'add rule inet urban forward iifname "wg0" oifname "wg0" counter drop comment "urban-wg: units do not reach each other"']
    for need in ("wg-inbox", "urban-wg.path", "urban-wg.timer", "urban.net.wireguard keygen", "urban-wg boot",
                 "/run/urban/nft/wg.nft", "udp dport 51820", "oifname != "):
        assert need in prepare, need
