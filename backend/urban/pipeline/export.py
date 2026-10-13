"""SUMO network -> roads, stops, signalised junctions, virtual cameras and scene metadata."""
import html
import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict

from .. import config as c
from .paths import NET, STOPS, dump

MIN_GREEN_PHASE = 10  # seconds: shorter phases are transitions, not something to rebalance
SENSOR_ZONE = 100.0   # metres of an approach a queue sensor covers
LANE_WIDTH = 3.5      # metres
PED_START, PED_SPEED = 4.0, 1.2  # seconds before pedestrians step off, and their speed in m/s
PLAN_STEPS = (5, 10, 15)  # seconds of green an approved plan may move between two phases


def min_green(phase: dict, phases: list[dict], lanes: dict[str, int]) -> int:
    """Green a phase must keep: pedestrians walking alongside it cross the roads that are stopped.

    Road width is estimated from the lane count (both directions). In a deployment this number
    comes from the traffic engineer, not from a formula.
    """
    stopped = [lanes[e] for other in phases if other is not phase for e in other["edges"]]
    width = 2 * max(stopped, default=1) * LANE_WIDTH
    return max(MIN_GREEN_PHASE, math.ceil(PED_START + width / PED_SPEED))


def build_plans(phases: list[dict]) -> list[dict]:
    """The plan table of a junction: the base plan and every shift of green between two phases
    that keeps the cycle length and the minimum green of the donor."""
    base = {p["index"]: p["duration"] for p in phases}
    plans = [{"id": 0, "splits": base}]
    for give in phases:
        for take in phases:
            if give is take:
                continue
            for step in PLAN_STEPS:
                if take["duration"] - step < take["min_green"]:
                    continue
                plans.append({"id": len(plans), "give": give["index"], "take": take["index"], "shift": step,
                              "splits": {**base, give["index"]: give["duration"] + step,
                                         take["index"]: take["duration"] - step}})
    return plans
CAMERAS = 8
CAMERA_SPACING = 380  # metres between cameras, so they spread over the district


def signal_plans(net, ll):
    """Every signalised junction with its approaches and green phases (which approaches each one serves).

    This is the signal plan a real integration would get from the road authority.
    """
    out = []
    for tls in net.getTrafficLights():
        links, edges = defaultdict(list), {}
        for in_lane, _out_lane, index in tls.getConnections():
            edge = in_lane.getEdge()
            links[edge.getID()].append(index)
            edges[edge.getID()] = edge
        approaches = [e for e in edges.values() if e.getLength() > 25 and e.allows("passenger")]
        if not approaches:
            continue

        phases = []
        for i, ph in enumerate(next(iter(tls.getPrograms().values())).getPhases()):
            if "y" in ph.state or ph.duration < MIN_GREEN_PHASE or not set(ph.state) & set("Gg"):
                continue
            served = [e.getID() for e in approaches
                      if 2 * sum(ph.state[j] in "Gg" for j in links[e.getID()] if j < len(ph.state)) >= len(links[e.getID()])]
            phases.append({"index": i, "duration": ph.duration, "edges": served})
        # an approach that is green in every phase conflicts with nobody: nothing to rebalance for it
        always = set.intersection(*(set(p["edges"]) for p in phases)) if phases else set()
        for p in phases:
            p["edges"] = [e for e in p["edges"] if e not in always]
            p["streets"] = [n for n in dict.fromkeys(html.unescape(edges[e].getName() or "") for e in p["edges"]) if n]
        phases = [p for p in phases if p["edges"]]
        lanes = {e.getID(): e.getLaneNumber() for e in approaches}
        for p in phases:
            p["min_green"] = min_green(p, phases, lanes)

        xs = [e.getToNode().getCoord()[0] for e in approaches]
        ys = [e.getToNode().getCoord()[1] for e in approaches]
        x, y = sum(xs) / len(xs), sum(ys) / len(ys)
        names = [n for n in dict.fromkeys(html.unescape(e.getName() or "") for e in approaches) if n]
        out.append({
            "tls": tls.getID(), "xy": (x, y), "target": ll(x, y), "name": " / ".join(names[:2]) or "перекрёсток",
            "approaches": [{"id": e.getID(), "name": html.unescape(e.getName() or ""),
                            "zone": round(min(SENSOR_ZONE, e.getLength()), 1)} for e in approaches],
            "phases": phases, "plans": build_plans(phases), "lanes": sum(e.getLaneNumber() for e in approaches),
            "main": max(approaches, key=lambda e: e.getLaneNumber()),
        })
    return out


