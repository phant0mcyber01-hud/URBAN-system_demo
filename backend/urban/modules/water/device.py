"""One pump station's unit: identity, the readings, what it works out, and its refusal of every command. No
networking here.

The station only reads. A command from the centre is checked like any device checks one (edge.device.envelope_refusal:
signed by the gateway key it was provisioned with, addressed to it, a sequence number above anything seen, fresh) and
then, whatever it says, refused: "станция только читает: команд нет". The reason goes up in the next report
("refused"), once. The one thing it accepts is an OS update offer, through the same signed path every device uses
(edge/update.py). The sequence high-water mark survives restarts (state_path), so even a refused command cannot be
replayed to fill the reports.

poll() reads the station (EmulatedStation, ModbusStation: adapters.py) about once a second. A failure to reach the
automation never stops the program: the values become null and source_error says why in a few words.
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path

from ...devices import crypto
from ...devices.trust import GatewayTrust
from ...edge.device import VERSION, envelope_refusal
from .observe import Observations
from .regmap import MAX_PUMPS, within

READ_ONLY = "станция только читает: команд нет"
ERROR_MAX = 120
VALUES = ("pressure_in", "pressure_out", "flow", "power_kw", "energy_kwh", "level")
DIGITS = {"pressure_in": 3, "pressure_out": 3, "flow": 2, "power_kw": 2, "energy_kwh": 3, "level": 3}


def _plain(text: str) -> str:
    return "".join(ch for ch in text if " " <= ch <= "~" or "Ѐ" <= ch <= "ӿ")


def _write_atomic(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _source_error(err: Exception) -> str:
    """A short text for the centre: the error's own words if it is ours (no paths in them), else its class."""
    text = str(err) if err.__class__.__name__ in ("ModbusError", "WriteRefused") else err.__class__.__name__
    return _plain(text)[:ERROR_MAX] or "error"


