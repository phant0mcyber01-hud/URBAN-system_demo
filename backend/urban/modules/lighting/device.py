"""One street-lighting cabinet: identity, command checks, the lease and the schedule. No networking here.

A command is obeyed only if all of this holds (the first four are edge.device.envelope_refusal, shared with the
junction unit and the district server):
- it is signed by the gateway key this cabinet was provisioned with;
- it is addressed to this device;
- its sequence number is higher than anything seen before (no replays);
- it is fresh: issued within MAX_SKEW seconds of the cabinet's wall clock;
- it is about this cabinet, with an action set/release, a level 0-100 and a lease of 30-300 seconds.
Everything else is refused, and the reason goes up in the next report ("refused"), once.

`set` is a lease: the level holds until the cabinet's clock reaches now+ttl. When the lease ends, or `release`
arrives, or the centre never comes back, the cabinet returns to its own schedule: the astronomical timer for its
longitude and latitude, or the photocell if one is wired. tick() is what ends leases, and it runs whether or
not there is a link to the centre. The sequence high-water mark survives restarts (state_path).
"""
from __future__ import annotations

import math
import os
import random
import time
from pathlib import Path

from ...devices import crypto
from ...devices.trust import GatewayTrust
from ...edge.device import VERSION, envelope_refusal
from . import solar

MIN_TTL, MAX_TTL = 30, 300
FULL = 100


