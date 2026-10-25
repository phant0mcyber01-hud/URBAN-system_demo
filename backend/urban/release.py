"""Released OS update packages: what the control centre keeps and offers to its devices.

    python -m urban.release add urban-edge-3.raucb --version 3 [--devices edge-J-21,district-1 | --wave 10]
    python -m urban.release list
    python -m urban.release promote 3 --wave 50 | --all [--arch x86_64|aarch64]
    python -m urban.release resume 3     a halted rollout goes on (the admin's decision)
    python -m urban.release withdraw 3 [--arch x86_64|aarch64]
    python -m urban.release floor 5          the minimum system version the centre commands
    python -m urban.release floor            show it
    python -m urban.release floor --clear    no minimum: every system version is commanded again

A release is a RAUC bundle (urban-edge-<N>.raucb) signed with URBAN's build key. `add` asks RAUC on this machine about
it (its signature against the build keyring, its version, and the system it is for: its compatible, urban-edge-x86_64
or urban-edge-aarch64; anything else is refused) and copies it into the releases folder next to releases.json. Each
compatible has its own numbering: an x86 release 2 and an arm64 release 2 live side by side. The device hub offers a
unit or district server only releases of the compatible it reports in its health (one that reports none, a system
before step 12, is x86), in a signed message, when its reported system version is older.

A release goes out in waves. Its first wave is every unit and district (the default), the listed devices (--devices),
or a share of the fleet (--wave P: a device is in it when the rank of its id, 0-99 from a hash of the id, is below P;
stable for a device and the same for every release, so the same devices go first each time); `promote` widens it to a
larger share or to everyone (--all). If a device of the wave reports that the release failed (its update helper
refused the package, or the package it downloaded is not the one offered) or that it rolled back (it reported the
release installing, installed or running, and a later session runs an older system), the hub halts the release: no
device is offered it any more, it is audited as `rollout-halted` with the device and the reason, and Devices shows
it; the devices that already run it are left as they are. A halt stops every package of that version number (x86 and
arm64 are built from the same source): a package of a halted number added later starts halted. `resume` lets it go
on, `withdraw` ends it. A withdrawn release is never offered or served again. The platform may be running meanwhile:
it notices the change itself.

The floor (floor.json next to releases.json) is the oldest system the centre still commands: a unit, district server
or lighting cabinet that does not report at least that version in its current session gets no commands, only update
offers, until it reports a version at or above it (the rule: devices/hub.py, DeviceHub.floor_verdict). No floor is
the default: nothing changes until an admin sets one, here or in the interface (POST /api/os-floor). Every change is
audited as `os-floor-set` with who made it; a change made here is audited by the platform the next time it looks.
"""
import argparse
import datetime
import getpass
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
from pathlib import Path

from . import config as c

KEYRING = Path("/etc/rauc/keyring.pem")
DEVICE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
FLOOR, FLOOR_AUDITED = "floor.json", "floor.audited"  # the version floor; the id of the floor record last audited
COMPATIBLES = ("urban-edge-x86_64", "urban-edge-aarch64")  # the systems URBAN builds, as RAUC names them
DEFAULT_COMPATIBLE = COMPATIBLES[0]  # a release entry or a device that names none (before step 12) is x86
REASON_MAX = 160  # the longest reason a halt keeps


def compatible_of(release: dict) -> str:
    """The system a release entry is for (an entry written before step 12 names none: x86)."""
    return release.get("compatible") or DEFAULT_COMPATIBLE


def compatible_arg(text: str | None) -> str | None:
    """--arch x86_64 | aarch64 (or the whole compatible) as the compatible; None stays None."""
    if text is None:
        return None
    for compatible in COMPATIBLES:
        if text in (compatible, compatible.removeprefix("urban-edge-")):
            return compatible
    raise ReleaseError(f"--arch is x86_64 or aarch64, not {text!r}")


def wave_rank(device: str) -> int:
    """Where a device stands in every wave: 0-99 from a hash of its id (stable, and the same for every release)."""
    return int.from_bytes(hashlib.sha256(str(device).encode()).digest()[:4], "big") * 100 >> 32


def in_wave(release: dict, device: str) -> bool:
    """Whether a release's current wave includes this device: every device when it names neither devices nor a share;
    else the listed devices and those whose rank is below the share."""
    devices, share = release.get("devices"), release.get("wave")
    if devices is None and share is None:
        return True
    return (devices is not None and device in devices) or (share is not None and wave_rank(device) < share)


