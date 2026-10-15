"""Junction passports of the real city: what the engineer checked against the controller and approved.

A passport is a draft until approved. Any edit makes it a draft again with a new version, so what runs
is always exactly what somebody approved; the hash of the approved content goes into the audit chain.
The plan table is never typed in: it is derived from the phases (shifts of 5/10/15 s that keep the cycle
and every minimum green), the same rule the units and the gateway check against.
"""
import copy
import hashlib
import json
import os
import time
from pathlib import Path

from ..pipeline.export import build_plans
from .build import LOST

APPROVED, DRAFT = "approved", "draft"
LIMITS = {"duration": (5, 120), "min_green": (5, 60), "zone": (10, 300), "lanes": (1, 8)}


class PassportError(ValueError):
    pass


def content_hash(j: dict) -> str:
    """Hash of what controls the junction: approaches, phases and the plan table."""
    body = {"approaches": [{k: a.get(k) for k in ("id", "zone", "lanes")} for a in j["approaches"]],
            "phases": [{k: p.get(k) for k in ("index", "duration", "min_green", "edges")} for p in j["phases"]],
            "plans": j["plans"]}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def problems(j: dict) -> list[str]:
    """What stops a passport from being approved, in words for the engineer."""
    out = []
    if len(j["phases"]) < 2:
        out.append("нужно хотя бы две фазы")
    ids = {a["id"] for a in j["approaches"]}
    for p in j["phases"]:
        if p["duration"] < p["min_green"]:
            out.append(f"фаза {p['index']}: зелёный {p['duration']} с меньше минимума {p['min_green']} с")
        if not p["edges"]:
            out.append(f"фаза {p['index']}: не обслуживает ни одного подъезда")
        if set(p["edges"]) - ids:
            out.append(f"фаза {p['index']}: неизвестный подъезд")
    served = {e for p in j["phases"] for e in p["edges"]}
    if ids - served:
        out.append("подъезды без фазы: " + ", ".join(sorted(ids - served)))
    return out


def _number(value, field: str) -> int:
    lo, hi = LIMITS[field]
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        raise PassportError(f"{field}: нужно число")
    if not lo <= v <= hi:
        raise PassportError(f"{field}: от {lo} до {hi}")
    return v


class Passports:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: list[dict] = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []

    def get(self, ident: str) -> dict | None:
        return next((j for j in self.items if j["id"] == ident), None)

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _need(self, ident: str) -> dict:
        j = self.get(ident)
        if j is None:
            raise PassportError("нет такого перекрёстка")
        return j

    def edit(self, ident: str, change: dict, by: str) -> dict:
        """change: {"name"?, "approaches": [{id, zone?, lanes?, name?}], "phases": [{index, duration?, min_green?,
        edges?}]}. Unknown ids are refused. The passport becomes a draft with the next version."""
        j = copy.deepcopy(self._need(ident))
        if "name" in change:
            name = str(change["name"]).strip()
            if not 1 <= len(name) <= 120:
                raise PassportError("название: от 1 до 120 символов")
            j["name"] = name
        approaches = {a["id"]: a for a in j["approaches"]}
        for a in change.get("approaches") or []:
            target = approaches.get(a.get("id"))
            if target is None:
                raise PassportError(f"нет подъезда {a.get('id')}")
            for f in ("zone", "lanes"):
                if f in a:
                    target[f] = _number(a[f], f)
            if "name" in a:
                target["name"] = str(a["name"]).strip()[:120]
        phases = {p["index"]: p for p in j["phases"]}
        for p in change.get("phases") or []:
            target = phases.get(p.get("index"))
            if target is None:
                raise PassportError(f"нет фазы {p.get('index')}")
            for f in ("duration", "min_green"):
                if f in p:
                    target[f] = _number(p[f], f)
            if "edges" in p:
                edges = [str(e) for e in p["edges"]]
                if set(edges) - set(approaches):
                    raise PassportError(f"фаза {target['index']}: неизвестный подъезд")
                target["edges"] = edges
                target["streets"] = [n for n in dict.fromkeys(approaches[e]["name"] for e in edges) if n]
        j["cycle"] = sum(p["duration"] for p in j["phases"]) + LOST * len(j["phases"])
        j["plans"] = json.loads(json.dumps(build_plans(j["phases"])))  # the same shape as on disk: string keys
        old = j.get("passport", {})
        j["passport"] = {"status": DRAFT, "source": old.get("source", "engineer"), "version": old.get("version", 1) + 1,
                         "edited_by": by, "edited_at": _now()}
        self.items[self.items.index(self._need(ident))] = j
        self._save()
        return j

    def approve(self, ident: str, by: str) -> dict:
        j = self._need(ident)
        p = j.get("passport", {})
        if p.get("status") == APPROVED:
            raise PassportError("паспорт уже утверждён")
        if issues := problems(j):
            raise PassportError("; ".join(issues))
        j["passport"] = {**p, "status": APPROVED, "approved_by": by, "approved_at": _now(), "hash": content_hash(j)}
        j["passport"].pop("note", None)
        self._save()
        return j

    def withdraw(self, ident: str, by: str, why: str = "") -> dict:
        """Back to draft: the junction is only watched again, its commands end."""
        j = self._need(ident)
        p = j.get("passport", {})
        if p.get("status") != APPROVED:
            raise PassportError("паспорт не утверждён")
        j["passport"] = {**{k: v for k, v in p.items() if k not in ("approved_by", "approved_at", "hash")},
                         "status": DRAFT, "withdrawn_by": by, "withdrawn_at": _now(), "note": why[:200]}
        self._save()
        return j


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
