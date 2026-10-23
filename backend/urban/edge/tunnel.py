"""The WireGuard tunnel to the district, device side: shared by the junction unit and the district server.

The centre sends a district its peer list and a unit its link (urban/net/wireguard.py), each signed by the gateway key,
inside an ordinary signed command {"wg": <message>} (urban/devices/hub.py). The service checks the inner message as the
root helper will (gateway signature, addressed to this device, not older than the one applied) and puts it, exactly as
it came, in the tunnel inbox. Nothing else is ever written. Applying it is not done here: the root helper urban-wg
(urban/net/wghelper.py) takes the message from the inbox, checks it again against the keys it pinned itself and sets up
wg0. It writes its state to a status file, which the service reports in its health. A unit also takes from it which
address of the centre to dial: the district's tunnel address once a handshake has proven the link, the kit's platform
otherwise.

Paths (environment overrides are for tests):
    /var/lib/urban/wg-inbox    URBAN_WG_INBOX   message.json; that the folder exists says the machine has the helper
    /run/urban/wg.json         URBAN_WG_STATUS  state of the root helper
"""
import ipaddress
import json
import os
from pathlib import Path

from ..net import wireguard

MAX_MESSAGE = 65536  # bytes of message.json; a full list of 253 peers is some 40 kB
# what goes into the health report from the helper's status (the centre checks each field, urban/devices/health.py)
HEALTH_KEYS = {"unit": ("public", "state", "applied", "epoch", "address", "reason", "proven", "handshake"),
               "district": ("public", "state", "applied", "epoch", "address", "reason", "peers", "peers_up", "mode")}


def inbox_dir() -> Path:
    return Path(os.getenv("URBAN_WG_INBOX", "/var/lib/urban/wg-inbox"))


def status_file() -> Path:
    return Path(os.getenv("URBAN_WG_STATUS", "/run/urban/wg.json"))


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _words(err: wireguard.MessageRefused) -> str:
    """Why a message is refused, in fixed words: never text taken from the message itself."""
    text = str(err)
    if text in ("bad signature", "no gateway key yet"):
        return text
    if text.startswith("addressed to "):
        return "not addressed here"
    if text.startswith("version ") and " is older than " in text:  # not "malformed: ..." quoting a peer's name
        return "older version"
    return "malformed"


class Tunnel:
    def __init__(self, role: str, device: str, inbox: Path | None = None, status: Path | None = None):
        """role: "unit" (takes a link) or "district" (takes a peer list); device: this machine's id;
        inbox, status: where messages go and where the root helper reports (default: the paths above)."""
        if role not in HEALTH_KEYS:
            raise ValueError(f"a tunnel role is unit or district, not {role!r}")
        self.role, self.device = role, device
        self.inbox, self.status = inbox, status

    def helper_status(self) -> dict | None:
        """The root helper's status, if it has written one that reads."""
        try:
            data = json.loads((self.status or status_file()).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ---- a message from the centre
    def take(self, msg, gateway_public, epoch: int = 0) -> str | None:
        """A message from inside a verified {"wg": ...} command. It is put in the inbox only if it verifies under the
        gateway key this service obeys (`epoch`: that key's certificate epoch) and is not older than the applied one.
        Returns None if it was queued, else the reason it was refused (for the report)."""
        text = json.dumps(msg)
        if len(text.encode()) > MAX_MESSAGE:
            return "wg: message too large"
        status = self.helper_status() or {}
        # a message under a newer gateway key than the applied one's is never held back by the old version number:
        # after a rotation the genuine message wins (the helper makes the final decision)
        last = _count(status.get("applied")) if epoch <= _count(status.get("epoch")) else 0
        parse = wireguard.parse_link if self.role == "unit" else wireguard.parse_peer_list
        try:
            parse(msg, gateway_public, wireguard.Plan.of(), self.device, last_version=last)
        except wireguard.MessageRefused as err:
            return f"wg: {_words(err)}"
        except (ValueError, TypeError, KeyError, AttributeError):
            return "wg: malformed"
        inbox = self.inbox or inbox_dir()
        if not os.path.isdir(inbox):  # one this service cannot even look at counts as none
            return "wg: no inbox"
        tmp = inbox / "message.json.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, inbox / "message.json")  # the helper never sees half a message
        except OSError as err:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return f"wg: write error ({err.__class__.__name__})"
        return None

    # ---- the report
    def health(self) -> dict:
        """The helper's state for the health report: {"wg": {...}}, or {} while there is no status (a machine
        without the tunnel helper), which is how the centre knows to send it nothing."""
        status = self.helper_status()
        if status is None:
            return {}
        return {"wg": {k: status[k] for k in HEALTH_KEYS[self.role] if k in status}}

    # ---- where a unit dials the centre
    def platform(self, default: str) -> str | None:
        """host:port of the centre for the next connection of a unit: the district's tunnel address once the helper
        says a fresh handshake has proven the applied link, else `default` (the kit's platform, the plain path).
        None means wait: the machine has the helper but its status cannot be read, so neither path is known to be right."""
        # os.path.isdir, not Path.is_dir: a folder this service may not look at must not stop its ticker
        if self.role != "unit" or not os.path.isdir(self.inbox or inbox_dir()):
            return default  # no helper on this machine (a server install, an older OS): the plain path as ever
        status = self.helper_status()
        if status is None:
            return None
        server = status.get("server")
        if status.get("proven") is True and isinstance(server, str):
            try:
                if ipaddress.IPv4Address(server) in wireguard.Plan.of().base:
                    return f"{server}:{wireguard.RELAY_PORT}"
            except ValueError:
                pass
        return default
