"""Street lighting, the second City OS module: the centre's side. Design: docs/city-os-modules.md.

An operator sets a level for some cabinets for 1-720 minutes. Each override is a series of short leases: the module
sends `set` (ttl 120 s) at once and again every RENEW seconds while the time is not up, then sends `release` and
audits `lighting-expired`. A cabinet that loses the centre returns to its own schedule within 2 minutes. Overrides
live in memory: if the platform restarts they are gone and the cabinets are back on schedule in at most 2 minutes
(the safe side). A cabinet's report is untrusted: it is accepted only for its own cabinet, field by field, with
numbers finite and in range and words from a fixed set; anything else is dropped without a trace in the state.
"""
from __future__ import annotations

import asyncio
import math
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictInt, field_validator

from ... import config as c
from .session import run_demo_cabinets

RENEW = 40          # seconds between the renewals of a lease
LEASE_TTL = 120     # seconds each lease holds
RETRY_SEND = 5      # seconds before a send that found the cabinet offline is tried again
SILENT = 30         # seconds without a report: the cabinet counts as offline
REFUSAL_SHOWN = 300  # seconds a refused command stays an alert
MODES, DOORS, SCHEDULES = ("schedule", "override"), ("closed", "open", "unknown"), ("astro", "photocell")
REFUSAL_MAX = 120

__all__ = ["LightingModule", "run_demo_cabinets", "validate_report"]


