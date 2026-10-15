"""The unit's cameras: where each one streams from, which approach it watches and its calibration.

/etc/urban/cameras.json, written by the setup page on the unit (python -m urban.vision setup):
    [{"camera": "cam-1", "source": "rtsp://${CAM1_USER}:${CAM1_PASS}@10.0.0.11:554/stream1",
      "approach": "J-07-N", "lanes": 2, "lane_width": 3.5, "zone": 100,
      "points": [{"px": [u, v], "m": [x, y]}, ... four ...]}]
x is metres from the stop line up the approach, y metres across it from the kerb. A camera without
its four points is not calibrated yet and measures nothing.
"""
import json
import os
from pathlib import Path

from ..sensors.geometry import Homography, View


def load(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: a list of cameras is expected")
    return data


def save(path: Path, cameras: list[dict]):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cameras, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def calibrated(cam: dict) -> bool:
    return bool(cam.get("approach")) and len(cam.get("points") or []) == 4


def view(cam: dict) -> View:
    return View(cam["approach"], cam["points"], int(cam.get("lanes", 2)), float(cam.get("lane_width", 3.5)),
                float(cam.get("zone", 100.0)))


def back(cam: dict) -> Homography:
    """Road metres -> pixels, to draw the lanes and the zone over a frame."""
    return Homography([((p["m"][0], p["m"][1]), (p["px"][0], p["px"][1])) for p in cam["points"]])
