"""Released OS update packages: what the control centre keeps and offers to its devices.

    python -m urban.release add urban-edge-3.raucb --version 3 [--devices edge-J-21,district-1]
    python -m urban.release list
    python -m urban.release withdraw 3

A release is a RAUC bundle (urban-edge-<N>.raucb) signed with URBAN's build key. `add` copies it into the
releases folder next to releases.json; the device hub then offers it, in a signed message, to every unit and
district server (all of them, or only the listed ones) whose reported system version is older. A withdrawn
release is never offered or served again. The platform may be running meanwhile: it notices the change itself.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from . import config as c

KEYRING = Path("/etc/rauc/keyring.pem")
DEVICE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


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

    # ---- writing
    def _write(self, items: list[dict]):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_name(self.index.name + ".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.index)

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
        else:
            rows = store.releases()
            for r in sorted(rows, key=lambda r: r["version"]):
                to = ", ".join(r["devices"]) if r.get("devices") is not None else "all"
                print(f"{r['version']:5} {'WITHDRAWN' if r.get('withdrawn') else 'offered  '} {r['size']:>11} bytes  "
                      f"{str(r.get('sha256', ''))[:12]}  {r.get('added', '')}  to {to}")
            if not rows:
                print("no releases")
    except ReleaseError as err:
        raise SystemExit(f"refused: {err}")


if __name__ == "__main__":
    main()
