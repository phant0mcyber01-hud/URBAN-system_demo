"""The root helper of the WireGuard tunnel between a junction unit and its district server: `urban-wg`.

The centre signs a peer list for a district and a link for a unit (urban/net/wireguard.py). The machine's own service
(user urban, sandboxed: urban/edge/tunnel.py) checks it and puts it, as it came, into /var/lib/urban/wg-inbox. This
helper runs as root (urban-wg.service: CAP_NET_ADMIN, and CAP_DAC_OVERRIDE to read the service's files) and trusts
nothing it finds there or in the service's files. It checks the message again against a trust anchor of its own (the
kit's root.pub or gateway.pub, pinned when the kit was installed), orders messages by (gateway key epoch, version),
binds a unit's link to the district its kit names, and only then sets up wg0, its peers and the cameras' forwards.

    urban-wg run              take the inbox's message if there is one, then bring wg0, its peers, the district's
                              relay mode and the unit's camera forwards in line with the applied message
                              (urban-wg.service, started by urban-wg.path and every 30 s by urban-wg.timer)
    urban-wg boot             every boot, from urban-prepare before the role's services start: the anchor pinned if
                              none is (units set up before the tunnel existed), the stored message checked again, the
                              kit compared with it, the status written; no interface and no firewall is touched
    urban-wg pin [--replace]  pin the kit's root.pub / gateway.pub as the helper's anchor (urban-unit setup: --replace)
    urban-wg status           print the status (/run/urban/wg.json)

Exit: 0 done or nothing to do; 1 a message was refused (the rest still converged), or pin found no anchor; 2 usage;
3 a wg, ip or nft command failed (the next run starts again from what the kernel holds); 4 an error of the helper itself.

What it keeps, root's only: /var/lib/urban/wg (the private key, the pinned anchor, the applied message, the proven
link) and in /run/urban the status the service reports, the screen line, the district's relay mode and its own memory.
A child's output is never shown, and the private key is only ever named by its file. Its lines on the console start
URBAN-WG and are printed only when something changes. Design: docs/wireguard-design.md.

Only a junction unit (ROLE=unit, not a lighting cabinet) and a district server (ROLE=district) have a tunnel; on any
other machine every command does nothing. A run with nothing applied, nothing in the inbox and no wg0 imports neither
urban.net.wireguard nor the signature code: it only rewrites the status.
"""
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

IFACE = "wg0"                # as wireguard.IFACE, which is not imported for a run with nothing to do
MAX_MESSAGE = 65536          # bytes of an inbox message (the service refuses larger ones too)
MAX_SMALL = 4096             # bytes of a certificate, a public key, unit.conf or the kit's env file
PEER_RESET = 600             # seconds: a district peer silent this long is removed and added again (clock poisoning)
CLOCK_SLACK = 7 * 86400      # seconds before BUILT= the clock may be and still count as set (a build machine's error)
CONSOLE = "/usr/local/bin/urban-console"
# why a message is not applied, in fixed words: never text taken from the message
REASONS = ("bad file", "no trust anchor", "bad gateway certificate", "older gateway key", "bad signature",
           "not addressed here", "older version", "wrong endpoint", "malformed")
# this machine's id by role, as urban.devices.enrolment.DEVICE spells it (not imported: a run with nothing to do stays light)
DEVICE_NAME = {"unit": re.compile(r"^edge-[A-Za-z0-9._-]{1,54}$"), "district": re.compile(r"^district-[A-Za-z0-9._-]{1,54}$")}
NETDEV = re.compile(r"^[A-Za-z0-9_.@-]{1,15}$")  # an interface name as `ip route get` prints it
STATES = ("none", "clock", "up", "down")
USAGE = "urban-wg run | boot | pin [--replace] | status"


def _flag(name: str) -> int:
    """An open flag this system may not have (O_NOFOLLOW and friends do not exist on Windows, where the tests run)."""
    return getattr(os, name, 0)


def _wireguard():
    from . import wireguard  # imported only when there is a message or a wg0 to deal with
    return wireguard


def _signatures():
    from ..devices import crypto, trust
    return crypto, trust


class BadFile(Exception):
    """Not a small regular file with one name: a link, a directory, a FIFO, a hard link, too large."""


class Refused(Exception):
    """A message that is not applied; the text is one of REASONS."""


class CommandFailed(Exception):
    def __init__(self, argv, code: int):
        super().__init__(f"{argv[0]} {argv[1] if len(argv) > 1 else '-'} exit {code}")


# ---- files -------------------------------------------------------------------------------------------------------

