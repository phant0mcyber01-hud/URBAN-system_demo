"""Over-the-air updates, device side: shared by the junction unit and the district server.

The centre offers a release in a gateway-signed command, {"update": {"version", "sha256", "size", "path"}}
(urban/devices/hub.py). The device ignores it unless it is newer than the system it runs, downloads the package
from the centre's device listener over the same TLS name it already uses for its websocket, checks the size and
the hash from the signed offer while it streams, and puts the package in the update inbox with a request.json.
Nothing else is ever written. Installing is not done here: a root helper on the OS picks the request up,
re-verifies the package with RAUC (signed by URBAN's build key, newer than the running system) and installs it.
It writes its own state to a status file, which is reported next to ours.

Paths (environment overrides are for tests):
    /etc/urban-os-release          URBAN_OS_RELEASE   VERSION=<n> of the running system
    /var/lib/urban/update-inbox    URBAN_UPDATE_INBOX urban-<n>.raucb[.part] and request.json
    /run/urban/update.json         URBAN_UPDATE_STATUS state of the root helper
"""
import asyncio
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
from pathlib import Path

MAX_SIZE = 2 << 30      # no package is that big; a lying centre must not fill the disk
CHUNK = 1 << 16
TIMEOUT = 60            # seconds of silence from the centre that end a download
SPACE_MARGIN = 16 << 20  # room left over after the package
REASON_MAX = 120
HELPER_STATES = ("installing", "installed", "refused")
_READ = object()        # Updater(version=...): read the running version from the OS release file
_PATH = re.compile(r"/update/[0-9a-f]{16,128}")
_HEX = re.compile(r"[0-9a-f]{64}")


def os_version(path: Path | str | None = None) -> int | None:
    """The OS release this device runs: the integer in VERSION=<n> of /etc/urban-os-release, None if unknown."""
    path = Path(path or os.getenv("URBAN_OS_RELEASE", "/etc/urban-os-release"))
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, eq, value = line.strip().partition("=")
            if eq and key == "VERSION":
                text = value.strip().strip("'\"")
                return int(text) if text.isascii() and text.isdigit() else None
    except (OSError, ValueError):
        pass
    return None


def inbox_dir() -> Path:
    return Path(os.getenv("URBAN_UPDATE_INBOX", "/var/lib/urban/update-inbox"))


def status_file() -> Path:
    return Path(os.getenv("URBAN_UPDATE_STATUS", "/run/urban/update.json"))


class _TlsConnection(http.client.HTTPSConnection):
    """HTTPS to an address while checking the certificate for another name: what the websocket does with
    server_hostname, the platform's certificate being issued for its name and not for its IP address."""

    def __init__(self, host, port, context, server_name, timeout=TIMEOUT):
        super().__init__(host, port, timeout=timeout, context=context)
        self.server_name = server_name

    def connect(self):
        http.client.HTTPConnection.connect(self)  # the TCP connection, as for plain HTTP
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.server_name)


def split_hostport(platform: str, default: int = 443) -> tuple[str, int]:
    host, colon, port = platform.rpartition(":")
    if colon and port.isdigit():
        return host.strip("[]"), int(port)
    return platform.strip("[]"), default


def make_updater(platform, server_name: str, context: ssl.SSLContext) -> "Updater":
    """An Updater that downloads from `platform` (host:port of the device listener, or of the district relay in
    front of it) with the TLS context and server name the device's websocket uses. `platform` may also be a function
    that gives it, asked at each download: a unit's path to the centre moves into the tunnel (edge/tunnel.py), and
    None from it means no path is known yet, so that download is not tried."""
    where = platform if callable(platform) else (lambda: platform)

    def connect():
        target = where()
        if target is None:
            return None
        host, port = split_hostport(target)
        return _TlsConnection(host, port, context, server_name)
    return Updater(connect)


def valid_offer(spec) -> dict | None:
    """The offer's fields if they are what the centre sends, else None."""
    if not isinstance(spec, dict):
        return None
    v, size, sha, path = spec.get("version"), spec.get("size"), spec.get("sha256"), spec.get("path")
    if (isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10 ** 9
            and isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_SIZE
            and isinstance(sha, str) and _HEX.fullmatch(sha)
            and isinstance(path, str) and _PATH.fullmatch(path)):
        return {"version": v, "size": size, "sha256": sha, "path": path}
    return None


class UpdateError(Exception):
    """A download that must not be handed over; the message is the reason that goes into the report."""


def _reason(text: str) -> str:
    return "".join(ch for ch in text if " " <= ch <= "~")[:REASON_MAX]