class WaterStation:
    def __init__(self, device_id: str, site: str, key, gateway_public, adapter, state_path: Path | None = None,
                 wall=time.time, root_public=None, gateway_state: Path | None = None, updater=None,
                 observations: Observations | None = None, lon: float | None = None, lat: float | None = None):
        """site: the station this device is installed at (S-01); adapter: EmulatedStation, ModbusStation or anything
        with read(now) -> readings and close(); wall(): wall-clock seconds, injected so a test runs a week in a moment;
        state_path: where the last accepted sequence number is kept; observations: kwh_per_m3 and the night minimum
        (observe.py), kept in a file; root_public, gateway_state: follow gateway key rotations (devices/trust.py);
        updater: takes the centre's signed OS update offers and reports the system version (edge/update.py);
        lon, lat: where it stands, for the machine's screen only (the centre's registry holds the place)."""
        for name, v, limit in (("lon", lon, 180), ("lat", lat, 90)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                                  or abs(v) > limit):
                raise ValueError(f"{name} must be degrees within +-{limit}")
        self.id, self.site, self.key, self.adapter = device_id, site, key, adapter
        self.lon, self.lat = (float(lon) if lon is not None else None), (float(lat) if lat is not None else None)
        self.trust = GatewayTrust(root_public, gateway_public, gateway_state)
        self.state_path, self.wall, self.updater = state_path, wall, updater
        self.obs = observations if observations is not None else Observations(None)
        self.started = time.monotonic()
        self.last_seq = 0
        if state_path is not None and state_path.exists():
            try:
                self.last_seq = int(state_path.read_text())
            except (OSError, ValueError):
                pass
        self.reading: dict = {name: None for name in VALUES} | {"pumps": [], "door": "unknown"}
        self.source_error: str | None = None     # why the last reading failed, or None
        self.pending_refusal: str | None = None  # waits for the next report that is really sent
        self.last_refusal: str | None = None
        self.refused: str | None = None          # why the centre's certificate was not accepted (on_welcome)
        self.commands_refused = 0

    # ---- session
    @property
    def gateway_public(self):
        return self.trust.public

    def hello(self, nonce: str) -> dict:
        return {"type": "hello", "device": self.id, "version": VERSION, "epoch": self.trust.epoch,
                "sig": crypto.sign(self.key, {"device": self.id, "nonce": nonce})}

    def on_welcome(self, msg: dict) -> bool:
        """The platform presents its gateway certificate; one this station cannot trust is refused."""
        why = self.trust.offer(msg.get("gateway") if isinstance(msg, dict) else None)
        if why:
            self.refused = why
            return False
        return True

    # ---- commands from the centre: none are obeyed, an OS update is taken
    def on_command(self, env) -> bool:
        why = envelope_refusal(env, self.id, self.gateway_public, self.last_seq, self.wall)
        if why is None:
            # a verified envelope uses up its number, refused or not, so it cannot be replayed later
            self.last_seq = env["seq"]
            if self.state_path is not None:
                try:
                    _write_atomic(self.state_path, str(self.last_seq))
                except OSError:
                    pass  # keeping the number in memory still stops replays until the next restart
            body = env["body"]
            if isinstance(body, dict) and set(body) == {"update"}:  # an OS update offer: for this device, not the pumps
                if self.updater is not None:
                    self.updater.offer(body["update"])
                return True
            why = READ_ONLY  # a `water` command or anything else: this station has no commands
        self.pending_refusal = self.last_refusal = why
        self.commands_refused += 1
        return False

    # ---- the readings: about once a second, link or not
    def poll(self) -> None:
        now = self.wall()
        try:
            raw = self.adapter.read(now)
            if not isinstance(raw, dict):
                raise ValueError("adapter gave no readings")
        except (OSError, ValueError, KeyError, TypeError) as err:
            self.reading = {name: None for name in VALUES} | {"pumps": [], "door": "unknown"}
            self.source_error = _source_error(err)
            self.obs.observe(now, None, None)
            return
        reading = {}
        for name in VALUES:  # out of what the centre accepts -> null, so one odd register never costs the report
            v = within(name, raw.get(name))
            reading[name] = round(v, DIGITS[name]) if v is not None else None
        pumps = [p for p in raw.get("pumps") or [] if isinstance(p, dict)] if isinstance(raw.get("pumps"), list) else []
        reading["pumps"] = [{k: p.get(k) if isinstance(p.get(k), bool) else None for k in ("run", "fault")}
                            for p in pumps[:MAX_PUMPS]]
        reading["door"] = raw.get("door") if raw.get("door") in ("open", "closed", "unknown") else "unknown"
        self.reading, self.source_error = reading, None
        self.obs.observe(now, reading["flow"], reading["energy_kwh"])

    # ---- what goes up
    def water(self) -> dict:
        now = self.wall()
        night_min, baseline = self.obs.night(now)
        out = {"site": self.site, **self.reading, "pumps": [dict(p) for p in self.reading["pumps"]],
               "kwh_per_m3": within("kwh_per_m3", self.obs.kwh_per_m3()),
               "night_min_flow": within("night_min_flow", night_min),
               "night_baseline": within("night_baseline", baseline),
               "source_error": self.source_error, "refused": self.pending_refusal}
        return out

    def report(self) -> dict:
        """The report envelope. A refusal is in it until mark_sent(): a report that never left does not use it up."""
        health = {"version": VERSION, "uptime": round(time.monotonic() - self.started),
                  "controller": self.source_error or "ok",
                  **(self.updater.health() if self.updater is not None else {})}
        return {"type": "report", "t": int(self.wall()), "epoch": self.trust.epoch, "health": health,
                "water": self.water()}

    def mark_sent(self, report: dict):
        if report.get("water", {}).get("refused") == self.pending_refusal:
            self.pending_refusal = None

    def close(self):
        self.obs.close()
        try:
            self.adapter.close()
        except Exception:
            pass
