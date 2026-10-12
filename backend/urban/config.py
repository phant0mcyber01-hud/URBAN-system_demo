"""Paths and settings (.env in the repo root overrides defaults)."""
import os
from pathlib import Path

import sumo

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
OUT = DATA / "out"
WEB_DIST = ROOT / "web" / "dist"

SUMO_HOME = Path(sumo.SUMO_HOME)
SUMO_BIN = SUMO_HOME / "bin"
SUMO_TOOLS = SUMO_HOME / "tools"

_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BBOX = os.getenv("BBOX", "71.770,40.372,71.800,40.398")  # west,south,east,north
SIM_SPEEDUP = float(os.getenv("SIM_SPEEDUP", "1"))
SIM_END = int(os.getenv("SIM_END", "3600"))
CAR_PERIOD = float(os.getenv("CAR_PERIOD", "0.6"))
BUS_PERIOD = int(os.getenv("BUS_PERIOD", "120"))
INCIDENT_AT = int(os.getenv("INCIDENT_AT", "120"))
INCIDENT_DURATION = int(os.getenv("INCIDENT_DURATION", "240"))
PORT = int(os.getenv("PORT", "8080"))


def sumo_env():
    env = dict(os.environ)
    env["SUMO_HOME"] = str(SUMO_HOME)
    env["PATH"] = str(SUMO_BIN) + os.pathsep + env.get("PATH", "")
    return env