def _int(v, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


# ------------------------------------------------------------------ what the cabinet is wired to

class EmulatedAdapter:
    """Lamps that are not there: for the demo and for checks. `failed` lamps (or one with some probability when
    only a `seed` is given) never light; the power is what the burning ones would draw."""

    def __init__(self, lamps: int = 24, failed: int | None = None, seed: int | None = None, watts: float = 90.0,
                 door: str = "closed"):
        self.lamps, self.watts, self.door = lamps, float(watts), door
        if failed is None:
            failed = random.Random(seed).choice([0, 0, 1]) if seed is not None else 0
        self.failed = max(0, min(lamps, failed))
        self.level = 0
        self.error: str | None = None

    def apply(self, level: int):
        self.level = level

    def photocell(self) -> bool | None:
        return None  # the emulated cabinet has none: it follows the astronomical timer

    def snapshot(self) -> dict:
        burning = (self.lamps - self.failed) if self.level > 0 else 0
        return {"lamps": self.lamps, "failed": self.failed,
                "power_w": round(burning * self.watts * self.level / 100.0, 1), "door": self.door}


class RelayAdapter:
    """One contactor, driven through a file: a sysfs GPIO value (/sys/class/gpio/gpio17/value), or any device
    file a relay board exposes. It writes "1" or "0"; with dimming=True it writes the level (0-100) instead.
    Without dimming, any level above 0 is full on, and that is what the cabinet then reports (on/off, not the
    level asked for). The door switch and the photocell are optional input files; without them the door is
    "unknown" and the schedule is astronomical. A write that fails keeps the last known state, is reported
    as the cabinet's controller fault and is tried again at the next tick."""

    REASSERT = 30.0  # seconds after which the output is written again even if unchanged

    def __init__(self, path: str | Path, dimming: bool = False, lamps: int = 0, watts: float = 90.0,
                 door_path: str | Path | None = None, door_open: str = "1",
                 photocell_path: str | Path | None = None, photocell_dark: str = "1", clock=time.monotonic):
        self.path, self.dimming, self.lamps, self.watts = Path(path), dimming, lamps, float(watts)
        self.door_path = Path(door_path) if door_path else None
        self.door_open = door_open
        self.photocell_path = Path(photocell_path) if photocell_path else None
        self.photocell_dark = photocell_dark
        self.clock = clock
        self.level = 0
        self.error: str | None = None
        self._written: str | None = None
        self._at = 0.0

    def apply(self, level: int):
        value = str(level) if self.dimming else ("1" if level > 0 else "0")
        if value == self._written and self.error is None and self.clock() - self._at < self.REASSERT:
            return
        try:
            self.path.write_text(value, encoding="ascii")
        except OSError as err:
            self.error = f"relay: {err.__class__.__name__}"  # no path, no errno text: this goes to the centre
            return
        self.error, self._written, self._at = None, value, self.clock()
        self.level = level if self.dimming else (FULL if level > 0 else 0)

    def photocell(self) -> bool | None:
        if self.photocell_path is None:
            return None
        try:
            return self.photocell_path.read_text().strip() == self.photocell_dark
        except OSError:
            return None  # unreadable: the astronomical timer decides

    def door(self) -> str:
        if self.door_path is None:
            return "unknown"
        try:
            return "open" if self.door_path.read_text().strip() == self.door_open else "closed"
        except OSError:
            return "unknown"

    def snapshot(self) -> dict:
        power = self.lamps * self.watts * self.level / 100.0 if self.lamps else 0.0
        return {"lamps": self.lamps, "failed": 0, "power_w": round(power, 1), "door": self.door()}


# ------------------------------------------------------------------ the cabinet

def _write_atomic(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class LightingCabinet:
    def __init__(self, device_id: str, cabinet: str, key, gateway_public, adapter, lon: float, lat: float,
                 state_path: Path | None = None, wall=time.time, root_public=None, gateway_state: Path | None = None,
                 updater=None):
        """cabinet: the object this device is installed at (C-01); lon/lat: where it stands, for the schedule;
        adapter: EmulatedAdapter, RelayAdapter or anything with apply(level), photocell(), snapshot(), error;
        wall(): wall-clock seconds, injected so a test runs a night in a moment;
        state_path: where the last accepted sequence number is kept;
        root_public, gateway_state: follow gateway key rotations (devices/trust.py), as every device does;
        updater: takes the centre's signed OS update offers and reports the system version (edge/update.py)."""
        if not (isinstance(lon, (int, float)) and isinstance(lat, (int, float)) and math.isfinite(lon)
                and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("lon and lat must be degrees, e.g. 71.78 and 40.39")
        self.id, self.cabinet, self.key, self.adapter = device_id, cabinet, key, adapter
        self.lon, self.lat = float(lon), float(lat)
        self.trust = GatewayTrust(root_public, gateway_public, gateway_state)
        self.state_path, self.wall, self.updater = state_path, wall, updater
        self.started = time.monotonic()
        self.last_seq = 0
        if state_path is not None and state_path.exists():
            try:
                self.last_seq = int(state_path.read_text())
            except (OSError, ValueError):
                pass
        self.lease: dict | None = None           # {"level", "until"} while a command of the centre holds
        self.pending_refusal: str | None = None  # waits for the next report that is really sent
        self.last_refusal: str | None = None
        self.refused: str | None = None          # why the centre's certificate was not accepted (on_welcome)
        self.dark = False
        self.schedule = "astro"
        self.tick()

    # ---- session
    @property
    def gateway_public(self):
        return self.trust.public

    def hello(self, nonce: str) -> dict:
        return {"type": "hello", "device": self.id, "version": VERSION, "epoch": self.trust.epoch,
                "sig": crypto.sign(self.key, {"device": self.id, "nonce": nonce})}

    def on_welcome(self, msg: dict) -> bool:
        """The platform presents its gateway certificate; one this cabinet cannot trust is refused. The welcome
        names no junction for a cabinet, and none is asked for."""
        why = self.trust.offer(msg.get("gateway") if isinstance(msg, dict) else None)
        if why:
            self.refused = why
            return False
        return True

    # ---- commands from the centre
    def on_command(self, env) -> bool:
        why = self._refusal(env)
        if why:
            self.pending_refusal = self.last_refusal = why
            return False
        body = env["body"]
        if "update" in body:  # an OS update offer: for this device, not for the lamps
            if self.updater is not None:
                self.updater.offer(body["update"])
            return True
        cmd = body["lighting"]
        if cmd["action"] == "release":
            self.lease = None
        else:
            self.lease = {"level": cmd["level"], "until": self.wall() + cmd["ttl"]}
        self.tick()
        return True

    def _refusal(self, env) -> str | None:
        """Why this command is not obeyed, or None. A verified envelope uses up its sequence number even when its
        body is refused, so it cannot be replayed later."""
        why = envelope_refusal(env, self.id, self.gateway_public, self.last_seq, self.wall)
        if why:
            return why
        self.last_seq = env["seq"]
        if self.state_path is not None:
            try:
                _write_atomic(self.state_path, str(self.last_seq))
            except OSError:
                pass  # keeping the number in memory still stops replays until the next restart
        body = env["body"]
        if isinstance(body, dict) and "update" in body:
            return None
        cmd = body.get("lighting") if isinstance(body, dict) else None
        if not isinstance(cmd, dict):
            return "not a lighting command"
        if cmd.get("cabinet") != self.cabinet:
            return "not my cabinet"
        action = cmd.get("action")
        if action == "release":
            return None
        if action != "set":
            return "unknown action"
        if not _int(cmd.get("level"), 0, 100):
            return "bad level"
        if not _int(cmd.get("ttl"), MIN_TTL, MAX_TTL):
            return "bad ttl"
        return None

    # ---- the clock: runs with or without a link
    def tick(self):
        """Let a lapsed lease go and put the schedule or the lease on the output. Cheap; call it every second."""
        now = self.wall()
        if self.lease is not None:
            ahead = self.lease["until"] - now
            if ahead <= 0 or ahead > MAX_TTL:  # ended, or the clock jumped back: never hold longer than granted
                self.lease = None
        photocell = self.adapter.photocell()
        if photocell is None:
            self.schedule, self.dark = "astro", solar.is_dark(now, self.lat, self.lon)
        else:
            self.schedule, self.dark = "photocell", photocell
        wanted = self.lease["level"] if self.lease is not None else (FULL if self.dark else 0)
        self.adapter.apply(wanted)

    # ---- what goes up
    def lighting(self) -> dict:
        snap = self.adapter.snapshot()
        level = getattr(self.adapter, "level", 0)
        out = {"cabinet": self.cabinet, "mode": "override" if self.lease is not None else "schedule",
               "on": level > 0, "level": level, "lamps": snap["lamps"], "failed": snap["failed"],
               "power_w": snap["power_w"], "door": snap["door"],
               "lease_until": int(math.ceil(self.lease["until"])) if self.lease is not None else None,
               "schedule": self.schedule, "dark": bool(self.dark)}
        if self.pending_refusal:
            out["refused"] = self.pending_refusal
        return out

    def report(self) -> dict:
        """The report envelope. A refusal is in it until mark_sent(): a report that never left does not use it up."""
        health = {"version": VERSION, "uptime": round(time.monotonic() - self.started),
                  "controller": getattr(self.adapter, "error", None) or "ok",
                  **(self.updater.health() if self.updater is not None else {})}
        return {"type": "report", "t": int(self.wall()), "epoch": self.trust.epoch, "health": health,
                "lighting": self.lighting()}

    def mark_sent(self, report: dict):
        if report.get("lighting", {}).get("refused") == self.pending_refusal:
            self.pending_refusal = None