def export_network():
    import sumolib
    from sumolib.geomhelper import positionAtShapeOffset

    net = sumolib.net.readNet(str(NET), withPrograms=True)

    def ll(x, y):
        lon, lat = net.convertXY2LonLat(x, y)
        return [round(lon, 6), round(lat, 6)]

    roads = [{
        "type": "Feature",
        "properties": {"id": e.getID(), "name": html.unescape(e.getName() or ""), "speed": round(e.getSpeed() * 3.6),
                       "lanes": e.getLaneNumber(), "bus": e.allows("bus"),
                       "cls": e.getType().split(".")[-1]},
        "geometry": {"type": "LineString", "coordinates": [ll(x, y) for x, y in e.getShape()]},
    } for e in net.getEdges()]

    stops = []
    for s in ET.parse(STOPS).getroot().iter("busStop"):
        lane = net.getLane(s.get("lane"))
        pos = (float(s.get("startPos")) + float(s.get("endPos"))) / 2
        x, y = positionAtShapeOffset(lane.getShape(), pos)
        stops.append({"type": "Feature", "properties": {"id": s.get("id"), "name": s.get("name", "")},
                      "geometry": {"type": "Point", "coordinates": ll(x, y)}})
    dump("roads.geojson", roads)
    dump("stops.geojson", stops)

    # the scene is a disc inscribed in the bbox: the web layer fades everything out towards its rim
    w, s_, e, n = (float(v) for v in c.BBOX.split(","))
    lat = (s_ + n) / 2
    radius = min((e - w) * 111320 * math.cos(math.radians(lat)), (n - s_) * 111320) / 2
    (c.OUT / "meta.json").write_text(json.dumps({"center": [(w + e) / 2, lat], "radius": round(radius)}))

    # signal plans for every junction; virtual cameras on the busiest ones, spaced apart
    plans = [p for p in signal_plans(net, ll) if len(p["phases"]) >= 2]
    cx, cy = net.convertLonLat2XY((w + e) / 2, lat)
    cams = []
    for p in sorted(plans, key=lambda p: -p["lanes"]):
        x, y = p["xy"]
        if len(cams) >= CAMERAS or math.hypot(x - cx, y - cy) > radius * 0.72 \
                or any(math.hypot(x - q["xy"][0], y - q["xy"][1]) < CAMERA_SPACING for q in cams):
            continue
        cams.append(p)

    def public(p, ident, camera):
        return {"id": ident, "camera": camera, "name": p["name"], "target": p["target"], "tls": p["tls"],
                "approaches": p["approaches"], "phases": p["phases"], "plans": p["plans"]}

    features = []
    for i, p in enumerate(cams, 1):
        (ax, ay), (bx, by) = p["main"].getShape()[-2:]
        d = math.hypot(bx - ax, by - ay) or 1
        ux, uy = (bx - ax) / d, (by - ay) / d
        features.append({
            "type": "Feature",
            "properties": {**public(p, f"CAM-{i:02d}", True), "bearing": round(math.degrees(math.atan2(ux, uy)) % 360)},
            "geometry": {"type": "Point", "coordinates": ll(bx - ux * 35, by - uy * 35)},
        })
    dump("cameras.geojson", features)

    # all signalised junctions: every one has a queue sensor, eight of them also a camera window
    with_cam = {p["tls"]: f"CAM-{i:02d}" for i, p in enumerate(cams, 1)}
    signals = [public(p, with_cam.get(p["tls"], f"J-{i:02d}"), p["tls"] in with_cam) for i, p in enumerate(plans, 1)]
    (c.OUT / "signals.json").write_text(json.dumps(signals, ensure_ascii=False), encoding="utf-8")
    print(f"   signals.json: {len(signals)}")
