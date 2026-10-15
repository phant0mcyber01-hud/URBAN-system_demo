"""SUMO (headless) as a stand-in for city data sources.

Emits one raw SourceFrame per simulated second: vehicle observations, events from
city services (a scripted incident and the units dispatched to it) and utility
telemetry. No analysis happens here; that is the platform's job.
"""
import html
import json
import math
import random
import time
from functools import lru_cache

import sumolib
import traci
import traci.constants as tc

from .. import config as c
from ..core.events import Vehicle
from ..equipment.edge import EdgeController
from ..equipment.sumo_adapter import SumoAdapter
from ..junctions import load_all_junctions, plan_tables
from .field import ControllerBank
from .measure import queue_lengths
from .telemetry import Telemetry

UNITS = [  # (unit label, OSM amenity kinds used as base, fallback base label)
    ("Скорая", ("hospital", "clinic"), "Подстанция скорой (условная)"),
    ("ДПС", ("police",), "Пост ДПС (условный)"),
]
RU = {"hospital": "Больница", "clinic": "Клиника", "police": "Отдел полиции"}


@lru_cache(maxsize=1)
def network():
    return sumolib.net.readNet(str(c.DATA / "net.net.xml"))


def load_bases(net, kinds):
    """Real OSM facilities of the given kinds -> [(name, edge id)] on the drivable network."""
    out = []
    path = c.OUT / "pois.geojson"
    if path.exists():
        for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
            if f["properties"]["kind"] not in kinds:
                continue
            x, y = net.convertLonLat2XY(*f["geometry"]["coordinates"])
            near = [(d, e) for e, d in net.getNeighboringEdges(x, y, 250) if e.allows("emergency")]
            if near:
                out.append((f["properties"]["name"] or RU[f["properties"]["kind"]],
                            min(near, key=lambda t: t[0])[1].getID()))
    return out


