"""Loads the camera definitions produced by the data pipeline."""
import json

from .. import config as c


def load_cameras() -> list[dict]:
    path = c.OUT / "cameras.geojson"
    if not path.exists():
        return []
    out = []
    for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
        p = f["properties"]
        out.append({"id": p["id"], "lon": p["target"][0], "lat": p["target"][1], "tls": p.get("tls"),
                    "name": p["name"], "approaches": p.get("approaches", [])})
    return out
