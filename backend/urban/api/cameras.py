"""Loads the camera definitions produced by the data pipeline."""
import json

from .. import config as c


def _camera(p: dict) -> dict:
    return {"id": p["id"], "camera": bool(p.get("camera", True)), "lon": p["target"][0], "lat": p["target"][1],
            "tls": p.get("tls"), "name": p["name"], "approaches": p.get("approaches", []), "phases": p.get("phases", [])}


def load_cameras() -> list[dict]:
    """The cameras shown in the UI."""
    path = c.OUT / "cameras.geojson"
    if not path.exists():
        return []
    return [_camera(f["properties"]) for f in json.loads(path.read_text(encoding="utf-8"))["features"]]


def load_all_junctions() -> list[dict]:
    """Every signalised junction as if it had a camera: for coverage experiments."""
    path = c.OUT / "signals.json"
    return [_camera(p) for p in json.loads(path.read_text(encoding="utf-8"))] if path.exists() else []