def run(publish, commands=None, *, field=None, seed=42, incident_at=None, realtime=True, until=None, sumo_args=()):
    """Run one pass of the scenario, calling publish(frame) every simulated second.

    Two ways to connect URBAN Edge to this street:
    - field: a sources.field.FieldEmulator. Edge units run as a separate process, read the queue
      sensors and drive the controllers through it; nothing about queues or signals is in the frames.
    - commands (no field): an Edge embedded in this process, fed by a queue.Queue of gateway
      messages. Used by experiments and tests, where one deterministic process is what is wanted.
    seed: SUMO random seed (driver behaviour, insertion); incident_at: second of the scripted
    incident (default from config, math.inf disables it); realtime: pace to the wall clock;
    until: stop at this simulated second; sumo_args: extra command-line options for SUMO.
    """
    incident_at = c.INCIDENT_AT if incident_at is None else incident_at
    net = network()
    meta = json.loads((c.OUT / "meta.json").read_text())
    cx, cy = net.convertLonLat2XY(*meta["center"])
    radius = meta["radius"]
    telemetry = Telemetry(meta["center"], radius)
    rnd = random.Random(7)

    bases = {}
    for unit, kinds, fallback in UNITS:
        bases[unit] = load_bases(net, kinds) or [
            (fallback, e.getID()) for e in rnd.sample([e for e in net.getEdges() if e.allows("emergency")], 3)]

    traci.start([str(c.SUMO_BIN / "sumo"), "-c", str(c.DATA / "demo.sumocfg"), "--seed", str(seed), *sumo_args])
    traci.vehicletype.copy("DEFAULT_VEHTYPE", "emg")
    traci.vehicletype.setVehicleClass("emg", "emergency")
    traci.vehicletype.setSpeedFactor("emg", 1.5)

    junctions = load_all_junctions()
    controllers = SumoAdapter(plan_tables(junctions))
    signals = EdgeController(controllers) if field is None else None  # embedded Edge
    bank = ControllerBank(controllers)                                 # controllers behind the field link
    first = True
    zones = {a["id"]: a["zone"] for j in junctions for a in j["approaches"] if "zone" in a}
    lane_length, lengths = {}, {}
    kinds = {}
    bus_edges = set()
    incident = None   # dict while active/cleared
    responders = {}   # vehicle id -> dispatch event
    try:
        while True:
            t0 = time.time()
            traci.simulationStep()
            now = int(traci.simulation.getTime())
            events = []
            if signals is not None:
                while commands is not None and not commands.empty():
                    events += signals.handle(commands.get_nowait(), now)
                events += signals.tick(now)

            for vid in traci.simulation.getDepartedIDList():
                cls = traci.vehicle.getVehicleClass(vid)
                kinds[vid] = "bus" if cls == "bus" else "emg" if cls == "emergency" else "car"
                lengths[vid] = traci.vehicle.getLength(vid)
                traci.vehicle.subscribe(vid, [tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_ROAD_ID,
                                              tc.VAR_LANE_ID, tc.VAR_LANEPOSITION])
                if kinds[vid] == "emg":
                    traci.vehicle.setSpeedMode(vid, 7)  # may pass red lights, still keeps safe gaps
            for vid in traci.simulation.getArrivedIDList():
                kinds.pop(vid, None)
                lengths.pop(vid, None)

            vehicles, bus_loss, where, samples = [], [], {}, []
            for vid, d in traci.vehicle.getAllSubscriptionResults().items():
                if not d:
                    continue
                x, y = d[tc.VAR_POSITION]
                if x < -1e6 or y < -1e6:  # SUMO's "invalid position" (e.g. vehicle being teleported)
                    continue
                lon, lat = net.convertXY2LonLat(x, y)
                kind, road = kinds.get(vid, "car"), d[tc.VAR_ROAD_ID]
                vehicles.append(Vehicle(vid, lon, lat, d[tc.VAR_ANGLE], d[tc.VAR_SPEED], kind, road))
                where[vid] = (x, y, road)
                lane = d[tc.VAR_LANE_ID]
                if road in zones:  # what the queue sensor of this approach sees
                    if lane not in lane_length:
                        lane_length[lane] = traci.lane.getLength(lane)
                    samples.append((lane, lane_length[lane] - d[tc.VAR_LANEPOSITION], d[tc.VAR_SPEED],
                                    lengths.get(vid, 5.0)))
                if kind == "bus":
                    bus_loss.append(traci.vehicle.getTimeLoss(vid))
                    bus_edges.add(road)

            if incident is None and now >= incident_at:
                # a moving car well inside the scene, preferably on a street used by buses
                cars = [v.id for v in vehicles if v.kind == "car" and v.speed > 3 and not v.edge.startswith(":")
                        and math.hypot(where[v.id][0] - cx, where[v.id][1] - cy) < radius * 0.55]
                pool = [x for x in cars if where[x][2] in bus_edges] or cars
                if pool:
                    vid = rnd.choice(pool)
                    x, y, edge = where[vid]
                    traci.vehicle.setLaneChangeMode(vid, 0)
                    traci.vehicle.setSpeed(vid, 0)
                    lon, lat = net.convertXY2LonLat(x, y)
                    incident = {"id": f"inc-{now}", "type": "incident", "status": "active", "t": now,
                                "lon": round(lon, 6), "lat": round(lat, 6), "edge": edge,
                                "street": html.unescape(net.getEdge(edge).getName() or ""),
                                "vid": vid, "xy": (x, y), "arrived": None}
                    events.append(public(incident))
                    for unit, _, _ in UNITS:
                        best = None
                        for name, base in bases[unit]:
                            try:
                                r = traci.simulation.findRoute(base, edge, vType="emg")
                            except traci.TraCIException:
                                continue
                            if r.edges and (best is None or r.travelTime < best[1].travelTime):
                                best = (name, r)
                        if best:
                            rid = f"emg-{unit}-{now}"
                            traci.route.add(rid, best[1].edges)
                            traci.vehicle.add(rid, rid, typeID="emg", departLane="best")
                            responders[rid] = {"id": rid, "type": "dispatch", "status": "en_route", "t": now,
                                               "unit": unit, "from": best[0], "eta": round(best[1].travelTime),
                                               "ref": incident["id"]}
                            # the route goes to the platform once, for the green corridor (an AVL feed on the street)
                            events.append({**responders[rid], "route": list(best[1].edges)})
            elif incident and incident["status"] == "active":
                for rid, ev in responders.items():
                    if ev["status"] == "en_route" and rid in where and \
                            math.hypot(where[rid][0] - incident["xy"][0], where[rid][1] - incident["xy"][1]) < 45:
                        ev.update(status="arrived", took=now - ev["t"], t=now)
                        traci.vehicle.setSpeed(rid, 0)
                        incident["arrived"] = incident["arrived"] or now
                        events.append(dict(ev))
                # cleared 60 s after the first responder arrives, or by timeout
                if (incident["arrived"] and now >= incident["arrived"] + 60) or now >= incident["t"] + c.INCIDENT_DURATION:
                    for vid in [incident["vid"], *responders]:
                        if vid in kinds:
                            traci.vehicle.setSpeed(vid, -1)
                    if incident["vid"] in kinds:
                        traci.vehicle.setLaneChangeMode(incident["vid"], 1621)
                    incident["status"] = "cleared"
                    events.append({**public(incident), "t": now, "duration": now - incident["t"]})

            frame = {"t": now, "vehicles": vehicles, "bus_loss": bus_loss, "events": events}
            measured = queue_lengths(samples, zones)
            if field is None:
                frame["queues"] = measured
            else:  # each unit sees only the approaches of its own junction
                field.step(bank, now, {j["tls"]: {a["id"]: measured.get(a["id"], 0.0) for a in j["approaches"]}
                                       for j in junctions}, reset=first)
                first = False
            if now % 5 == 0:
                frame["telemetry"] = telemetry.sample(now)
            publish(frame)

            if traci.simulation.getMinExpectedNumber() <= 0 or (until is not None and now >= until):
                break
            if realtime:
                time.sleep(max(0.0, 1.0 / c.SIM_SPEEDUP - (time.time() - t0)))
    finally:
        traci.close()


def public(incident):
    return {k: v for k, v in incident.items() if k not in ("vid", "xy", "arrived")}
