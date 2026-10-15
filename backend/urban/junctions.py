"""Junction passports: approaches, phases and approved plans.

In the real system (data/city) each passport is what the traffic engineer checked and approved, and what
is loaded into the controller; until it is approved the junction is watched but never commanded.
In the demo twin (data/out) the passports are synthetic and count as approved.
The platform, the gateway and the equipment side each read them independently.
"""
import json
from pathlib import Path

from . import config as c

APPROVED, DRAFT = "approved", "draft"


def _junction(p: dict, default: str = APPROVED) -> dict:
    """default: the status of a passport the file does not state (real city: draft, demo twin: approved)."""
    return {"id": p["id"], "camera": bool(p.get("camera", True)), "lon": p["target"][0], "lat": p["target"][1],
            "tls": p.get("tls") or p["id"], "name": p["name"], "approaches": p.get("approaches", []),
            "phases": p.get("phases", []), "plans": p.get("plans", []),
            "passport": p.get("passport") or {"status": default}}


def approved(j: dict) -> bool:
    """Descriptions made in code (tests, experiments) carry no passport and count as approved."""
    return j.get("passport", {"status": APPROVED}).get("status") == APPROVED


def _default(folder: Path) -> str:
    return DRAFT if folder.resolve() == c.CITY.resolve() else APPROVED


def load_cameras(folder: Path | None = None) -> list[dict]:
    """Junctions that have a camera window in the demo twin."""
    folder = folder or c.MAP
    path = folder / "cameras.geojson"
    if not path.exists():
        return []
    return [_junction(f["properties"], _default(folder)) for f in json.loads(path.read_text(encoding="utf-8"))["features"]]


def load_all_junctions(folder: Path | None = None) -> list[dict]:
    """Every signalised junction: of the real city by default, of the demo twin with folder=config.OUT."""
    folder = folder or c.MAP
    path = folder / "signals.json"
    return [_junction(p, _default(folder)) for p in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


def plan_tables(junctions: list[dict]) -> dict[str, dict[int, dict[int, float]]]:
    """tls id -> plan id -> {phase index: green seconds}: what is loaded into the controllers.
    Only approved passports count: a draft gives the platform nothing to propose and the unit nothing to obey."""
    return {j["tls"]: {pl["id"]: {int(k): v for k, v in pl["splits"].items()} for pl in j["plans"]}
            for j in junctions if j.get("plans") and approved(j)}


def approved_plans(junctions: list[dict]) -> dict[str, set[int]]:
    """tls id -> ids of the approved plans (the base plan included)."""
    return {tls: set(table) for tls, table in plan_tables(junctions).items()}