def read_hostile(path: Path, limit: int) -> bytes:
    """The bytes of a file someone else may have planted: never through a link, only a regular file with one name, at
    most `limit` bytes. FileNotFoundError if there is nothing; BadFile for anything there that is not such a file."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise BadFile(str(path))
    try:
        fd = os.open(path, os.O_RDONLY | _flag("O_NOFOLLOW") | _flag("O_NONBLOCK") | _flag("O_CLOEXEC") | _flag("O_BINARY"))
    except FileNotFoundError:
        raise
    except OSError:
        raise BadFile(str(path))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > limit:
            raise BadFile(str(path))
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise BadFile(str(path))
        return data
    finally:
        os.close(fd)


def strict_json(data: bytes):
    """UTF-8 JSON and nothing else (no NaN or Infinity, no byte order mark). ValueError otherwise."""
    def constant(name):
        raise ValueError(f"{name} is not JSON")
    try:
        return json.loads(data.decode("utf-8"), parse_constant=constant)
    except RecursionError:
        raise ValueError("nested too deep")


def remove_any(path: Path):
    """Remove whatever is at `path` without following it: a link is removed as a link, a directory with its contents."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def write_file(path: Path, text: str, mode: int, sync: bool = False):
    """Write through a temporary file and a rename, so that a reader never sees half of it; the mode is set by chmod,
    never by the umask. sync: flushed to the disk first (the data partition)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _flag("O_NOFOLLOW") | _flag("O_CLOEXEC") | _flag("O_BINARY"), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(text.encode("utf-8"))
        f.flush()
        if sync:
            os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def fingerprint(public) -> str:
    """wireguard.fingerprint without importing it (XXXX-XXXX-XXXX-XXXX), or "none" for a text that is no key."""
    try:
        raw = base64.b64decode(str(public).strip(), validate=True)
    except ValueError:
        return "none"
    if len(raw) != 32:
        return "none"
    digest = hashlib.sha256(raw).hexdigest()[:16].upper()
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def ipv4(text) -> str | None:
    """The IPv4 literal in its one spelling, or None for anything else (a host name, IPv6, garbage)."""
    try:
        return str(ipaddress.IPv4Address(str(text).strip()))
    except ValueError:
        return None


def host_of(address: str) -> str:
    """host:port -> host (an address without a port is its own host)."""
    return address.rpartition(":")[0] if ":" in address else address


def words(err) -> str:
    """A MessageRefused of wireguard.parse_* in the helper's fixed words."""
    text = str(err)
    if text == "bad signature":
        return "bad signature"
    if text == "no gateway key yet":
        return "no trust anchor"
    if text.startswith("addressed to "):
        return "not addressed here"
    if " is older than " in text:
        return "older version"
    return "malformed"


# ---- the world outside: paths, commands, the clock, the lock ---------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    data: Path = Path("/var/lib/urban")
    run: Path = Path("/run/urban")
    release: Path = Path("/etc/urban-os-release")
    conf: Path = Path("/proc/sys/net/ipv4/conf")  # <device>/forwarding

    @property
    def etc(self) -> Path:
        return self.data / "etc"

    @property
    def pki(self) -> Path:
        return self.etc / "pki"

    @property
    def wg(self) -> Path:
        return self.data / "wg"

    @property
    def trust(self) -> Path:
        return self.wg / "trust"

    @property
    def inbox(self) -> Path:
        return self.data / "wg-inbox"

    @property
    def key(self) -> Path:
        return self.wg / "key"

    @property
    def applied(self) -> Path:
        return self.wg / "applied.json"

    @property
    def proven(self) -> Path:
        return self.wg / "proven"

    @property
    def status(self) -> Path:
        return self.run / "wg.json"

    @property
    def screen(self) -> Path:
        return self.run / "wg.txt"

    @property
    def relay_mode(self) -> Path:
        return self.run / "relay-mode"

    @property
    def lock(self) -> Path:
        return self.run / "wg.lock"

    @property
    def verified(self) -> Path:
        return self.run / "wg.verified"

    @property
    def memory(self) -> Path:
        return self.run / "wg-state.json"

    @property
    def cameras_nft(self) -> Path:
        return self.run / "nft" / "wg-cams.nft"


def run_command(argv) -> tuple[int, str]:
    """A command's exit code and its standard output, which is parsed, never shown; its errors are thrown away."""
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=30)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 126, ""
    return r.returncode, r.stdout or ""


@contextmanager
def file_lock(path: Path):
    """Every urban-wg invocation holds /run/urban/wg.lock: the timer, the path unit, boot and pin never overlap."""
    import fcntl  # Linux only; the tests (Windows too) pass a lock of their own
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _flag("O_NOFOLLOW") | _flag("O_CLOEXEC"), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ---- the helper ----------------------------------------------------------------------------------------------------

