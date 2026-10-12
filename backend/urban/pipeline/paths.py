"""File locations and small helpers shared by the pipeline steps."""
import json
import subprocess

from .. import config as c

OSM = c.DATA / "fergana.osm"
NET = c.DATA / "net.net.xml"
STOPS = c.DATA / "stops.add.xml"
PTLINES = c.DATA / "ptlines.xml"
CARS = c.DATA / "cars.rou.xml"
PT = c.DATA / "pt.rou.xml"
CFG = c.DATA / "demo.sumocfg"


def run(cmd, check=True):
    print(">>", " ".join(str(x) for x in cmd[:3]), "...")
    return subprocess.run([str(x) for x in cmd], env=c.sumo_env(), check=check, cwd=c.DATA)


def dump(name, feats):
    with open(c.OUT / name, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"   {name}: {len(feats)}")