def wave_text(release: dict) -> str:
    """The current wave in words, for the command line."""
    devices, share = release.get("devices"), release.get("wave")
    if devices is None and share is None:
        return "every unit and district"
    parts = ([", ".join(devices)] if devices else []) + ([f"{share}% of the fleet by device id"] if share is not None else [])
    return " and ".join(parts)


def _version(v) -> bool:
    """A system version: a positive whole number (True is not one)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


class ReleaseError(ValueError):
    """A refusal, worded for the person who ran the command."""


def sha256_of(path: Path) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def check_bundle(path: Path, version: int, keyring: Path = KEYRING, which=shutil.which,
                 run=subprocess.run) -> tuple[str, str]:
    """Ask RAUC what it thinks of the bundle: its signature against the build keyring, its own version and the system
    it is for. Returns (its compatible, what was checked); raises ReleaseError when RAUC disagrees, when the bundle is
    for a system URBAN does not build, or when there is no RAUC here to read the compatible from the package."""
    rauc = which("rauc")
    if rauc is None:
        raise ReleaseError("rauc is not installed here: the package's signature and the system it is for cannot be "
                           "read (add releases on the centre's URBAN OS)")
    cmd = [rauc, "info", "--output-format=shell"] + ([f"--keyring={keyring}"] if keyring.exists() else []) + [str(path)]
    try:
        done = run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as err:
        raise ReleaseError(f"rauc could not be run on the bundle: {err}")
    if done.returncode != 0:
        raise ReleaseError(f"rauc refuses the bundle (signature or format): {(done.stderr or done.stdout).strip()[:200]}")
    info = {}
    for line in done.stdout.splitlines():
        key, eq, value = line.partition("=")
        if eq:
            try:
                info[key.strip()] = " ".join(shlex.split(value))
            except ValueError:
                pass
    if info.get("RAUC_MF_VERSION") != str(version):
        raise ReleaseError(f"the bundle says it is version {info.get('RAUC_MF_VERSION')!r}, not {version}")
    compatible = info.get("RAUC_MF_COMPATIBLE")
    if compatible not in COMPATIBLES:
        raise ReleaseError(f"the bundle is for {compatible!r}, not for a system URBAN builds ({', '.join(COMPATIBLES)})")
    return compatible, ("rauc verified the bundle's signature"
                        + ("" if keyring.exists() else f" (no {keyring}: against rauc's default keyring)"))


class ReleaseStore:
    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory if directory is not None else c.RELEASES)
        self.index = self.dir / "releases.json"
        self._seen = None  # (mtime, size, inode) of releases.json as last read
        self._items: list[dict] = []
        self._floor_seen, self._floor = None, None  # the same for floor.json

    # ---- reading: cheap enough to call on every device report (one stat)
    def releases(self) -> list[dict]:
        try:
            st = self.index.stat()
            seen = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            self._seen, self._items = None, []
            return self._items
        if seen != self._seen:
            try:
                data = json.loads(self.index.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return self._items  # unreadable right now: keep what we had until the next change
            self._items = ([r for r in data if isinstance(r, dict) and isinstance(r.get("version"), int)]
                           if isinstance(data, list) else [])
            self._seen = seen
        return self._items

    def get(self, version: int, compatible: str = DEFAULT_COMPATIBLE) -> dict | None:
        return next((r for r in self.releases() if r["version"] == version and compatible_of(r) == compatible), None)

    def of_version(self, version: int) -> list[dict]:
        """Every package of this version number, whatever system it is for (withdrawn ones too)."""
        return [r for r in self.releases() if r["version"] == version]

    def path(self, release: dict) -> Path:
        return self.dir / Path(str(release["file"])).name  # a file name, never a path

    def newest_for(self, device: str, compatible: str = DEFAULT_COMPATIBLE) -> dict | None:
        """The highest version for this compatible that is neither withdrawn nor halted and whose current wave includes
        this device."""
        best = None
        for r in self.releases():
            if (compatible_of(r) != compatible or r.get("withdrawn") or r.get("halted")
                    or not in_wave(r, device)):
                continue
            if best is None or r["version"] > best["version"]:
                best = r
        return best

    def newest(self) -> int | None:
        """The highest release still offered (to any device, of any compatible), or None."""
        return max((r["version"] for r in self.releases() if not r.get("withdrawn")), default=None)

    # ---- the version floor
    def floor_record(self) -> dict | None:
        """The floor as last set: {"floor": int, or None once cleared, "was", "by", "at", "id"}; None if it never was.
        As cheap as releases(): one stat while floor.json is unchanged. A file that is not a record we write is
        ignored and the last good record stays, as with an unreadable releases.json."""
        path = self.dir / FLOOR
        try:
            st = path.stat()
            seen = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            self._floor_seen, self._floor = None, None
            return None
        if seen != self._floor_seen:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return self._floor
            if (not isinstance(data, dict) or not (data.get("floor") is None or _version(data.get("floor")))
                    or not isinstance(data.get("id"), str)):
                return self._floor
            self._floor, self._floor_seen = data, seen
        return self._floor

    def floor(self) -> int | None:
        """The minimum system version the centre commands, or None: no floor."""
        rec = self.floor_record()
        return rec["floor"] if rec is not None else None

    def set_floor(self, floor: int | None, by: str = "cli", now: datetime.datetime | None = None) -> dict:
        """Set the floor (a positive whole number) or clear it (None). by: who did it, for the audit log."""
        if floor is not None and not _version(floor):
            raise ReleaseError("the floor is a system version: a positive whole number")
        rec = {"floor": floor, "was": self.floor(), "by": str(by)[:60] or "-",
               "at": (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "id": secrets.token_hex(8)}
        self._write_json(self.dir / FLOOR, rec)
        self._floor_seen = None  # read back on the next call
        return rec

    def floor_audited(self) -> str | None:
        """The id of the floor record the platform last wrote into its audit log, or None."""
        try:
            return (self.dir / FLOOR_AUDITED).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def mark_floor_audited(self, ident: str):
        self._write_json(self.dir / FLOOR_AUDITED, None, text=ident)

    # ---- writing
    def _write_json(self, path: Path, data, text: str | None = None):
        """Whole or not at all: another process may read the file at any moment."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text if text is not None else json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _write(self, items: list[dict]):
        self._write_json(self.index, items)

    def _now(self, now: datetime.datetime | None) -> str:
        return (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _live(self, items: list[dict], version: int, compatible: str | None) -> list[dict]:
        """The packages of this version (of one compatible, or of every one) still offered; ReleaseError if none."""
        found = [r for r in items if r["version"] == version and compatible in (None, compatible_of(r))]
        what = f"release {version}" + (f" for {compatible}" if compatible else "")
        if not found:
            raise ReleaseError(f"no {what}")
        live = [r for r in found if not r.get("withdrawn")]
        if not live:
            raise ReleaseError(f"{what} is already withdrawn")
        return live

    def add(self, source: Path, version: int, devices: list[str] | None = None,
            now: datetime.datetime | None = None, compatible: str = DEFAULT_COMPATIBLE, wave: int | None = None) -> dict:
        """Keep a package for one compatible. Its first wave: every device (devices and wave None), the listed devices,
        or a share of the fleet (wave: a percentage, 1-100). A package of a version number that is halted starts halted."""
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ReleaseError("the version is a positive whole number")
        if compatible not in COMPATIBLES:
            raise ReleaseError(f"the package is for {compatible!r}, not for a system URBAN builds ({', '.join(COMPATIBLES)})")
        source = Path(source)
        if not source.is_file() or source.stat().st_size == 0:
            raise ReleaseError(f"{source} is not a package file")
        if devices is not None and wave is not None:
            raise ReleaseError("the first wave is --devices or --wave, not both")
        if devices is not None:
            devices = sorted(set(devices))
            if not devices or not all(DEVICE_ID.fullmatch(d) for d in devices):
                raise ReleaseError("--devices: a comma-separated list of device ids, for example edge-J-21,district-1")
        if wave is not None and (isinstance(wave, bool) or not isinstance(wave, int) or not 1 <= wave <= 100):
            raise ReleaseError("--wave: a share of the fleet in percent, 1 to 100")
        items = list(self.releases())
        live = max((r["version"] for r in items if not r.get("withdrawn") and compatible_of(r) == compatible), default=0)
        if version <= live:
            raise ReleaseError(f"version {version} is not newer than release {live} for {compatible}, which is still offered")
        halted = next((r["halted"] for r in items if r["version"] == version and not r.get("withdrawn") and r.get("halted")),
                      None)
        digest, size = sha256_of(source)
        # x86 keeps the file name it had before there were two systems
        name = (f"urban-{version}.raucb" if compatible == DEFAULT_COMPATIBLE
                else f"urban-{compatible.removeprefix('urban-edge-')}-{version}.raucb")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (name + ".tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, self.dir / name)
        rel = {"version": version, "compatible": compatible, "file": name, "sha256": digest, "size": size,
               "devices": devices, "wave": wave, "added": self._now(now), "withdrawn": False, "halted": halted}
        # a withdrawn release of that number and compatible is replaced
        self._write([r for r in items if (r["version"], compatible_of(r)) != (version, compatible)] + [rel])
        return rel

    def withdraw(self, version: int, compatible: str | None = None) -> list[dict]:
        """Withdraw the packages of this version (of one compatible, or every one still offered). Returns them."""
        items = [dict(r) for r in self.releases()]  # copies: the cache changes only when the file does
        live = self._live(items, version, compatible)
        for rel in live:
            rel["withdrawn"] = True
        self._write(items)
        return live

    def promote(self, version: int, wave: int | None = None, compatible: str | None = None) -> list[dict]:
        """Widen the wave of this version's packages: to a larger share of the fleet (wave, a percentage), or to every
        device (wave None). The listed devices of a first wave stay in it. Returns the packages."""
        if wave is not None and (isinstance(wave, bool) or not isinstance(wave, int) or not 1 <= wave <= 100):
            raise ReleaseError("--wave: a share of the fleet in percent, 1 to 100")
        items = [dict(r) for r in self.releases()]
        live = self._live(items, version, compatible)
        for rel in live:
            if rel.get("devices") is None and rel.get("wave") is None:
                raise ReleaseError(f"release {version} for {compatible_of(rel)} already goes to every unit and district")
            if wave is not None and wave <= (rel.get("wave") or 0):
                raise ReleaseError(f"release {version} for {compatible_of(rel)} already goes to {rel['wave']}% of the "
                                   f"fleet: a wider wave is more than that")
        for rel in live:
            if wave is None:
                rel["devices"], rel["wave"] = None, None
            else:
                rel["wave"] = wave
        self._write(items)
        return live

    def halt(self, version: int, device: str, compatible: str, reason: str,
             now: datetime.datetime | None = None) -> dict | None:
        """Halt every package of this version still offered, after a failure or a rollback that `device` (running
        `compatible`) reported. Returns the halt record, or None when there is nothing to halt (it is halted already,
        or withdrawn)."""
        items = [dict(r) for r in self.releases()]
        live = [r for r in items if r["version"] == version and not r.get("withdrawn")]
        if not live or any(r.get("halted") for r in live):
            return None
        record = {"device": str(device)[:64], "compatible": str(compatible)[:64], "reason": str(reason)[:REASON_MAX],
                  "at": self._now(now)}
        for rel in live:
            rel["halted"] = record
        self._write(items)
        return record

    def resume(self, version: int) -> dict:
        """The admin's decision: a halted version is offered again (to its wave). Returns the halt that ended."""
        items = [dict(r) for r in self.releases()]
        live = self._live(items, version, None)
        halted = next((r["halted"] for r in live if r.get("halted")), None)
        if halted is None:
            raise ReleaseError(f"release {version} is not halted")
        for rel in live:
            rel["halted"] = None
        self._write(items)
        return halted


def _cli_actor() -> str:
    """Who changed the floor from the command line, for the audit log."""
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # no user name to be had (a bare container): it was still the command line
        return "cli"


def floor_text(rec: dict | None) -> str:
    if rec is None or rec.get("floor") is None:
        return "no floor: devices of every system version get commands"
    return f"floor: {rec['floor']}, set by {rec.get('by', '-')} at {rec.get('at', '-')}"


def halt_text(halted: dict) -> str:
    return f"{halted.get('device', '-')} ({halted.get('compatible', '-')}): {halted.get('reason', '-')}, at {halted.get('at', '-')}"


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="URBAN OS update packages of the control centre")
    ap.add_argument("--dir", default=None, help=f"the releases folder (default: {c.RELEASES})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="keep a package and offer it to the devices of its system on an older version")
    add.add_argument("file")
    add.add_argument("--version", type=int, required=True, help="the system version this package installs")
    first = add.add_mutually_exclusive_group()
    first.add_argument("--devices", help="a first wave of these device ids only (comma-separated); default: every unit "
                                         "and district")
    first.add_argument("--wave", type=int, help="a first wave of this share of the fleet, in percent (chosen by device id)")
    sub.add_parser("list")
    pr = sub.add_parser("promote", help="widen a release's wave: a larger share of the fleet, or every device")
    pr.add_argument("version", type=int)
    to = pr.add_mutually_exclusive_group(required=True)
    to.add_argument("--wave", type=int, help="this share of the fleet, in percent (the devices of the first wave stay)")
    to.add_argument("--all", action="store_true", help="every unit and district")
    pr.add_argument("--arch", help="only the package for x86_64 or aarch64 (default: every package of that version)")
    sub.add_parser("resume", help="a halted release is offered again to its wave").add_argument("version", type=int)
    wd = sub.add_parser("withdraw", help="stop offering a release")
    wd.add_argument("version", type=int)
    wd.add_argument("--arch", help="only the package for x86_64 or aarch64 (default: every package of that version)")
    fl = sub.add_parser("floor", help="the minimum system version the centre commands: show it, set it, or --clear it")
    fl.add_argument("version", type=int, nargs="?", help="devices that do not report this version or newer get no commands")
    fl.add_argument("--clear", action="store_true", help="no minimum: devices of every system version get commands again")
    args = ap.parse_args(argv)

    store = ReleaseStore(Path(args.dir) if args.dir else None)
    try:
        if args.cmd == "add":
            devices = [d.strip() for d in args.devices.split(",") if d.strip()] if args.devices is not None else None
            if args.devices is not None and not devices:
                raise ReleaseError("--devices is empty")
            if args.wave is not None and not 1 <= args.wave <= 100:
                raise ReleaseError("--wave: a share of the fleet in percent, 1 to 100")
            compatible, note = check_bundle(Path(args.file), args.version)
            rel = store.add(Path(args.file), args.version, devices, compatible=compatible, wave=args.wave)
            whom = (f"a first wave: {', '.join(devices)}" if devices is not None
                    else f"a first wave: {wave_text(rel)}" if args.wave is not None else wave_text(rel))
            print(f"added release {rel['version']} for {compatible}: {rel['size']} bytes, sha256 {rel['sha256'][:12]}..., "
                  f"to {whom}; {note}")
            if rel["halted"]:
                print(f"halted: release {rel['version']} was halted by {halt_text(rel['halted'])}; nothing of it is "
                      f"offered until `release resume {rel['version']}`")
        elif args.cmd == "promote":
            for rel in store.promote(args.version, None if args.all else args.wave, compatible_arg(args.arch)):
                print(f"promoted release {rel['version']} for {compatible_of(rel)}: now to {wave_text(rel)}")
                if rel.get("halted"):
                    print(f"halted: release {rel['version']} was halted by {halt_text(rel['halted'])}; nothing of it "
                          f"is offered until `release resume {rel['version']}`")
        elif args.cmd == "resume":
            halted = store.resume(args.version)
            print(f"resumed release {args.version}: offered again to its wave (it was halted by {halt_text(halted)})")
        elif args.cmd == "withdraw":
            for rel in store.withdraw(args.version, compatible_arg(args.arch)):
                print(f"withdrawn release {rel['version']} for {compatible_of(rel)}: it is no longer offered or served")
        elif args.cmd == "floor":
            if args.clear and args.version is not None:
                raise ReleaseError("give a version or --clear, not both")
            if args.clear:
                rec = store.set_floor(None, by=_cli_actor())
                print(f"floor cleared (was {rec['was'] or 'none'}): devices of every system version get commands again")
            elif args.version is not None:
                rec = store.set_floor(args.version, by=_cli_actor())
                print(f"floor set to {rec['floor']} (was {rec['was'] or 'none'}): units, district servers and lighting "
                      f"cabinets that do not report system {rec['floor']} or newer get no commands, only update offers")
                newest = store.newest()
                if newest is None or newest < rec["floor"]:
                    print(f"warning: no release offered here is {rec['floor']} or newer (newest: {newest or 'none'}): "
                          f"devices below the floor cannot be updated from the centre")
            else:
                print(floor_text(store.floor_record()))
        else:
            rows = store.releases()
            for r in sorted(rows, key=lambda r: (r["version"], compatible_of(r))):
                to = "all" if r.get("devices") is None and r.get("wave") is None else wave_text(r)
                state = "WITHDRAWN" if r.get("withdrawn") else "HALTED   " if r.get("halted") else "offered  "
                print(f"{r['version']:5} {state} {compatible_of(r):<18} {r['size']:>11} bytes  "
                      f"{str(r.get('sha256', ''))[:12]}  {r.get('added', '')}  to {to}")
                if r.get("halted") and not r.get("withdrawn"):
                    print(f"      halted by {halt_text(r['halted'])}")
            if not rows:
                print("no releases")
            print(floor_text(store.floor_record()))
    except ReleaseError as err:
        raise SystemExit(f"refused: {err}")


if __name__ == "__main__":
    main()