class Helper:
    def __init__(self, paths: Paths | None = None, runner=None, clock=None, lock=None, say=None):
        """paths: where everything lives; runner(argv) -> (exit code, stdout); clock() -> unix time;
        lock(path): a context manager; say(line): a console line (stdout, which the unit sends to the console)."""
        self.p = paths or Paths()
        self.runner = runner or run_command
        self.clock = clock or time.time
        self.lock = lock or file_lock
        self.say = say or (lambda line: print(line, flush=True))
        self.mem: dict = {}
        self.role = self.unit = None

    # ---- who this machine is
    def identity(self) -> bool:
        """Role and name from unit.conf. True only on a tunnel machine: exactly ROLE=unit without MODULE=lighting or
        MODULE=water, or exactly ROLE=district (the pilot's district,center, a centre, a lighting cabinet and a pump
        station have no tunnel)."""
        try:
            text = read_hostile(self.p.etc / "unit.conf", MAX_SMALL).decode("utf-8")
        except (OSError, BadFile, ValueError):
            return False
        values = {}
        for line in text.splitlines():
            name, sep, value = line.partition("=")
            if sep and name in ("UNIT", "ROLE", "MODULE"):
                values[name] = value.strip()
        role, unit = values.get("ROLE"), values.get("UNIT", "")
        if role == "unit" and values.get("MODULE") in ("lighting", "water"):
            return False
        if role not in DEVICE_NAME or not DEVICE_NAME[role].fullmatch(unit):
            return False
        self.role, self.unit = role, unit
        return True

    # ---- memory (what was printed, which devices got forwarding, what wg0 was built from), kept in /run
    def load_memory(self):
        try:
            mem = json.loads(self.p.memory.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mem = {}
        self.mem = mem if isinstance(mem, dict) else {}

    def save_memory(self):
        write_file(self.p.memory, json.dumps(self.mem, sort_keys=True), 0o600)

    # ---- commands
    def cmd(self, argv) -> tuple[int, str]:
        return self.runner([str(a) for a in argv])

    def check(self, argv) -> str:
        code, out = self.cmd(argv)
        if code != 0:
            raise CommandFailed(argv, code)
        return out

    def link_state(self) -> tuple[bool, bool]:
        """(wg0 exists, wg0 is administratively up)."""
        code, out = self.cmd(["ip", "-o", "link", "show", "dev", IFACE])
        if code != 0:
            return False, False
        m = re.search(r"<([^>]*)>", out)
        return True, bool(m and "UP" in m.group(1).split(","))

    def route(self, host: str) -> tuple[str | None, bool]:
        """(the device `ip route get` sends to host through, whether it goes through a gateway); (None, False) if no
        route or not an ordinary one (local, broadcast, unreachable ...)."""
        code, out = self.cmd(["ip", "-4", "-o", "route", "get", host])
        words_ = out.split()
        if code != 0 or not words_ or words_[0] != host or "dev" not in words_:
            return None, False
        i = words_.index("dev")
        dev = words_[i + 1] if i + 1 < len(words_) else None
        return (dev if dev and NETDEV.fullmatch(dev) else None), "via" in words_

    def handshakes(self) -> dict:
        wg = _wireguard()
        code, out = self.cmd(["wg", "show", IFACE, "latest-handshakes"])
        return wg.parse_latest_handshakes(out) if code == 0 else {}

    # ---- the pinned anchor and the ordering state
    def key_text(self, path: Path) -> str | None:
        """A public key file (hex, Ed25519) read as a hostile file, or None if absent or not a key."""
        try:
            text = read_hostile(path, MAX_SMALL).decode("ascii").strip().lower()
        except (OSError, BadFile, ValueError):
            return None
        crypto, _ = _signatures()
        try:
            crypto.load_public(text)
        except ValueError:
            return None
        return text

    def pinned(self) -> tuple[str | None, str | None]:
        return self.key_text(self.p.trust / "root.pub"), self.key_text(self.p.trust / "gateway.pub")

    def anything_pinned(self) -> bool:
        return any(os.path.lexists(self.p.trust / n) for n in ("root.pub", "gateway.pub"))

    def number(self, path: Path, count: int) -> list[int]:
        try:
            parts = [int(x) for x in path.read_text(encoding="ascii").split()]
        except (OSError, ValueError):
            parts = []
        return [max(0, x) for x in parts] if len(parts) == count else [0] * count

    def epoch(self) -> int:
        return self.number(self.p.trust / "epoch", 1)[0]

    def floor(self) -> tuple[int, int]:
        e, v = self.number(self.p.trust / "floor", 2)
        return e, v

    def certificate(self):
        """This machine's gateway certificate as its service keeps it (pki/edge/<id>.gateway.json), or None."""
        try:
            cert = strict_json(read_hostile(self.p.pki / "edge" / f"{self.unit}.gateway.json", MAX_SMALL))
        except (OSError, BadFile, ValueError):
            return None
        return cert if isinstance(cert, dict) else None

    def kit_host(self) -> str | None:
        """The host of the last PLATFORM= line of the kit's env file (edge-<id>.env, root's), or None."""
        try:
            text = read_hostile(self.p.etc / f"edge-{self.unit}.env", MAX_SMALL).decode("utf-8")
        except (OSError, BadFile, ValueError):
            return None
        value = None
        for line in text.splitlines():
            line = line.lstrip()
            if line.startswith("PLATFORM="):
                value = line[len("PLATFORM="):].strip()
        if value and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        if not value:
            return None
        return host_of(value) or None

    # ---- the applied message
    def stored(self) -> dict | None:
        """applied.json as written ({"msg", "cert", "epoch"}), {} if it is there but unreadable, None if absent."""
        try:
            rec = json.loads(read_hostile(self.p.applied, 2 * MAX_MESSAGE).decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, BadFile, ValueError):
            return {}
        return rec if isinstance(rec, dict) else {}

    def unsigned(self, msg):
        """The stored message as a Link or PeerList without its signature checked again (that was done at boot), or
        None if it is not one for this machine."""
        wg = _wireguard()
        kind = wg.LINK_TYPE if self.role == "unit" else wg.PEERS_TYPE
        if not isinstance(msg, dict) or msg.get("type") != kind:
            return None
        try:
            if self.role == "unit":
                if msg["unit"] != self.unit:
                    return None
                return wg.make_link(wg.Plan.of(), msg["unit"], msg["district"], msg["index"], msg["version"], msg["t"],
                                    msg["address"], msg["server_public"], msg["endpoint"])
            if msg["district"] != self.unit:
                return None
            return wg.make_peer_list(wg.Plan.of(), msg["district"], msg["index"], msg["version"], msg["t"], msg["peers"],
                                     msg["plain_relay"])
        except (KeyError, TypeError, ValueError):
            return None

    def applied(self):
        """(Link or PeerList, the record) of what is applied, or (None, None)."""
        rec = self.stored()
        if not rec:
            return None, None
        obj = self.unsigned(rec.get("msg"))
        return (obj, rec) if obj is not None else (None, None)

    def parse(self, msg, gateway, last_version: int):
        wg = _wireguard()
        parse = wg.parse_link if self.role == "unit" else wg.parse_peer_list
        try:
            return parse(msg, gateway, wg.Plan.of(), self.unit, last_version=last_version)
        except wg.MessageRefused as err:
            raise Refused(words(err))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise Refused("malformed")

    def verify(self, msg):
        """A message from the inbox, checked from scratch: (Link or PeerList, certificate or None, its epoch)."""
        crypto, trust = _signatures()
        root, gateway = self.pinned()
        if root is not None:
            cert = self.certificate()
            if cert is None or not trust.valid(crypto.load_public(root), cert):
                raise Refused("bad gateway certificate")
            if cert["epoch"] < self.epoch():
                raise Refused("older gateway key")
            key, epoch = crypto.load_public(cert["gateway"]), cert["epoch"]
        elif gateway is not None:
            key, epoch, cert = crypto.load_public(gateway), 0, None
        else:
            raise Refused("no trust anchor")
        floor_epoch, floor_version = self.floor()
        if epoch < floor_epoch:
            raise Refused("older gateway key")
        # a message under a newer gateway key replaces anything signed under the old one, whatever its version
        obj = self.parse(msg, key, floor_version if epoch == floor_epoch else 0)
        if self.role == "unit":
            kit = ipv4(self.kit_host())
            if kit is None or ipv4(host_of(obj.endpoint)) != kit:
                raise Refused("wrong endpoint")
        return obj, cert, epoch

    def verify_stored(self, rec):
        """The stored message against the certificate stored with it (no epoch floor: an applied message stays valid
        until a newer one arrives) or the pinned gateway key."""
        crypto, trust = _signatures()
        if not rec:
            raise Refused("malformed")
        root, gateway = self.pinned()
        cert = rec.get("cert")
        if cert is not None:
            if root is None:
                raise Refused("no trust anchor")
            if not trust.valid(crypto.load_public(root), cert):
                raise Refused("bad gateway certificate")
            key = crypto.load_public(cert["gateway"])
        elif gateway is not None:
            key = crypto.load_public(gateway)
        else:
            raise Refused("no trust anchor")
        return self.parse(rec.get("msg"), key, 0)

    # ---- dropping what is applied
    def drop(self, kernel: bool):
        """The applied message goes, with its interface and camera table; trust/floor stays (no replay of anything
        older afterwards)."""
        if kernel:
            if self.link_state()[0]:
                self.check(["ip", "link", "del", "dev", IFACE])
            self.cameras_off()
        remove_any(self.p.applied)
        remove_any(self.p.proven)

    def reverify(self, kernel: bool):
        """Once per boot: the stored message checked again in full; a failing one is dropped."""
        rec = self.stored()
        if rec is not None:
            try:
                self.verify_stored(rec)
            except Refused as err:
                self.drop(kernel)
                self.mem["reason"] = str(err)
                self.say(f"URBAN-WG refused stored message: {err}")
        write_file(self.p.verified, "", 0o600)

    def kit_check(self, kernel: bool):
        """A unit's new kit for another district or for the centre directly: the old link goes (boot, pin --replace)."""
        if self.role != "unit":
            return
        link, _ = self.applied()
        host = self.kit_host()
        if link is None or host is None:
            return
        endpoint = host_of(link.endpoint)
        if (ipv4(host) or host) != (ipv4(endpoint) or endpoint):
            self.drop(kernel)
            self.say("URBAN-WG dropped the link: the kit points elsewhere")

    # ---- the inbox
    def take(self):
        """The inbox's message as a dict, None if there is none, Refused("bad file") for anything that is not one.
        It is renamed first (in the same folder: each sandbox path is a mount of its own), so the path unit is
        satisfied at once and a newer message written meanwhile is not lost."""
        message, taken = self.p.inbox / "message.json", self.p.inbox / "taken.json"
        try:
            remove_any(taken)
        except OSError:
            pass
        if not os.path.lexists(message):
            return None
        try:
            os.replace(message, taken)
        except OSError:
            try:
                remove_any(message)
            except OSError:
                pass
            raise Refused("bad file")
        try:
            msg = strict_json(read_hostile(taken, MAX_MESSAGE))
        except (OSError, BadFile, ValueError):
            raise Refused("bad file")
        finally:
            try:
                remove_any(taken)
            except OSError:
                pass
        if not isinstance(msg, dict):
            raise Refused("bad file")
        return msg

    def intake(self) -> bool:
        """The inbox's message applied if it verifies. False if one was refused."""
        try:
            msg = self.take()
            if msg is None:
                return True
            obj, cert, epoch = self.verify(msg)
        except Refused as err:
            self.refused(str(err))
            return False
        rec = {"msg": msg, "cert": cert, "epoch": epoch}
        os.makedirs(self.p.trust, mode=0o700, exist_ok=True)
        if rec != self.stored():
            write_file(self.p.applied, json.dumps(rec, sort_keys=True), 0o600, sync=True)
            if self.role == "unit":
                self.say(f"URBAN-WG applied unit version={obj.version} address={obj.address} endpoint={obj.endpoint}")
            else:
                self.say(f"URBAN-WG applied district version={obj.version} peers={len(obj.peers)} "
                         f"mode={'allow' if obj.plain_relay else 'only'}")
        write_file(self.p.trust / "epoch", f"{max(self.epoch(), epoch)}\n", 0o600, sync=True)
        write_file(self.p.trust / "floor", f"{epoch} {obj.version}\n", 0o600, sync=True)
        self.mem["reason"] = ""
        self.mem.pop("refused", None)
        return True

    def refused(self, reason: str):
        """Said on the console when the reason differs from the last one said, or that one is a minute old."""
        self.mem["reason"] = reason
        last = self.mem.get("refused")
        now = self.clock()
        if not (isinstance(last, list) and len(last) == 2 and last[0] == reason and isinstance(last[1], (int, float))
                and 0 <= now - last[1] < 60):
            self.say(f"URBAN-WG refused {reason}")
            self.mem["refused"] = [reason, now]

    # ---- pinning the kit's anchor
    def pin(self, replace: bool, kernel: bool) -> bool:
        """The kit's root.pub / gateway.pub become the helper's anchor. False (nothing changed) if neither is a key."""
        if not replace and self.anything_pinned():
            return True
        root, gateway = (self.key_text(self.p.pki / n) for n in ("root.pub", "gateway.pub"))
        if root is None and gateway is None:
            return False
        if (root, gateway) == self.pinned() and self.anything_pinned():
            # the same district's new kit: the link stays, and it is proven again only by a handshake after this moment
            remove_any(self.p.proven)
            self.mem["prove_after"] = int(self.clock())
            return True
        # another anchor: nothing signed under the old one stands
        if kernel:
            if self.link_state()[0]:
                self.check(["ip", "link", "del", "dev", IFACE])
            self.cameras_off()
        remove_any(self.p.applied)
        remove_any(self.p.proven)
        remove_any(self.p.relay_mode)
        # The new anchor with a fresh epoch and floor is made beside the old one and put in its place by renames. Cut
        # short anywhere, this leaves the old anchor with its floor, the new one with a fresh floor, or no anchor at all
        # (the next boot pins the kit's then), never a fresh floor under the old anchor (which would let an old message
        # of it in again).
        new, old = self.p.wg / "trust.new", self.p.wg / "trust.old"
        remove_any(new)
        remove_any(old)
        os.makedirs(new, mode=0o700)
        write_file(new / "epoch", "0\n", 0o600, sync=True)
        write_file(new / "floor", "0 0\n", 0o600, sync=True)
        for name, text in (("root.pub", root), ("gateway.pub", gateway)):
            if text is not None:
                write_file(new / name, text + "\n", 0o600, sync=True)
        if os.path.lexists(self.p.trust):
            os.replace(self.p.trust, old)
        os.replace(new, self.p.trust)
        remove_any(old)
        return True

    def raise_epoch(self):
        """The service's certificate, if the pinned root signed it and it is newer, raises the epoch floor."""
        root, _ = self.pinned()
        cert = self.certificate()
        if root is None or cert is None:
            return
        crypto, trust = _signatures()
        if trust.valid(crypto.load_public(root), cert) and cert["epoch"] > self.epoch():
            write_file(self.p.trust / "epoch", f"{cert['epoch']}\n", 0o600, sync=True)

    # ---- the clock rule
    def clock_ok(self) -> tuple[bool, str]:
        """(time is not before the build, BUILT=). Without a readable BUILT the rule holds. The rule is there for a board
        whose clock battery died (years back); a build machine whose own clock was hours or days off (a wrong time zone
        gives 12 h) must not keep every unit of that release off its tunnel, so CLOCK_SLACK of it is forgiven."""
        try:
            text = self.p.release.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True, ""
        built = ""
        for line in text.splitlines():
            if line.startswith("BUILT="):
                built = line[len("BUILT="):].strip()
        try:
            when = datetime.datetime.strptime(built, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return True, ""
        return self.clock() >= when.timestamp() - CLOCK_SLACK, built

    # ---- converge: the kernel in line with the applied message
    def converge(self) -> int:
        """0, or 3 when a command failed (said once; the next run starts again from what the kernel holds)."""
        try:
            self._converge()
        except CommandFailed as err:
            line = f"URBAN-WG command failed: {err}"
            if self.mem.get("failed") != line:
                self.say(line)
            self.mem["failed"] = line
            return 3
        self.mem.pop("failed", None)
        return 0

    def _converge(self):
        obj, _ = self.applied()
        if obj is None:
            if self.link_state()[0]:
                self.check(["ip", "link", "del", "dev", IFACE])
            remove_any(self.p.relay_mode)
            self.cameras_off()
            self.forwarding(set())
            self.mem.pop("built", None)
            return
        wg = _wireguard()
        if self.role == "district":
            self.relay_mode(obj)
        built = ({"address": obj.address, "index": obj.index, "server_public": obj.server_public,
                  "endpoint": obj.endpoint} if self.role == "unit" else {"index": obj.index})
        present, up = self.link_state()
        if present and self.mem.get("built") != built:
            self.check(["ip", "link", "del", "dev", IFACE])  # another link (address, district, key or endpoint)
            present = up = False
        if not present:
            ok, when = self.clock_ok()
            if not ok:
                if not self.mem.get("clock"):
                    self.say(f"URBAN-WG clock waiting: the system time is before the build ({when})")
                self.mem["clock"] = True
                self.cameras_off()
                self.forwarding(set())
                return
            self.create(obj, wg)
            self.mem["built"] = built
            present = up = True
        self.mem.pop("clock", None)
        if self.role == "district":
            self.district_peers(obj, wg)
            return
        self.unit_peer(obj, wg, up)
        self.prove(obj, wg)  # before the cameras: a camera table that fails must not hold the unit on the plain path
        devices = self.cameras(obj, wg)
        self.forwarding({IFACE} | devices)

    def create(self, obj, wg):
        """wg0 from scratch; if any step fails, whatever was made goes again."""
        plan, key = wg.Plan.of(), str(self.p.key)
        steps = (wg.unit_up_commands(plan, obj, key) if self.role == "unit"
                 else wg.district_up_commands(plan, obj.index, key))
        try:
            for argv in steps:
                self.check(argv)
        except CommandFailed:
            self.cmd(["ip", "link", "del", "dev", IFACE])
            raise
        if self.role == "unit":
            self.say(f"URBAN-WG up unit address={obj.address}")
        else:
            self.say(f"URBAN-WG up district address={plan.server(obj.index)} port={wg.PORT}")

    def district_peers(self, lst, wg):
        """The peers the kernel holds turned into the list's with the fewest `wg set` commands, then a listed peer
        that has been silent too long removed and added again: WireGuard then forgets the newest handshake time it
        saw from that unit, so a unit whose clock once ran ahead is not shut out for good."""
        listed = {p.public: p for p in lst.peers}
        old = []
        for line in self.check(["wg", "show", IFACE, "allowed-ips"]).splitlines():
            parts = line.split()
            if not parts:
                continue
            try:
                key = wg.check_key(parts[0])
            except wg.WireGuardError:
                continue
            ips = parts[1:]
            ip = ips[0][:-3] if len(ips) == 1 and ips[0].endswith("/32") else " ".join(ips)
            old.append(wg.Peer(listed[key].unit if key in listed else "", key, ip))
        for argv in wg.set_commands(old, lst.peers):
            self.check(argv)
        seen = wg.parse_latest_handshakes(self.check(["wg", "show", IFACE, "latest-handshakes"]))
        now = self.clock()
        for peer in lst.peers:
            t = seen.get(peer.public, 0)
            if t > 0 and now - t > PEER_RESET:
                self.check(["wg", "set", IFACE, "peer", peer.public, "remove"])
                self.check(["wg", "set", IFACE, "peer", peer.public, "allowed-ips", f"{peer.ip}/32"])
                self.say(f"URBAN-WG reset peer {wg.fingerprint(peer.public)}: no handshake for {PEER_RESET} s")

    def unit_peer(self, link, wg, up: bool):
        """The district is wg0's one peer; its route is put back while wg0 is up (an interface switched off and on
        again loses it)."""
        server = wg.Plan.of().server(link.index)
        peers = set(self.check(["wg", "show", IFACE, "peers"]).split())
        if peers != {link.server_public}:
            for key in sorted(peers - {link.server_public}):
                self.check(["wg", "set", IFACE, "peer", key, "remove"])
            self.check(["wg", "set", IFACE, "private-key", str(self.p.key), "peer", link.server_public,
                        "endpoint", link.endpoint, "allowed-ips", f"{server}/32", "persistent-keepalive", str(wg.KEEPALIVE)])
        if up:
            self.check(["ip", "route", "replace", f"{server}/32", "dev", IFACE])

    def prove(self, link, wg):
        """A fresh handshake with the district proves this link: from then on the unit reaches the centre through it.
        After a new kit (pin --replace) only a handshake newer than the kit counts."""
        now = self.clock()
        seen = self.handshakes().get(link.server_public, 0)
        if link.server_public not in wg.peers_up({link.server_public: seen}, now):
            return
        after = self.mem.get("prove_after")
        if isinstance(after, int) and after <= now and seen <= after:  # (a clock set back since then does not hold it)
            return
        self.mem.pop("prove_after", None)
        triple = f"{link.district} {link.index} {link.address}"
        if self.proven_text() != triple:
            write_file(self.p.proven, triple + "\n", 0o600, sync=True)
            self.say(f"URBAN-WG proven district={link.district} server={wg.Plan.of().server(link.index)}")

    def proven_text(self) -> str:
        try:
            return self.p.proven.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return ""

    def relay_mode(self, lst):
        """The district's relay mode from the applied list, for urban-relay (written after the applied line)."""
        mode = "allow" if lst.plain_relay else "only"
        try:
            current = self.p.relay_mode.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            current = None
        if current != mode:
            write_file(self.p.relay_mode, mode + "\n", 0o644)

    # ---- a unit's cameras
    def camera_list(self, wg) -> tuple[list, dict]:
        """(the forwards planned from wg-cameras.json, {name: "bad entry"} for what cannot be planned)."""
        try:
            entries = strict_json(read_hostile(self.p.etc / "wg-cameras.json", MAX_MESSAGE))
        except FileNotFoundError:
            return [], {}
        except (OSError, BadFile, ValueError):
            return [], {"wg-cameras.json": "bad entry"}
        if not isinstance(entries, list):
            return [], {"wg-cameras.json": "bad entry"}
        good, bad = [], []
        for i, entry in enumerate(entries):
            try:
                wg.plan_camera_forwards(good + [entry])
                good.append(entry)
            except (wg.WireGuardError, TypeError, AttributeError):
                name = entry.get("name") if isinstance(entry, dict) else None
                bad.append((i, name if isinstance(name, str) and wg.CAMERA_NAME.fullmatch(name) else None))
        planned = wg.plan_camera_forwards(good)
        taken = {c.name for c in planned}  # a bad entry named like a good camera is called by its place in the list
        return planned, {(name if name and name not in taken else f"#{i + 1}"): "bad entry" for i, name in bad}

    def cameras(self, link, wg) -> set:
        """The camera table: a camera is forwarded only if it is directly connected on a device that is neither wg0,
        the loopback nor the one that carries the tunnel itself. Returns the devices the forwarded cameras are on."""
        server = wg.Plan.of().server(link.index)
        planned, refused = self.camera_list(wg)
        uplink, _ = self.route(host_of(link.endpoint))
        accepted, devices = [], set()
        for cam in planned:
            dev, via = self.route(cam.host)
            if dev is None or uplink is None:
                refused[cam.name] = "no route"
            elif dev == IFACE:
                refused[cam.name] = "on the tunnel"
            elif dev == uplink:
                refused[cam.name] = "on the uplink"
            elif via or dev == "lo":
                refused[cam.name] = "no route"
            else:
                accepted.append(cam)
                devices.add(dev)
        said = self.mem.get("camera_refused") if isinstance(self.mem.get("camera_refused"), dict) else {}
        for name in sorted(refused):
            if said.get(name) != refused[name]:
                self.say(f"URBAN-WG camera refused {name}: {refused[name]}")
        self.mem["camera_refused"] = refused
        if not accepted:
            self.cameras_off()
            return set()
        text = camera_rules(server, accepted)
        try:
            current = self.p.cameras_nft.read_text(encoding="utf-8")
        except (OSError, ValueError):
            current = None
        if current != text or self.cmd(["nft", "list", "table", "ip", "urban_wg"])[0] != 0:
            self.p.cameras_nft.parent.mkdir(parents=True, exist_ok=True)
            write_file(self.p.cameras_nft, text, 0o600)
            code, _ = self.cmd(["nft", "-f", str(self.p.cameras_nft)])
            if code != 0:
                remove_any(self.p.cameras_nft)  # so that the next run tries again
                raise CommandFailed(["nft", "-f"], code)
        names = sorted(c.name for c in accepted)
        if self.mem.get("cameras") != names:
            self.say(f"URBAN-WG cameras {len(names)} forwarded")
        self.mem["cameras"] = names
        return devices

    def cameras_off(self):
        """No camera table and no file for it."""
        if self.p.cameras_nft.exists() or self.mem.get("cameras"):
            remove_any(self.p.cameras_nft)
            if self.cmd(["nft", "list", "table", "ip", "urban_wg"])[0] == 0:
                self.check(["nft", "delete", "table", "ip", "urban_wg"])
        if self.mem.get("cameras"):
            self.say("URBAN-WG cameras 0 forwarded")
        self.mem["cameras"] = []

    def forwarding(self, devices: set):
        """Forwarding on wg0 and on the forwarded cameras' devices only, never the global switch or conf/all and
        conf/default; a device set earlier that carries no forwarded camera any more is set back to 0."""
        before = set(self.mem.get("forwarding") or [])
        for dev, value in [(d, "1") for d in sorted(devices)] + [(d, "0") for d in sorted(before - devices)]:
            if not NETDEV.fullmatch(dev) or dev in ("all", "default", ".", ".."):
                continue
            try:
                with open(self.p.conf / dev / "forwarding", "w", encoding="ascii") as f:
                    f.write(value)
            except FileNotFoundError:
                if value == "1":
                    raise CommandFailed(["sysctl", f"net.ipv4.conf.{dev}.forwarding"], 1)
            except OSError:
                raise CommandFailed(["sysctl", f"net.ipv4.conf.{dev}.forwarding"], 1)
        self.mem["forwarding"] = sorted(devices)

    # ---- the status
    def status(self, kernel: bool) -> dict:
        """What the service reports and the screen shows. kernel=False (boot): wg0 cannot exist yet."""
        try:
            public = (self.p.wg / "key.pub").read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            public = ""
        st = {"role": self.role, "public": public if fingerprint(public) != "none" else "", "state": "none",
              "applied": 0, "epoch": 0, "district": None, "index": None, "address": None, "server": None,
              "proven": False, "handshake": 0, "peers": 0, "peers_up": 0, "mode": "off",
              "reason": self.mem.get("reason") if self.mem.get("reason") in REASONS else "", "t": int(self.clock())}
        obj, rec = self.applied()
        if obj is None:
            return st
        wg = _wireguard()
        server = wg.Plan.of().server(obj.index)
        epoch = rec.get("epoch")
        st.update(applied=obj.version, epoch=epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else 0,
                  index=obj.index, server=server)
        present, up = self.link_state() if kernel else (False, False)
        seen = self.handshakes() if present else {}
        fresh = wg.peers_up(seen, self.clock())
        if self.role == "unit":
            st.update(district=obj.district, address=obj.address, peers=1, handshake=seen.get(obj.server_public, 0),
                      peers_up=int(obj.server_public in fresh),
                      proven=self.proven_text() == f"{obj.district} {obj.index} {obj.address}")
            ok = up and obj.server_public in fresh
        else:
            st.update(district=obj.district, address=server, peers=len(obj.peers),
                      peers_up=len(fresh & {p.public for p in obj.peers}), mode="allow" if obj.plain_relay else "only")
            ok = up
        if not present and not self.clock_ok()[0]:
            st["state"] = "clock"
        else:
            st["state"] = "up" if ok else "down"
        return st

    def write_status(self, kernel: bool):
        try:
            st = self.status(kernel)
        except Exception:  # the kernel could not be asked: what is applied, without wg0's own state
            st = self.status(False)
        write_file(self.p.status, json.dumps(st) + "\n", 0o644)
        write_file(self.p.screen, screen_line(st) + "\n", 0o644)
        if kernel:  # the machine's screen redrawn when what it says changes (urban-prepare redraws it after boot)
            shown = [st[k] for k in ("state", "applied", "address", "proven", "peers", "peers_up", "mode", "reason")]
            if self.mem.get("screen") != shown:
                self.cmd([CONSOLE])
            self.mem["screen"] = shown

    # ---- the commands
    def run(self) -> int:
        if not self.identity():
            return 0
        with self.lock(self.p.lock):
            self.load_memory()
            try:
                if not self.p.verified.exists():
                    self.reverify(kernel=True)
                taken = self.intake()
                code = self.converge()
                return code or (0 if taken else 1)
            except CommandFailed as err:  # dropping a stored message that failed its check
                self.say(f"URBAN-WG command failed: {err}")
                return 3
            finally:
                self.write_status(kernel=True)
                self.save_memory()

    def boot(self) -> int:
        if not self.identity():
            return 0
        with self.lock(self.p.lock):
            self.load_memory()
            try:
                if not self.anything_pinned():
                    self.pin(replace=False, kernel=False)  # a unit set up before the tunnel existed
                self.raise_epoch()
                self.reverify(kernel=False)
                self.kit_check(kernel=False)
            finally:
                obj, _ = self.applied()
                if self.role == "district" and obj is not None:
                    self.relay_mode(obj)
                elif self.role == "district":
                    remove_any(self.p.relay_mode)
                self.write_status(kernel=False)
                self.save_memory()
        return 0

    def pin_command(self, replace: bool) -> int:
        if not self.identity():
            return 0
        with self.lock(self.p.lock):
            self.load_memory()
            try:
                if not self.pin(replace, kernel=True):
                    return 1
                if not replace:
                    return 0
                if not self.p.verified.exists():
                    self.reverify(kernel=True)
                self.kit_check(kernel=True)
                return self.converge()
            except CommandFailed as err:
                self.say(f"URBAN-WG command failed: {err}")
                return 3
            except OSError:
                return 1
            finally:
                self.write_status(kernel=True)
                self.save_memory()

    def print_status(self) -> int:
        try:
            print(self.p.status.read_text(encoding="utf-8"), end="", flush=True)
        except OSError:
            print("no tunnel status: not a tunnel machine, or urban-wg boot has not run", file=sys.stderr)
        return 0


def camera_rules(server: str, cams) -> str:
    """The unit's camera table (/run/urban/nft/wg-cams.nft): from the district's tunnel address only, each camera's
    tunnel port to the camera, the camera's answers back, and TCP segments sized for the tunnel."""
    pre = "".join(f'\n        iifname "{IFACE}" ip saddr {server} tcp dport {c.tunnel_port} dnat to {c.host}:{c.port}'
                  for c in cams)
    targets = sorted({(c.host, c.port) for c in cams}, key=lambda hp: (ipaddress.IPv4Address(hp[0]), hp[1]))
    post = "".join(f"\n        ct status dnat ip daddr {host} tcp dport {port} masquerade" for host, port in targets)
    # each rule on a line of its own and each closing brace too: nft takes no rule followed by "}" on one line
    return ("table ip urban_wg\n"
            "delete table ip urban_wg\n"
            "table ip urban_wg {\n"
            f"    chain prerouting {{\n        type nat hook prerouting priority dstnat; policy accept;{pre}\n    }}\n"
            f"    chain postrouting {{\n        type nat hook postrouting priority srcnat; policy accept;{post}\n    }}\n"
            "    chain forward {\n        type filter hook forward priority -150; policy accept;\n"
            f'        oifname "{IFACE}" tcp flags syn tcp option maxseg size set rt mtu\n    }}\n'
            "}\n")


def screen_line(st: dict) -> str:
    """The status in one line for the machine's screen: it starts with the state and ends with the key's fingerprint."""
    why = f" (last message refused: {st['reason']})" if st.get("reason") else ""
    if st["state"] == "none":
        text = f"none: no tunnel from the centre yet{why}"
    elif st["state"] == "clock":
        text = f"clock: waiting for the time (system time before the build date){why}"
    elif st["role"] == "unit":
        age = f"{max(0, st['t'] - st['handshake'])} s ago" if st["handshake"] else "never"
        text = (f"{st['state']} {st['address']} -> {st['district']} ({st['server']}), handshake {age}, "
                f"{'proven' if st['proven'] else 'not proven yet'}{why}")
    else:
        text = (f"{st['state']} {st['address']}, mode {st['mode']}, {st['peers_up']} of {st['peers']} unit(s) "
                f"with a fresh handshake{why}")
    return f"{text} key {fingerprint(st['public'])}"


def main(argv=None, helper: Helper | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    helper = helper or Helper()
    commands = {("run",): helper.run, ("boot",): helper.boot, ("status",): helper.print_status,
                ("pin",): lambda: helper.pin_command(False), ("pin", "--replace"): lambda: helper.pin_command(True)}
    command = commands.get(tuple(argv))
    if command is None:
        print(f"usage: {USAGE}", file=sys.stderr)
        return 2
    try:
        return command()
    except Exception as err:  # the helper's own fault: said without any text that could come from a file
        tb = err.__traceback__
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        where = f" at {Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}" if tb is not None else ""
        helper.say(f"URBAN-WG internal error: {type(err).__name__}{where}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
