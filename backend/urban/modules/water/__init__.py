"""Water utility (Водоканал), the third City OS module: pump stations, READ-ONLY. The centre's side.
Design and the binding contract: docs/water-module.md.

A unit in a pump station's cabinet reads the station's automation over Modbus and reports its pressure, flow, power,
energy counter, pumps and door every 5 seconds. This module shows the stations and raises alerts; it sends nothing.
It has one route, GET /state, and no command of any kind: not a POST, not a Core.send. A pump is not a traffic light:
a wrong command can burst a main or leave a district without water, so control comes only after reading has run on
real stations and the utility has approved the order of commands.

A station's report is untrusted: it is accepted only for the unit's own station, field by field, with numbers finite
and in range and words from a fixed set; a report with anything wrong is dropped whole. The pressure limits come from
the registry record (set by the administrator at enrolment), never from the station, so a station cannot switch off
its own alarm. Alerts and the last reports live in memory: after a restart of the platform they are back within one
report. The start and the end of each station alert (source, pressure-*, pump-fault, door, leak) are audited once
(water-alert / water-alert-cleared), not at every report.
"""
from __future__ import annotations

import asyncio
import math
import threading

from fastapi import APIRouter, Depends, HTTPException

from ... import config as c
from .regmap import LIMITS, MAX_PUMPS
from .session import run_demo_stations

SILENT = 30          # seconds without a report: the station counts as offline
REFUSAL_SHOWN = 300  # seconds a refused command stays an alert
TEXT_MAX = 120
LEAK_FACTOR = 1.2    # last night's minimum above this times the usual one: a possible leak
DOORS = ("open", "closed", "unknown")
DEMO_STATIONS = 3
AUDITED = ("source", "pressure-low", "pressure-high", "pump-fault", "door", "leak")

__all__ = ["WaterModule", "run_demo_stations", "validate_report"]


def _num(v, lo: float, hi: float) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def _plain(text: str) -> str:
    """Printable ASCII and Cyrillic only: no control characters, no look-alike or invisible ones."""
    return "".join(ch for ch in text if " " <= ch <= "~" or "Ѐ" <= ch <= "ӿ")


def _text(v) -> tuple[bool, str | None]:
    """(acceptable, cleaned text or None) for an optional short text."""
    if v is None:
        return True, None
    if not isinstance(v, str):
        return False, None
    return True, _plain(v).strip()[:TEXT_MAX] or None


def validate_report(data, site: str) -> dict | None:
    """The `water` object of a report if every field is what a station sends and it is about `site`, else None.
    Never raises. Every field but `site` may be null or absent (unknown)."""
    if not isinstance(data, dict) or not isinstance(site, str) or data.get("site") != site:
        return None
    out = {}
    for name, (lo, hi) in LIMITS.items():
        v = data.get(name)
        if v is not None and not _num(v, lo, hi):
            return None
        out[name] = None if v is None else round(float(v), 3)
    pumps = data.get("pumps")
    if pumps is None:
        pumps = []
    if not isinstance(pumps, list) or len(pumps) > MAX_PUMPS:
        return None
    out["pumps"] = []
    for pump in pumps:
        if not isinstance(pump, dict) or any(pump.get(k) is not None and not isinstance(pump.get(k), bool)
                                             for k in ("run", "fault")):
            return None
        out["pumps"].append({"run": pump.get("run"), "fault": pump.get("fault")})
    door = data.get("door")
    if door is not None and door not in DOORS:
        return None
    out["door"] = door or "unknown"
    for name in ("source_error", "refused"):
        ok, text = _text(data.get(name))
        if not ok:
            return None
        out[name] = text
    return out


def _limit(v) -> float | None:
    """A pressure limit from the registry record, if it is one."""
    return float(v) if _num(v, -1, 100) else None


def _fmt(v: float) -> str:
    return f"{v:g}"


