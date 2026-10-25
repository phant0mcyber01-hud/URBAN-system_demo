"""The control centre updates itself (step 13), through the same signed path as every unit.

    urban-center release self-update N      (python -m urban.release self-update N)

The admin names the version; there is no automatic self-update. The command, run as the user urban like every
`release` command, takes release N for this machine's own system (its RAUC compatible, /etc/rauc/system.conf) from the
centre's release store and refuses it, with the reason and with nothing staged, when it is halted or withdrawn, not
newer than the running version (/etc/urban-os-release), or when the store has no release N for this compatible. An
accepted one is not installed by the platform: the command copies the package into the update inbox and writes
request.json there, exactly as a unit's client does with a package it downloaded (edge/update.py), and the root update
helper (`urban-unit apply-update`, started by urban-update.path) checks URBAN's signature, that the version is the one
asked for and newer than the running one, installs it into the other slot with RAUC and restarts the machine. The
existing self-check and A/B fallback decide the rest: a version that fails its self-check sends the centre back to the
slot it came from by itself.

Before it hands the package over, the command records the self-update on the data partition (self-update.json next to
releases.json: from which version to which, for which compatible, in which boot, by whom). The platform (Watch, every few
seconds while it runs, and once at its start) audits it as centre-update-started, then, in a later boot whose self-check
has spoken (/run/urban/health: good or degraded, never while a bad slot is on its way back), compares the running version
with the one it started: centre-updated (`now N`) or centre-rolled-back (`started N, back on M`), once; the record then
says how it ended and stays as the centre's last self-update, shown in Devices (GET /api/centre-os). A version the
centre rolled back from is halted like a release a unit rolled back from (rollout-halted, device `centre`), so no unit
is offered it until the admin resumes it. A refusal by the root helper (its status file says refused, written after
this hand-over: the record keeps the stamp the file had then, so an earlier attempt's refusal is never taken for this
one's) is audited as centre-update-refused with its reason, in whichever boot it is seen; so is a self-update the helper
never started to install (the machine restarted before it said installing): an older version after the restart is a
rollback only when the helper said it was installing or installed this version (`reached`, noted by the command and by
the platform), as a unit's rollback halts a release only when the unit reported installing it.

Paths (environment overrides are for tests): the update inbox and the helper's status as in edge/update.py, and
    /run/urban/health          URBAN_HEALTH          the verdict of this boot's self-check (urban-health)
    /run/urban/boot-id         URBAN_BOOT_ID         this boot's id (urban-prepare copies the kernel's; the platform's
                                                     sandbox does not see /proc/sys), else the kernel's own
    /run/urban/update-reboot   URBAN_UPDATE_REBOOT   the helper installed a release and waits for the restart
"""
import datetime
import json
import os
import secrets
import shutil
import time
from pathlib import Path

from .edge.update import SPACE_MARGIN, inbox_dir, os_compatible, os_version, status_file
from .release import ReleaseError, ReleaseStore, halt_text

RECORD, AUDITED = "self-update.json", "self-update.audited"  # the last self-update; the id whose start is audited
TARGET = "centre"  # the audit target and the halting device of the centre's own self-update
STATES = ("started", "updated", "rolled-back", "refused")
KERNEL_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def _path(env: str, default: str) -> Path:
    return Path(os.getenv(env, default))


def boot_id() -> str | None:
    """This boot's id: the copy urban-prepare puts in /run/urban (the platform's sandbox hides /proc/sys), else the
    kernel's own; None if neither is readable."""
    for path in (_path("URBAN_BOOT_ID", "/run/urban/boot-id"), KERNEL_BOOT_ID):
        try:
            text = path.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            continue
        if text and len(text) <= 64:
            return text
    return None


def health_verdict() -> str | None:
    """This boot's self-check as urban-health wrote it: "good", "bad" (going back to the other slot) or "degraded";
    None while it has not spoken."""
    try:
        word = _path("URBAN_HEALTH", "/run/urban/health").read_text(encoding="utf-8").split(maxsplit=1)[0].lower()
    except (OSError, ValueError, IndexError):
        return None
    return word if word in ("good", "bad", "degraded") else None


def helper_status() -> dict | None:
    """The root update helper's state ({"state", "version", "reason"}), if it wrote one in this boot."""
    try:
        data = json.loads(status_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("version"), int) or isinstance(data.get("version"), bool):
        return None
    return {"state": str(data.get("state", "")), "version": data["version"], "reason": str(data.get("reason") or "")}


def _status_seen() -> tuple | None:
    """What the helper's status file is now (its stamp and content), to tell its next answer from an old one."""
    path = status_file()
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_ino, path.read_bytes()
    except OSError:
        return None