class Updater:
    def __init__(self, connect, version=_READ, inbox: Path | None = None, status: Path | None = None):
        """connect(): a fresh http.client connection to the centre's device listener, or None while there is no path
        to it (that download fails and the offer is taken again later);
        version: the running system's version (default: read from the OS release file; None: unknown, so
        every offer is ignored); inbox, status: where the package goes and where the root helper reports."""
        self.connect = connect
        self.version = os_version() if version is _READ else version
        self.inbox = inbox
        self.status = status
        self.state: dict | None = None  # our own: {"state", "version", "reason"}
        self.busy: set[int] = set()     # versions being downloaded or already handed to the helper
        self.queue: list[dict] = []
        self._task = None

    # ---- the offer
    def offer(self, spec) -> bool:
        """A verified offer from the centre. True if it was taken up: newer than the running system, well formed,
        not already handled, and no installation under way."""
        spec = valid_offer(spec)
        if spec is None or self.version is None or spec["version"] <= self.version:
            return False
        v = spec["version"]
        theirs = self.helper_status()
        if v in self.busy or (theirs is not None and (theirs["state"] == "installing" or (
                theirs["version"] >= v and theirs["state"] in HELPER_STATES))):
            return False
        self.busy.add(v)
        self.queue.append(spec)
        self._kick()
        return True

    def _kick(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop here: whoever called offer() runs drain()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._work())

    async def _work(self):
        while self.queue:
            await asyncio.to_thread(self.fetch, self.queue.pop(0))  # the session keeps serving commands meanwhile

    def drain(self):
        while self.queue:
            self.fetch(self.queue.pop(0))

    # ---- the download
    def fetch(self, spec: dict):
        v = spec["version"]
        self.state = {"state": "downloading", "version": v, "reason": ""}
        inbox = self.inbox or inbox_dir()
        part = inbox / f"urban-{v}.raucb.part"
        try:
            self._download(spec, inbox, part)
            self.state = {"state": "ready", "version": v, "reason": ""}
        except UpdateError as err:
            self._failed(v, part, str(err))
        except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as err:
            self._failed(v, part, f"download failed: {err.__class__.__name__} {err}")
        except Exception as err:  # whatever it was, the device stays as it is and says so
            self._failed(v, part, f"unexpected: {err.__class__.__name__}")

    def _failed(self, v: int, part: Path, why: str):
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        self.busy.discard(v)  # the centre offers it again after a while
        self.state = {"state": "failed", "version": v, "reason": _reason(why)}

    def _download(self, spec: dict, inbox: Path, part: Path):
        v, size = spec["version"], spec["size"]
        inbox.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(inbox).free < size + SPACE_MARGIN:
            raise UpdateError("not enough free space for the package")
        digest, got = hashlib.sha256(), 0
        conn = self.connect()
        if conn is None:  # the centre offers it again after a while
            raise UpdateError("no path to the centre yet (waiting for the tunnel status)")
        try:
            conn.request("GET", spec["path"], headers={"Accept": "application/octet-stream"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise UpdateError(f"the centre answered {resp.status}")
            length = resp.getheader("Content-Length")
            if length is not None and length.strip() != str(size):
                raise UpdateError("the package's length differs from the offer")
            with open(part, "wb") as out:
                while True:
                    chunk = resp.read(min(CHUNK, size - got + 1))  # one byte more than offered is enough to know
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > size:
                        raise UpdateError("the package is larger than offered")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        finally:
            conn.close()
        if got != size:
            raise UpdateError(f"the package is shorter than offered ({got} of {size} bytes)")
        if digest.hexdigest() != spec["sha256"]:
            raise UpdateError("the package's hash differs from the offer")
        name = f"urban-{v}.raucb"
        os.replace(part, inbox / name)
        request = {"version": v, "file": name, "sha256": spec["sha256"], "size": size}
        tmp = inbox / "request.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(request, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, inbox / "request.json")  # the helper never sees half a request

    # ---- the report
    def helper_status(self) -> dict | None:
        """The root helper's own state, if it has written one."""
        try:
            data = json.loads((self.status or status_file()).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("state") not in HELPER_STATES:
            return None
        v, reason = data.get("version"), data.get("reason")
        if not isinstance(v, int) or isinstance(v, bool):
            return None
        return {"state": data["state"], "version": v, "reason": reason if isinstance(reason, str) else ""}

    def update_state(self) -> dict | None:
        """What to report: the helper's state when it concerns our version or a newer one, else ours."""
        theirs = self.helper_status()
        if theirs is not None and (self.state is None or theirs["version"] >= self.state["version"]):
            return theirs
        return self.state

    def health(self) -> dict:
        """The fields a unit or district puts into its health report: os and update."""
        out = {}
        if self.version is not None:
            out["os"] = self.version
        state = self.update_state()
        if state is not None:
            out["update"] = state
        return out