def station_alerts(data: dict, p_min: float | None, p_max: float | None) -> list[dict]:
    """The alerts a station's last report raises with the centre's limits (not offline, refused or os-floor)."""
    alerts = []
    if data.get("source_error"):
        alerts.append({"kind": "source", "level": "alert",
                       "text": f"нет связи с автоматикой станции: {data['source_error']}"})
    p = data.get("pressure_out")
    if p is not None and p_min is not None and p < p_min:
        alerts.append({"kind": "pressure-low", "level": "alert",
                       "text": f"давление ниже нормы: {_fmt(p)} бар (норма от {_fmt(p_min)})"})
    if p is not None and p_max is not None and p > p_max:
        alerts.append({"kind": "pressure-high", "level": "alert",
                       "text": f"давление выше нормы: {_fmt(p)} бар (норма до {_fmt(p_max)})"})
    for n, pump in enumerate(data.get("pumps") or [], start=1):
        if pump.get("fault"):
            alerts.append({"kind": "pump-fault", "level": "alert", "text": f"авария насоса {n}", "pump": n})
    if data.get("door") == "open":
        alerts.append({"kind": "door", "level": "alert", "text": "шкаф открыт"})
    night, usual = data.get("night_min_flow"), data.get("night_baseline")
    # rounded: exactly 1.2 times is not yet a leak, whatever the binary fractions say
    if night is not None and usual is not None and usual > 0 and night > round(LEAK_FACTOR * usual, 6):
        alerts.append({"kind": "leak", "level": "warn",
                       "text": f"возможна утечка: ночной минимум {_fmt(night)} м³/ч против обычных {_fmt(usual)}"})
    return alerts


def _key(alert: dict) -> str:
    """What makes an alert the same one from report to report (its numbers change, it does not)."""
    return f"{alert['kind']}:{alert['pump']}" if "pump" in alert else alert["kind"]


def _forbidden():
    raise HTTPException(403, "no role for this")