def _num(v, lo: float, hi: float) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def _int(v, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _plain(text: str) -> str:
    """Printable ASCII and Cyrillic only: no control characters, no look-alike or invisible ones."""
    return "".join(ch for ch in text if " " <= ch <= "~" or "Ѐ" <= ch <= "ӿ")


def validate_report(data, cabinet: str) -> dict | None:
    """The `lighting` object of a report if every field is what a cabinet sends and it is about `cabinet`, else
    None. Never raises. `refused` is optional (a short reason, cleaned)."""
    if not isinstance(data, dict) or data.get("cabinet") != cabinet:
        return None
    lamps = data.get("lamps")
    ok = (data.get("mode") in MODES and isinstance(data.get("on"), bool) and _int(data.get("level"), 0, 100)
          and _int(lamps, 0, 100_000) and _int(data.get("failed"), 0, lamps if isinstance(lamps, int) else 0)
          and _num(data.get("power_w"), 0, 10_000_000) and data.get("door") in DOORS
          and (data.get("lease_until") is None or _num(data.get("lease_until"), 0, 10 ** 11))
          and data.get("schedule") in SCHEDULES and isinstance(data.get("dark"), bool))
    if not ok:
        return None
    out = {k: data[k] for k in ("mode", "on", "level", "lamps", "failed", "door", "lease_until", "schedule", "dark")}
    out["power_w"] = round(float(data["power_w"]), 1)
    refused = data.get("refused")
    out["refused"] = _plain(refused)[:REFUSAL_MAX] or None if isinstance(refused, str) else None
    return out


class Override(BaseModel):
    cabinets: list[str] = Field(min_length=1, max_length=500)
    level: StrictInt = Field(ge=0, le=100)
    minutes: StrictInt = Field(ge=1, le=720)
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = _plain(v).strip()
        if not v:
            raise ValueError("a reason is needed")
        return v


class Release(BaseModel):
    cabinets: list[str] = Field(min_length=1, max_length=500)


def _forbidden():
    raise HTTPException(403, "no role for this")


class LightingModule:
    id = "lighting"
    title = "Уличное освещение"
    device_kind = "lighting"

    def __init__(self):
        self.core = None
        self.reports: dict[str, dict] = {}    # device id -> {"at", "data", "refusal": (text, at) | None}
        self.overrides: dict[str, dict] = {}  # cabinet -> {"level", "until", "actor", "reason", "next"}
        self._lock = threading.RLock()
        self._demo: asyncio.Task | None = None

    # ---- the core
    def attach(self, core) -> None:
        self.core = core

    async def startup(self) -> None:
        """In the demo twin: a few emulated cabinets in this process, connecting to the platform's device listener
        like real ones (the demo provisioning gives them keys). core.extra may name platform_addr, server_name,
        ca_path and pki_dir; the defaults are the platform's own settings."""
        core = self.core
        if core is None or not core.demo or self._demo is not None:
            return
        extra = core.extra or {}
        cabinets = [r for r in core.devices(self.device_kind) if r.get("status", "active") == "active"]
        if not cabinets:
            return
        pki = extra.get("pki_dir") or c.PKI
        self._demo = asyncio.ensure_future(run_demo_cabinets(
            extra.get("platform_addr") or f"127.0.0.1:{c.DEVICE_PORT}", extra.get("server_name") or c.DEVICE_NAME,
            extra.get("ca_path") or f"{pki}/ca.pem", pki, cabinets[:4]))

    def stop(self) -> None:
        """Ends the demo cabinets (the platform may call it at shutdown)."""
        if self._demo is not None:
            self._demo.cancel()
            self._demo = None

    # ---- reports from cabinets
    def on_report(self, rec, report) -> None:
        try:
            if not isinstance(rec, dict) or not isinstance(report, dict):
                return
            ident, cabinet = rec.get("id"), rec.get("cabinet")
            if not isinstance(ident, str) or not isinstance(cabinet, str):
                return
            data = validate_report(report.get("lighting"), cabinet)
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
                    self.core.audit("lighting-refused", cabinet, note=refused, actor=ident)
        except Exception:  # an untrusted report must never stop the hub
            return

    # ---- leases
    def _send(self, rec_id: str, cabinet: str, body: dict) -> bool:
        try:
            return bool(self.core.send(rec_id, {"lighting": {"cabinet": cabinet, **body}}))
        except Exception:
            return False

    def _device_of(self, cabinet: str) -> str | None:
        rec = self._known().get(cabinet)
        return rec["id"] if rec else None

    def tick(self, now: float) -> None:
        if self.core is None:
            return
        with self._lock:
            for cabinet, ov in list(self.overrides.items()):
                device = self._device_of(cabinet)
                if now >= ov["until"]:
                    del self.overrides[cabinet]
                    if device is not None:
                        self._send(device, cabinet, {"action": "release"})
                    self.core.audit("lighting-expired", cabinet, note=f"level {ov['level']}, {ov['reason']}",
                                    actor="platform")
                elif now >= ov["next"]:
                    ok = device is not None and self._send(
                        device, cabinet, {"action": "set", "level": ov["level"], "ttl": LEASE_TTL})
                    ov["next"] = now + (RENEW if ok else RETRY_SEND)

    # ---- state
    def _known(self) -> dict[str, dict]:
        """cabinet -> registry record, for the cabinets that can be commanded."""
        out = {}
        for rec in self.core.devices(self.device_kind):
            if isinstance(rec, dict) and isinstance(rec.get("cabinet"), str) and rec.get("status", "active") == "active":
                out[rec["cabinet"]] = rec
        return out

    def _cabinet_state(self, rec: dict, now: float) -> dict:
        rep = self.reports.get(rec["id"])
        online = bool(self.core.online(rec["id"])) and rep is not None and now - rep["at"] <= SILENT
        data = rep["data"] if rep else {}
        refusal = rep.get("refusal") if rep else None
        refused = refusal[0] if refusal and now - refusal[1] <= REFUSAL_SHOWN else None
        ov = self.overrides.get(rec["cabinet"])
        alerts = []
        if not online:
            alerts.append({"kind": "offline", "level": "warn", "text": "нет связи"})
        if data.get("failed", 0) > 0:
            alerts.append({"kind": "lamps", "level": "warn", "text": f"перегорели лампы: {data['failed']}"})
        if data.get("door") == "open":
            alerts.append({"kind": "door", "level": "alert", "text": "шкаф открыт"})
        if refused:
            alerts.append({"kind": "refused", "level": "warn", "text": f"шкаф отклонил команду: {refused}"})

        def coord(v):
            return float(v) if _num(v, -180, 180) else None

        return {"id": rec["id"], "cabinet": rec["cabinet"], "lon": coord(rec.get("lon")), "lat": coord(rec.get("lat")),
                "online": online, "mode": data.get("mode"), "on": data.get("on"), "level": data.get("level"),
                "lamps": data.get("lamps"), "failed": data.get("failed"), "power_w": data.get("power_w"),
                "door": data.get("door"), "lease_until": data.get("lease_until"), "schedule": data.get("schedule"),
                "dark": data.get("dark"), "age": round(now - rep["at"]) if rep else None, "refused": refused,
                "alerts": alerts,
                "override": {k: ov[k] for k in ("level", "until", "actor", "reason")} if ov else None}

    def state(self) -> dict:
        with self._lock:
            now = self.core.now()
            cabinets = [self._cabinet_state(rec, now) for _, rec in sorted(self._known().items())]
            return {"cabinets": cabinets, "alerts": sum(len(cab["alerts"]) for cab in cabinets)}

    def summary(self) -> dict:
        try:
            st = self.state()
        except Exception:
            return {"devices": 0, "online": 0, "alerts": 0, "overrides": 0}
        return {"devices": len(st["cabinets"]), "online": sum(1 for cab in st["cabinets"] if cab["online"]),
                "alerts": st["alerts"], "overrides": len(self.overrides)}

    # ---- the people
    def _targets(self, cabinets: list[str]) -> list[tuple[str, dict]]:
        known = self._known()
        wanted = list(dict.fromkeys(cabinets))  # once each, in the order given
        unknown = [x for x in wanted if x not in known]
        if unknown:
            raise HTTPException(400, "unknown cabinet: " + ", ".join(_plain(x)[:40] for x in unknown[:5]))
        return [(x, known[x]) for x in wanted]

    def override(self, who: str, body: Override) -> dict:
        with self._lock:
            targets = self._targets(body.cabinets)
            now = self.core.now()
            for cabinet, rec in targets:
                ok = self._send(rec["id"], cabinet, {"action": "set", "level": body.level, "ttl": LEASE_TTL})
                self.overrides[cabinet] = {"level": body.level, "until": now + body.minutes * 60, "actor": who,
                                           "reason": body.reason, "next": now + (RENEW if ok else RETRY_SEND)}
                self.core.audit("lighting-override", cabinet,
                                note=f"level {body.level}, {body.minutes} min, {body.reason}", actor=who)
            return self.state()

    def release(self, who: str, body: Release) -> dict:
        with self._lock:
            targets = self._targets(body.cabinets)
            for cabinet, rec in targets:
                self.overrides.pop(cabinet, None)
                self._send(rec["id"], cabinet, {"action": "release"})
                self.core.audit("lighting-release", cabinet, note="released", actor=who)
            return self.state()

    def routes(self):
        router = APIRouter()
        viewer = self.core.viewer if self.core is not None and self.core.viewer is not None else _forbidden
        operator = self.core.operator if self.core is not None and self.core.operator is not None else _forbidden

        @router.get("/state")
        async def get_state(who: str = Depends(viewer)):
            return self.state()

        @router.post("/override")
        async def post_override(body: Override, who: str = Depends(operator)):
            return self.override(who, body)

        @router.post("/release")
        async def post_release(body: Release, who: str = Depends(operator)):
            return self.release(who, body)

        return router
