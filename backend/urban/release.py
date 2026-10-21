"""Released OS update packages: what the control centre keeps and offers to its devices.

    python -m urban.release add urban-edge-3.raucb --version 3 [--devices edge-J-21,district-1]
    python -m urban.release list
    python -m urban.release withdraw 3
    python -m urban.release floor 5          the minimum system version the centre commands
    python -m urban.release floor            show it
    python -m urban.release floor --clear    no minimum: every system version is commanded again

A release is a RAUC bundle (urban-edge-<N>.raucb) signed with URBAN's build key. `add` copies it into the
releases folder next to releases.json; the device hub then offers it, in a signed message, to every unit and
district server (all of them, or only the listed ones) whose reported system version is older. A withdrawn
release is never offered or served again. The platform may be running meanwhile: it notices the change itself.

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


def check_bundle(path: Path, version: int, keyring: Path = KEYRING, which=shutil.which, run=subprocess.run) -> str:
    """Ask RAUC what it thinks of the bundle: its signature against the build keyring, and its own version.
    Returns what was checked (or that rauc is not here); raises ReleaseError when RAUC disagrees."""
    rauc = which("rauc")
    if rauc is None:
        return "rauc is not installed here: the bundle's signature and version were not cross-checked"
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
    return "rauc verified the bundle's signature" + ("" if keyring.exists() else f" (no {keyring}: against rauc's default keyring)")


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

    def get(self, version: int) -> dict | None:
        return next((r for r in self.releases() if r["version"] == version), None)

    def path(self, release: dict) -> Path:
        return self.dir / Path(str(release["file"])).name  # a file name, never a path

    def newest_for(self, device: str) -> dict | None:
        """The highest version that is not withdrawn and targets this device (devices null: every device)."""
        best = None
        for r in self.releases():
            if r.get("withdrawn") or (r.get("devices") is not None and device not in r["devices"]):
                continue
            if best is None or r["version"] > best["version"]:
                best = r
        return best

    def newest(self) -> int | None:
        """The highest release still offered (to any device), or None."""
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

    def add(self, source: Path, version: int, devices: list[str] | None = None,
            now: datetime.datetime | None = None) -> dict:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ReleaseError("the version is a positive whole number")
        source = Path(source)
        if not source.is_file() or source.stat().st_size == 0:
            raise ReleaseError(f"{source} is not a package file")
        if devices is not None:
            devices = sorted(set(devices))
            if not devices or not all(DEVICE_ID.fullmatch(d) for d in devices):
                raise ReleaseError("--devices: a comma-separated list of device ids, for example edge-J-21,district-1")
        items = list(self.releases())
        live = max((r["version"] for r in items if not r.get("withdrawn")), default=0)
        if version <= live:
            raise ReleaseError(f"version {version} is not newer than release {live}, which is still offered")
        digest, size = sha256_of(source)
        name = f"urban-{version}.raucb"
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (name + ".tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, self.dir / name)
        rel = {"version": version, "file": name, "sha256": digest, "size": size, "devices": devices,
               "added": (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "withdrawn": False}
        self._write([r for r in items if r["version"] != version] + [rel])  # a withdrawn release of that number is replaced
        return rel

    def withdraw(self, version: int) -> dict:
        items = [dict(r) for r in self.releases()]  # a copy: the cache changes only when the file does
        rel = next((r for r in items if r["version"] == version), None)
        if rel is None:
            raise ReleaseError(f"no release {version}")
        if rel.get("withdrawn"):
            raise ReleaseError(f"release {version} is already withdrawn")
        rel["withdrawn"] = True
        self._write(items)
        return rel


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


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="URBAN OS update packages of the control centre")
    ap.add_argument("--dir", default=None, help=f"the releases folder (default: {c.RELEASES})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="keep a package and offer it to the devices on an older version")
    add.add_argument("file")
    add.add_argument("--version", type=int, required=True, help="the system version this package installs")
    add.add_argument("--devices", help="offer only to these device ids (comma-separated); default: every unit and district")
    sub.add_parser("list")
    sub.add_parser("withdraw", help="stop offering a release").add_argument("version", type=int)
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
            note = check_bundle(Path(args.file), args.version)
            rel = store.add(Path(args.file), args.version, devices)
            print(f"added release {rel['version']}: {rel['size']} bytes, sha256 {rel['sha256'][:12]}..., "
                  f"to {', '.join(devices) if devices else 'every unit and district'}; {note}")
        elif args.cmd == "withdraw":
            store.withdraw(args.version)
            print(f"withdrawn release {args.version}: it is no longer offered or served")
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
            for r in sorted(rows, key=lambda r: r["version"]):
                to = ", ".join(r["devices"]) if r.get("devices") is not None else "all"
                print(f"{r['version']:5} {'WITHDRAWN' if r.get('withdrawn') else 'offered  '} {r['size']:>11} bytes  "
                      f"{str(r.get('sha256', ''))[:12]}  {r.get('added', '')}  to {to}")
            if not rows:
                print("no releases")
            print(floor_text(store.floor_record()))
    except ReleaseError as err:
        raise SystemExit(f"refused: {err}")


if __name__ == "__main__":
    main()