class WaterModule:
    id = "water"
    title = "Водоканал"
    device_kind = "water"

    def __init__(self):
        self.core = None
        self.reports: dict[str, dict] = {}  # device id -> {"at", "data", "refusal": (text, at) | None, "raised": {}}
        self._lock = threading.RLock()
        self._demo: asyncio.Task | None = None

    # ---- the core
    def attach(self, core) -> None:
        self.core = core

    async def startup(self) -> None:
        """In the demo twin: three emulated stations in this process (the second with a pump in fault), connecting to
        the platform's device listener like real ones (the demo provisioning gives them keys). core.extra may name
        platform_addr, server_name, ca_path and pki_dir; the defaults are the platform's own settings."""
        core = self.core
        if core is None or not core.demo or self._demo is not None:
            return
        extra = core.extra or {}
        stations = [r for r in core.devices(self.device_kind) if r.get("status", "active") == "active"]
        if not stations:
            return
        pki = extra.get("pki_dir") or c.PKI
        self._demo = asyncio.ensure_future(run_demo_stations(
            extra.get("platform_addr") or f"127.0.0.1:{c.DEVICE_PORT}", extra.get("server_name") or c.DEVICE_NAME,
            extra.get("ca_path") or f"{pki}/ca.pem", pki, stations[:DEMO_STATIONS]))

    def stop(self) -> None:
        """Ends the demo stations (the platform may call it at shutdown)."""
        if self._demo is not None:
            self._demo.cancel()
            self._demo = None

    # ---- reports from stations
    def on_report(self, rec, report) -> None:
        try:
            if not isinstance(rec, dict) or not isinstance(report, dict):
                return
            ident, site = rec.get("id"), rec.get("site")
            if not isinstance(ident, str) or not isinstance(site, str):
                return
            data = validate_report(report.get("water"), site)
            if data is None:
                return
            with self._lock:
                now = self.core.now()
                refused = data.pop("refused")
                entry = self.reports.get(ident, {})
                entry.update(at=now, data=data)
                self.reports[ident] = entry
                if refused:
                    entry["refusal"] = (refused, now)
                    self.core.audit("water-refused", site, note=refused, actor=ident)
                self._audit_alerts(entry, rec, site, ident)
        except Exception:  # an untrusted report must never stop the hub
            return

    def _audit_alerts(self, entry: dict, rec: dict, site: str, ident: str) -> None:
        """Each station alert's start and end, once: compared with what the previous report raised."""
        now_raised = {_key(a): a["text"] for a in station_alerts(entry["data"], _limit(rec.get("p_min")),
                                                                  _limit(rec.get("p_max")))}
        before = entry.get("raised", {})
        for key, text in now_raised.items():
            if key not in before:
                self.core.audit("water-alert", site, note=text, actor=ident)
        for key, text in before.items():
            if key not in now_raised:
                self.core.audit("water-alert-cleared", site, note=text, actor=ident)
        entry["raised"] = {k: before.get(k, v) for k, v in now_raised.items()}  # the text it started with

    def tick(self, now: float) -> None:
        """Nothing to renew: the module commands nothing."""

    # ---- state
    def _known(self) -> dict[str, dict]:
        """site -> registry record, for the active stations."""
        out = {}
        for rec in self.core.devices(self.device_kind):
            if isinstance(rec, dict) and isinstance(rec.get("site"), str) and rec.get("status", "active") == "active":
                out[rec["site"]] = rec
        return out

    def _refusal(self, device: str) -> str | None:
        """Why the core would not command this station's unit now (below the version floor), or None. The module never
        commands it; the alert says the unit's system is too old."""
        try:
            why = self.core.refusal(device)
        except Exception:
            return None
        return _plain(why)[:TEXT_MAX] or None if isinstance(why, str) else None

    def _station_state(self, rec: dict, now: float) -> dict:
        rep = self.reports.get(rec["id"])
        online = bool(self.core.online(rec["id"])) and rep is not None and now - rep["at"] <= SILENT
        data = rep["data"] if rep else {}
        refusal = rep.get("refusal") if rep else None
        refused = refusal[0] if refusal and now - refusal[1] <= REFUSAL_SHOWN else None
        p_min, p_max = _limit(rec.get("p_min")), _limit(rec.get("p_max"))
        alerts = []
        if not online:
            alerts.append({"kind": "offline", "level": "warn", "text": "нет связи"})
        alerts += [{k: a[k] for k in ("kind", "level", "text")} for a in station_alerts(data, p_min, p_max)]
        if refused:
            alerts.append({"kind": "refused", "level": "warn", "text": f"станция отклонила команду: {refused}"})
        if (below := self._refusal(rec["id"])) is not None:
            alerts.append({"kind": "os-floor", "level": "alert", "text": below})

        def coord(v, limit):
            return float(v) if _num(v, -limit, limit) else None

        return {"id": rec["id"], "site": rec["site"], "lon": coord(rec.get("lon"), 180), "lat": coord(rec.get("lat"), 90),
                "online": online, "age": round(now - rep["at"]) if rep else None, "p_min": p_min, "p_max": p_max,
                **{name: data.get(name) for name in ("pressure_in", "pressure_out", "flow", "power_kw", "energy_kwh",
                                                     "level")},
                "pumps": [dict(p) for p in data.get("pumps", [])], "door": data.get("door"),
                **{name: data.get(name) for name in ("kwh_per_m3", "night_min_flow", "night_baseline", "source_error")},
                "refused": refused, "alerts": alerts}

    def state(self) -> dict:
        with self._lock:
            now = self.core.now()
            sites = [self._station_state(rec, now) for _, rec in sorted(self._known().items())]
            return {"sites": sites, "alerts": sum(len(s["alerts"]) for s in sites)}

    def summary(self) -> dict:
        try:
            st = self.state()
        except Exception:
            return {"devices": 0, "online": 0, "alerts": 0}
        return {"devices": len(st["sites"]), "online": sum(1 for s in st["sites"] if s["online"]),
                "alerts": st["alerts"]}

    # ---- the people: they look, nothing more
    def routes(self):
        router = APIRouter()
        viewer = self.core.viewer if self.core is not None and self.core.viewer is not None else _forbidden

        @router.get("/state")
        async def get_state(who: str = Depends(viewer)):
            return self.state()

        return router
