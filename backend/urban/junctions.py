"""Junction descriptions produced by the data pipeline: approaches, phases and approved plans.

In a deployment this is the signal passport of each junction: what the traffic engineer
approved and loaded into the controller. The platform, the gateway and the equipment side
each read it independently.
"""
import json

from . import config as c


def _junction(p: dict) -> dict:
    return {"id": p["id"], "camera": bool(p.get("camera", True)), "lon": p["target"][0], "lat": p["target"][1],
            "tls": p.get("tls"), "name": p["name"], "approaches": p.get("approaches", []),
            "phases": p.get("phases", []), "plans": p.get("plans", [])}


def load_cameras() -> list[dict]:
    """Junctions that have a camera window in the UI."""
    path = c.OUT / "cameras.geojson"
    if not path.exists():
        return []
    return [_junction(f["properties"]) for f in json.loads(path.read_text(encoding="utf-8"))["features"]]


def load_all_junctions() -> list[dict]:
    """Every signalised junction with a queue sensor."""
    path = c.OUT / "signals.json"
    return [_junction(p) for p in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []


def plan_tables(junctions: list[dict]) -> dict[str, dict[int, dict[int, float]]]:
    """tls id -> plan id -> {phase index: green seconds}: what is loaded into the controllers."""
    return {j["tls"]: {pl["id"]: {int(k): v for k, v in pl["splits"].items()} for pl in j["plans"]}
            for j in junctions if j.get("plans")}


def approved_plans(junctions: list[dict]) -> dict[str, set[int]]:
    """tls id -> ids of the approved plans (the base plan included)."""
    return {tls: set(table) for tls, table in plan_tables(junctions).items()}
