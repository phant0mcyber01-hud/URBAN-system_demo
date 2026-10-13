"""Platform core: raw source frames in, analysed client-ready frames out.

The platform detects problems and proposes actions. It never applies anything by itself:
actions go through the Command Gateway, which needs an operator's decision.
"""
import math

from .analytics import traffic
from .analytics.infra import Detector
from .analytics.queues import RELIEVED, WINDOW, QueueMonitor
from .gateway import CommandGateway
from .junctions import approved_plans

SETTLE = 2 * WINDOW  # seconds after a plan change before judging it: a controller needs a cycle to switch
STALE = 5            # seconds without a report before a junction counts as "no data"

M_LAT = 111320.0


class Platform:
    def __init__(self, center, radius, cameras=(), send=lambda msg: None, reachable=lambda tls: True):
        """center: [lon, lat], radius: metres,
        cameras: junctions with a queue sensor, as loaded by urban.junctions
                 (camera: True when the junction also has a camera window),
        send: delivers a gateway message to the (emulated) equipment."""
        self.center, self.radius = center, radius
        self.m_lon = M_LAT * math.cos(math.radians(center[1]))
        self.cameras = list(cameras)
        self.camera_xy = [(c["id"], *self.local(c["lon"], c["lat"])) for c in self.cameras]
        self.gateway = CommandGateway(send, approved_plans(self.cameras), reachable)
        self._by_tls = {c["tls"]: c for c in self.cameras}
        self.sensors: dict[str, tuple[int, dict]] = {}  # junction id -> (report time, queues) from its Edge unit
        self.queues, self.last_cmd_version, self._logged = {}, -1, 0
        self._reset()
        self.t = -1

    def _reset(self):
        self.detector = Detector()
        self.events, self.shadow, self.alerts, self.infra = [], [], [], []
        self.gateway.reset()
        self.monitor = QueueMonitor(self.cameras)

    def local(self, lon, lat):
        """lon/lat -> metres east/north of the scene centre."""
        return (lon - self.center[0]) * self.m_lon, (lat - self.center[1]) * M_LAT

    def handle(self, raw: dict) -> dict:
        frame = {"t": raw["t"]}
        if raw["t"] < self.t:  # the scenario restarted
            self._reset()
            self.sensors.clear()
            frame["reset"] = True
        self.t = raw["t"]
        t = raw["t"]

        # vehicles: local decimetres, only what the scene shows
        limit = self.radius * 1.03
        out, points = [], []
        for v in raw["vehicles"]:
            x, y = self.local(v.lon, v.lat)
            points.append((x, y, v.speed))
            if math.hypot(x, y) <= limit:
                out.append([v.id, round(x * 10), round(y * 10), round(v.angle), round(v.speed, 1), v.kind])
        frame["v"] = out
        frame["jam"] = traffic.jams(raw["vehicles"])
        frame["stats"] = traffic.stats(raw["vehicles"], raw.get("bus_loss", []))

        # sensors: flow counters and queue length per approach (aggregates only)
        if "queues" in raw:  # an Edge embedded in the source: readings arrive with the frame
            readings, missing = raw["queues"], set()
        else:                # separate Edge units: use what they reported, and only while it is fresh
            readings, missing = {}, set()
            for cam in self.cameras:
                at, reported = self.sensors.get(cam["id"], (None, None))
                if at is None or not 0 <= t - at <= STALE:
                    missing.add(cam["id"])
                else:
                    readings.update(reported)
        queues, problems = self.monitor.update(t, readings, missing)
        self.queues = queues
        counts = traffic.camera_counts(points, self.camera_xy)
        frame["cams"] = {cid: [*counts[cid], None if queues[cid] is None else round(max(queues[cid].values(), default=0))]
                         for cid in counts}

        shadow = []
        frame["events"] = []
        for ev in raw.get("events", []):
            if ev["type"] == "tls":  # equipment report, not a city event
                self.gateway.on_equipment(ev, t)
                continue
            self.events.append(ev)
            frame["events"].append(ev)
            shadow += self._respond(ev)

        # one direction is overloaded while another has green to spare -> propose an approved plan
        self.gateway.keep_muted({pr["tls"] for pr in problems})
        for pr in problems:
            self.gateway.propose(
                t, "tls.select_plan", pr,
                f"{'камера' if pr['camera'] else 'детектор'} {pr['cam']}: "
                f"очередь в среднем {pr['queue']} м, у поперечного направления {pr['other']} м")
        demand = {(cam["tls"], phase): value
                  for cam in self.cameras for phase, value in self.monitor.demands.get(cam["id"], {}).items()}
        self.gateway.tick(t, demand)
        # going back to the base plan needs nobody's approval: release as soon as it stops helping
        for p in list(self.gateway.proposals.values()):
            if p["status"] != "active" or t - p.get("applied_at", t) < SETTLE:
                continue
            loaded, donor = demand.get((p["tls"], p["give"])), demand.get((p["tls"], p["take"]))
            if loaded is None or donor is None:
                continue
            if loaded < RELIEVED:
                self.gateway.release(p["id"], "relieved", t)
            elif donor >= loaded:
                self.gateway.release(p["id"], "balance", t)

        if (samples := raw.get("telemetry")) is not None:
            self.infra, alerts = self.detector.update(t, samples)
            frame["infra"] = self.infra
            for a in alerts:
                self.alerts.append(a)
                if a["level"] == "warn":
                    shadow.append(self.gateway.propose_shadow(
                        a["t"], a["id"], "workorder.create", a["asset"],
                        "плановая диагностика до достижения аварийного порога"))
            if alerts:
                frame["alerts"] = alerts
        if shadow:
            self.shadow += shadow
            frame["shadow"] = shadow
        frame["audit"] = self.audit()
        if (new := [r for r in self.gateway.records if r["seq"] > self._logged]):
            self._logged = new[-1]["seq"]
            frame["log"] = new
        frame["link"] = self.gateway.link_up
        if self.gateway.version != self.last_cmd_version or any(
                p["status"] in ("pending", "approved", "active") for p in self.gateway.proposals.values()):
            self.last_cmd_version = self.gateway.version
            frame["cmd"] = self.gateway.public()
        return frame

    def on_report(self, tls: str, report: dict):
        """A report from the Edge unit of a junction: queue readings and what happened to its leases."""
        cam = self._by_tls.get(tls)
        if cam is None:
            return
        self.sensors[cam["id"]] = (report.get("t", self.t), report.get("queues", {}))
        for ev in report.get("events", []):
            if ev.get("type") == "tls":
                self.gateway.on_equipment(ev, self.t)
            elif ev.get("type") == "edge":
                self.gateway.audit.append(self.t, "device-refused", "-", "-", str(ev.get("device")),
                                          str(ev.get("reason")), actor="equipment", mode="approval")

    def _respond(self, ev: dict) -> list[dict]:
        street = ev.get("street") or ev.get("edge", "")
        if ev["type"] == "dispatch" and ev["status"] == "en_route":
            return [self.gateway.propose_shadow(ev["t"], ev["ref"], "tls.preempt",
                                                f"маршрут: {ev['unit']} от «{ev['from']}»",
                                                "зелёный коридор по маршруту следования")]
        return []

    def audit(self) -> dict:
        return {"count": self.gateway.count, "head": self.gateway.head, "applied": self.gateway.applied}

    def snapshot(self) -> dict:
        return {"t": self.t, "events": self.events[-100:], "shadow": self.shadow[-100:],
                "alerts": self.alerts[-100:], "infra": self.infra, "audit": self.audit(),
                "cmd": self.gateway.public(), "link": self.gateway.link_up,
                "log": self.gateway.records[-20:]}