def _stamp(seen: tuple | None) -> list | None:
    """The status file's identity (mtime, inode): the helper replaces it with mv -f, so each answer has a new one."""
    return None if seen is None else [seen[0], seen[1]]


def answer(rec: dict) -> dict | None:
    """The helper's answer about this record's version written after its hand-over, or None (none yet, an older one,
    or one about another version)."""
    seen = _status_seen()
    if seen is None or _stamp(seen) == rec.get("helper_seen"):
        return None
    theirs = helper_status()
    return theirs if theirs is not None and theirs["version"] == rec["to"] else None


def note(store: ReleaseStore, rec: dict, **fields):
    """Add what was learnt (reached, refused) to the record, if it is still the same self-update and under way."""
    now = read_record(store)
    if now is not None and now["id"] == rec["id"] and now["state"] == "started":
        _write_record(store, {**now, **fields})


def read_record(store: ReleaseStore) -> dict | None:
    """The last self-update as recorded (None if there never was one, or the file is not one we write)."""
    try:
        data = json.loads((store.dir / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("state") not in STATES or not isinstance(data.get("id"), str)
            or not all(isinstance(data.get(k), int) and not isinstance(data.get(k), bool) for k in ("from", "to"))):
        return None
    return data


def _write_record(store: ReleaseStore, rec: dict):
    store._write_json(store.dir / RECORD, rec)  # whole or not at all: the platform reads it at any moment


def _now(now: datetime.datetime | None = None) -> str:
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def start(store: ReleaseStore, version: int, by: str = "cli", now: datetime.datetime | None = None) -> tuple[dict, tuple | None]:
    """Hand release `version` of this machine's own system to the root update helper. Refuses (ReleaseError) before
    anything is written. Returns the record written and the helper's status as it was before (for wait_answer)."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ReleaseError("the version is a positive whole number")
    running, compatible = os_version(), os_compatible()
    if compatible is None:
        raise ReleaseError("this machine's system is not known (no compatible in /etc/rauc/system.conf): self-update "
                           "runs on the centre's URBAN OS")
    if running is None:
        raise ReleaseError("the running system version is not known (/etc/urban-os-release)")
    rel = store.get(version, compatible)
    if rel is None:
        others = sorted({r.get("compatible") or "urban-edge-x86_64" for r in store.of_version(version)})
        raise ReleaseError(f"no release {version} for this machine's compatible ({compatible})"
                           + (f": only for {', '.join(others)}" if others else ""))
    if rel.get("withdrawn"):
        raise ReleaseError(f"release {version} for {compatible} is withdrawn")
    if rel.get("halted"):
        raise ReleaseError(f"release {version} is halted (by {halt_text(rel['halted'])}): nothing of it is installed "
                           f"until `release resume {version}`")
    if version <= running:
        raise ReleaseError(f"release {version} is not newer than the running version {running}")
    source = store.path(rel)
    size, digest = rel.get("size"), rel.get("sha256")
    if not source.is_file() or source.stat().st_size != size or not isinstance(digest, str):
        raise ReleaseError(f"the package of release {version} for {compatible} is missing from the releases folder or "
                           f"not the one added")
    last, here = read_record(store), boot_id()
    if last is not None and last["state"] == "started":
        theirs = answer(last) if here is not None and last.get("boot") == here else None
        refused = last.get("refused") is not None or (theirs is not None and theirs["state"] == "refused")
        if not refused:
            raise ReleaseError(f"the self-update to release {last['to']} started at {last.get('at', '-')} has not ended "
                               f"yet (the platform records how it ended after the restart)")
    if _path("URBAN_UPDATE_REBOOT", "/run/urban/update-reboot").exists():
        raise ReleaseError("an update is installed already and this machine has not restarted into it yet")
    inbox = inbox_dir()
    if not inbox.is_dir():
        raise ReleaseError(f"no update inbox ({inbox}): the root update helper is not set up on this machine")
    if (inbox / "request.json").exists():
        raise ReleaseError("an update is already waiting for the root update helper")
    if shutil.disk_usage(inbox).free < size + SPACE_MARGIN:
        raise ReleaseError(f"not enough free space in {inbox} for the package ({size} bytes)")

    # the package goes into the inbox under the name the helper expects, whole before it has that name
    name = f"urban-{version}.raucb"
    part = inbox / (name + ".part")
    try:
        with open(source, "rb") as src, open(part, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        os.replace(part, inbox / name)
    except OSError as err:
        part.unlink(missing_ok=True)
        raise ReleaseError(f"the package could not be put in the update inbox: {err}")
    before = _status_seen()
    rec = {"state": "started", "from": running, "to": version, "compatible": compatible, "sha256": digest,
           "size": size, "boot": here, "at": _now(now), "by": str(by)[:60] or "-", "id": secrets.token_hex(8),
           "helper_seen": _stamp(before)}
    _write_record(store, rec)  # recorded before it is handed over: the platform finds it after the restart
    request = {"version": version, "file": name, "sha256": digest, "size": size}
    tmp = inbox / "request.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(request, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, inbox / "request.json")  # the helper never sees half a request
    return rec, before


def wait_answer(version: int, before: tuple | None, timeout: float, step: float = 2.0,
                clock=time.monotonic, pause=time.sleep) -> dict | None:
    """The root helper's first answer about `version` after the hand-over (installing, installed or refused), or None
    if it said nothing new within `timeout` seconds."""
    end = clock() + max(0.0, timeout)
    while True:
        if _status_seen() != before:
            theirs = helper_status()
            if theirs is not None and theirs["version"] == version and theirs["state"] in ("installing", "installed",
                                                                                          "refused"):
                return theirs
        if clock() >= end:
            return None
        pause(step)


class Watch:
    """The platform's side: audits a self-update once when it starts and once when it ended (see the module's text).
    step() on the event loop (audit: the platform's audit log), at the start and every few seconds."""

    def __init__(self, store: ReleaseStore, audit):
        self.store, self.audit = store, audit

    def _audited(self) -> str | None:
        try:
            return (self.store.dir / AUDITED).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def step(self) -> str | None:
        """One look. Returns the event it audited last, or None."""
        rec = read_record(self.store)
        if rec is None or rec["state"] != "started":
            return None
        said = None
        if self._audited() != rec["id"]:
            self.audit("centre-update-started", TARGET,
                       f"release {rec['to']} for {rec.get('compatible') or '-'}, from {rec['from']}", actor=str(rec.get("by") or "-"))
            self.store._write_json(self.store.dir / AUDITED, None, text=rec["id"])
            said = "centre-update-started"
        if rec.get("refused") is not None:  # the command saw the helper refuse it (in any boot since)
            return self._refused(rec, rec["refused"])
        here = boot_id()
        if rec.get("boot") is not None and rec.get("boot") == here:
            # still the boot that handed it over: the helper's answer to this hand-over, not an earlier one
            theirs = answer(rec)
            if theirs is not None and theirs["state"] == "refused":
                return self._refused(rec, theirs["reason"] or "-")
            if theirs is not None and theirs["state"] in ("installing", "installed") and not rec.get("reached"):
                note(self.store, rec, reached=True)
            return said
        running = os_version()
        if running is not None and running >= rec["to"] and not rec.get("reached"):
            note(self.store, rec, reached=True)  # this boot runs it: it was installed
            rec = read_record(self.store) or rec
        if health_verdict() not in ("good", "degraded"):
            return said  # the self-check has not spoken yet, or this slot is bad and the machine is going back
        if running is None:
            return said
        if running < rec["to"] and not rec.get("reached"):
            # the helper never said it was installing it: the machine restarted before (a power cut, a reboot)
            return self._refused(rec, "not installed: the centre restarted before the root update helper installed it")
        if running >= rec["to"]:
            self._settle(rec, "updated", running)
            self.audit("centre-updated", TARGET, f"now {running}")
            return "centre-updated"
        self._settle(rec, "rolled-back", running)
        self.audit("centre-rolled-back", TARGET, f"started {rec['to']}, back on {running}")
        # a release the centre rolled back from is halted like one a unit rolled back from (DeviceHub._halt)
        reason = f"rolled back: installed {rec['to']}, runs {running}"
        try:
            halted = self.store.halt(rec["to"], TARGET, rec.get("compatible") or "-", reason)
        except OSError:
            halted = None
        if halted is not None:
            self.audit("rollout-halted", f"release-{rec['to']}", f"{TARGET} ({rec.get('compatible') or '-'}): {reason}")
        return "centre-rolled-back"

    def _refused(self, rec: dict, reason: str) -> str:
        self._settle(rec, "refused", os_version(), reason)
        self.audit("centre-update-refused", TARGET, f"release {rec['to']}: {reason}")
        return "centre-update-refused"

    def _settle(self, rec: dict, state: str, running: int | None, reason: str = ""):
        _write_record(self.store, {**rec, "state": state, "running": running, "reason": reason, "ended": _now()})


def view(store: ReleaseStore) -> dict:
    """The centre's own system for Devices: its version and compatible, and its last self-update (None if none)."""
    rec = read_record(store)
    last = None
    if rec is not None:
        last = {k: rec.get(k) for k in ("state", "from", "to", "compatible", "at", "by", "ended", "running", "reason")}
    return {"os": os_version(), "compatible": os_compatible(), "self_update": last}
