"""Platform core: raw source frames in, analysed client-ready frames out.

The platform detects problems and proposes actions. It never applies anything by itself:
actions go through the Command Gateway, which needs an operator's decision.
"""
import math

from .analytics import traffic
from .corridor import Corridors
from .analytics.infra import Detector
from .analytics.queues import RELIEVED, WINDOW, QueueMonitor
from .devices import health
from .gateway import CommandGateway
from .junctions import approved_plans

SETTLE = 2 * WINDOW  # seconds after a plan change before judging it: a controller needs a cycle to switch
STALE = 5            # seconds without a report before a junction counts as "no data"
SAMPLE = 60          # seconds between history samples of queues and device health
LATE = 5  # seconds: a report older than this was kept through an outage; it is history, not the live reading
SPILL = 0.8          # a downstream approach queued over this share of its sensor zone counts as full

M_LAT = 111320.0


class Platform:
    def __init__(self, center, radius, cameras=(), send=lambda msg: None, reachable=lambda tls: True, store=None,
                 corridor: bool = True, spillback: bool = False):
        """center: [lon, lat], radius: metres,
        cameras: junctions with a queue sensor, as loaded by urban.junctions
                 (camera: True when the junction also has a camera window),
        send: delivers a gateway message to the (emulated) equipment,
        store: persistent history (urban.store.Store) or None to keep everything in memory."""
        self.center, self.radius = center, radius
        self.m_lon = M_LAT * math.cos(math.radians(center[1]))
        self.cameras = list(cameras)
        self.camera_xy = [(c["id"], *self.local(c["lon"], c["lat"])) for c in self.cameras]
        self.store = store
        self._saved_version, self._queue_sampled, self._dev_sampled = -1, None, None
        self.gateway = CommandGateway(send, approved_plans(self.cameras), reachable, store=store)
        self.corridors = Corridors(self.cameras)
        self.corridor_on = corridor  # propose green corridors for emergency units (config GREEN_CORRIDOR)
        self.spillback = spillback   # do not push cars into a block that is already full (config SPILLBACK)
        self._zone = {a["id"]: a.get("zone", 100.0) for c in self.cameras for a in c["approaches"]}
        self._downstream = {(c["tls"], ph["index"]): ph.get("downstream", [])
                            for c in self.cameras for ph in c.get("phases", [])}
        self._by_tls = {c["tls"]: c for c in self.cameras}
        self.sensors: dict[str, tuple[int, dict]] = {}  # junction id -> (report time, queues) from its Edge unit
        self.dev: dict[str, dict] = {}        # junction id -> brief of its Edge unit, for the UI
        self.dev_alerts: list[dict] = []      # health alerts as they appeared, for the feed and the audit log
        self._dev_state: dict[str, set] = {}  # device id -> alert kinds currently raised
        self._dev_keys: dict[str, str] = {}   # device id -> fingerprint of its enrolled key
        self.queues, self.last_cmd_version, self._logged = {}, -1, 0
        self._reset()
        self.t = -1

    def _reset(self):
        self.detector = Detector()
        self.events, self.shadow, self.alerts, self.infra = [], [], [], []
        self.gateway.reset()
        self.monitor = QueueMonitor(self.cameras)
        if hasattr(self, "corridors"):
            self.corridors.reset()

    def local(self, lon, lat):
        """lon/lat -> metres east/north of the scene centre."""
        return (lon - self.center[0]) * self.m_lon, (lat - self.center[1]) * M_LAT

    def handle(self, raw: dict) -> dict:
        frame = {"t": raw["t"]}
        if raw["t"] < self.t:  # the scenario restarted
            self._reset()
            self.sensors.clear()
            frame["reset"] = True
            if self.store is not None:
                self.store.start_run(note="scenario restarted")
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
        observed = raw.get("observed", True)  # False: no traffic source at all (field mode), nothing to count
        frame["jam"] = traffic.jams(raw["vehicles"])
        frame["stats"] = traffic.stats(raw["vehicles"], raw.get("bus_loss", [])) if observed else None

        # sensors: flow counters and queue length per approach (aggregates only)
        if "queues" in raw:  # an Edge embedded in the source: readings arrive with the frame
            readings, missing = raw["queues"], set()
        else:                # separate Edge units: use what they reported, and only while it is fresh
            readings, missing = {}, set()
            for cam in self.cameras:
                at, reported = self.sensors.get(cam["id"], (None, None))
                if at is None or abs(t - at) > STALE:  # a report may run a second ahead of the frame
                    missing.add(cam["id"])
                else:
                    readings.update(reported)
        queues, problems = self.monitor.update(t, readings, missing)
        self.queues = queues
        counts = (traffic.camera_counts(points, self.camera_xy) if observed
                  else {cid: [None, None] for cid, *_ in self.camera_xy})
        frame["cams"] = {cid: [*counts[cid], None if queues[cid] is None else round(max(queues[cid].values(), default=0))]
                         for cid in counts}

        shadow = []
        frame["events"] = []
        for ev in raw.get("events", []):
            if ev["type"] == "tls":  # equipment report, not a city event
                self.gateway.on_equipment(ev, t)
                continue
            if ev["type"] == "dispatch":
                if ev["status"] == "en_route" and ev.get("route") and self.corridor_on:
                    incident = next((e for e in reversed(self.events) if e.get("id") == ev.get("ref")), {})
                    self.corridors.open(self.gateway, ev, t, incident.get("street", ""))
                elif ev["status"] == "arrived":
                    self.corridors.close(self.gateway, ev["id"], "arrived", t)
                ev = {k: v for k, v in ev.items() if k != "route"}  # the route stays here, not in every client
            self.events.append(ev)
            frame["events"].append(ev)
            shadow += self._respond(ev)
        if self.corridors.active:
            self.corridors.follow(self.gateway, {v.id: v.edge for v in raw["vehicles"]
                                                 if v.id in self.corridors.active}, t)

        # one direction is overloaded while another has green to spare -> propose an approved plan
        self.gateway.keep_muted({pr["tls"] for pr in problems})
        for pr in problems:
            if pr["tls"] not in self.gateway.plans:
                continue  # no approved passport: the junction is watched, never commanded
            if self.spillback and self.full_ahead(pr["tls"], pr["give"]):
                continue  # more green here would only move the cars into the next queue
            self.gateway.propose(
                t, "tls.select_plan", pr,
                f"{'камера' if pr['camera'] else 'детектор'} {pr['cam']}: "
                f"очередь в среднем {pr['queue']} м, у поперечного направления {pr['other']} м")
        demand = {(cam["tls"], phase): value
                  for cam in self.cameras for phase, value in self.monitor.demands.get(cam["id"], {}).items()}
        self.gateway.tick(t, demand)
        # going back to the base plan needs nobody's approval: release as soon as it stops helping
        for p in list(self.gateway.proposals.values()):
            if p["status"] != "active" or p["command"] != "tls.select_plan" or t - p.get("applied_at", t) < SETTLE:
                continue
            loaded, donor = demand.get((p["tls"], p["give"])), demand.get((p["tls"], p["take"]))
            if loaded is None or donor is None:
                continue
            if loaded < RELIEVED:
                self.gateway.release(p["id"], "relieved", t)
            elif donor >= loaded:
                self.gateway.release(p["id"], "balance", t)
            elif self.spillback and self.full_ahead(p["tls"], p["give"]):
                self.gateway.release(p["id"], "spillback", t)

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
        self._persist(t, queues)
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

    def _persist(self, t, queues: dict):
        """Write what changed to the history: commands on every change, queues once a minute."""
        if self.store is None:
            return
        if self.gateway.version != self._saved_version:
            self._saved_version = self.gateway.version
            for p in self.gateway.unsettled():
                self.store.save_command(p)
        if self._queue_sampled is None or not 0 <= t - self._queue_sampled < SAMPLE:
            self._queue_sampled = t
            self.store.add_queues(queues)

    def full_ahead(self, tls: str, phase: int) -> str | None:
        """The downstream approach of this phase that is already queued to the end of its sensor zone, if any."""
        for d in self._downstream.get((tls, phase), []):
            q = (self.queues.get(d["junction"]) or {}).get(d["approach"])
            if q is not None and q >= SPILL * self._zone.get(d["approach"], 100.0):
                return d["junction"]
        return None

    def update_junction(self, j: dict, why: str):
        """A passport was approved, edited or withdrawn: take the new description and plan table at once."""
        old = self._by_tls.get(j["tls"])
        if old is None:
            return
        old.clear()
        old.update(j)  # the same object is shared with the queue monitor and the corridors
        self._zone.update({a["id"]: a.get("zone", 100.0) for a in j["approaches"]})
        for ph in j.get("phases", []):
            self._downstream[(j["tls"], ph["index"])] = ph.get("downstream", [])
        self.monitor.forget(j["id"])
        table = {pl["id"] for pl in j["plans"]} if j.get("passport", {}).get("status") == "approved" else None
        self.gateway.set_plans(j["tls"], table, why, self.t)

    def devices(self, rows: list[dict], t: int) -> list[dict]:
        """Take the hub's view of the fleet, judge each unit, lock junctions whose unit cannot be trusted.
        Returns the alerts that are new since the last call; the brief for the UI is in self.dev."""
        new = []
        if self.store is not None and rows and (self._dev_sampled is None or not 0 <= t - self._dev_sampled < SAMPLE):
            self._dev_sampled = t
            self.store.add_device_samples(rows)
        for row in rows:
            fp = row.get("fingerprint")
            if fp and self._dev_keys.get(row["id"], fp) != fp:  # a new key was enrolled for this unit
                self.gateway.audit.append(t, "device-key-changed", "-", "-", row["id"],
                                          f"{self._dev_keys[row['id']]} -> {fp}", actor="registry", mode="approval")
            if fp:
                self._dev_keys[row["id"]] = fp
            alerts = health.assess(row)
            kinds = {(a["kind"], a["level"]) for a in alerts}
            before = self._dev_state.get(row["id"], set())
            for a in alerts:
                if (a["kind"], a["level"]) not in before:  # new, or the same trouble got worse
                    rec = {"t": t, "device": row["id"], "junction": row["junction"], "level": a["level"],
                           "kind": a["kind"], "text": a["text"]}
                    self.dev_alerts.append(rec)
                    new.append(rec)
                    self.gateway.audit.append(t, f"device-{a['level']}", "-", "-", row["id"], a["text"],
                                              actor="equipment", mode="approval")
            for kind in {k for k, _ in before} - {k for k, _ in kinds}:
                self.gateway.audit.append(t, "device-clear", "-", "-", row["id"], kind, actor="equipment", mode="approval")
            self._dev_state[row["id"]] = kinds
            blocking = [a["text"] for a in alerts if a["kind"] in health.BLOCKING]
            if row.get("tls"):
                self.gateway.set_locked(row["tls"], bool(blocking), "; ".join(blocking) or "unit trusted again", t)
            h = row.get("health") or {}
            kind = row.get("kind") or "unit"
            self.dev[row["junction"] if kind == "unit" else row["id"]] = {
                "id": row["id"], "k": kind, "addr": row.get("address"), "via": row.get("district"),
                "r": h.get("relay"), "b": h.get("buffered"),
                "serial": row.get("serial", ""), "o": int(row["online"]), "s": row["status"],
                "v": h.get("version"), "t": h.get("temp"), "d": h.get("door"), "c": h.get("controller"),
                "l": h.get("leases"), "u": h.get("uptime"), "a": row.get("age"), "n": row.get("connects", 0),
                "f": int(bool(row.get("first_seen"))),
                "al": [[a["level"], a["text"]] for a in alerts], "lock": row.get("tls") in self.gateway.locked,
            }
        present = {row["junction"] if (row.get("kind") or "unit") == "unit" else row["id"] for row in rows}
        for key in [k for k in self.dev if k not in present]:  # removed from the registry
            del self.dev[key]
        self.dev_alerts = self.dev_alerts[-200:]
        return new

    def on_report(self, rec, report: dict):
        """A report from a device: the Edge unit of a junction (queue readings and what happened to its leases),
        or a district server (queues of its cameras, for the junctions routed through it). rec: the registry
        record, or just the junction's tls as older callers pass it."""
        if isinstance(rec, str):
            rec = {"tls": rec}
        if rec.get("kind") == "district":
            return self._district_report(rec, report)
        tls = rec.get("tls")
        cam = self._by_tls.get(tls)
        if cam is None:
            return
        # a device is trusted for its own approaches only, and only for numbers
        queues = {}
        raw_queues = report.get("queues")
        if isinstance(raw_queues, dict):
            for a in cam["approaches"]:
                try:
                    queues[a["id"]] = max(0.0, float(raw_queues[a["id"]]))
                except (KeyError, TypeError, ValueError):
                    pass
        try:
            at = int(report.get("t", self.t))
        except (TypeError, ValueError):
            at = self.t
        before = self.sensors.get(cam["id"])
        # a unit without cameras (a light unit) reports no queues: it must not wipe out what its district's
        # cameras measured for this junction a moment ago
        if queues or before is None or not before[1] or abs(at - before[0]) > STALE:
            self.sensors[cam["id"]] = (at, queues)
        events = report.get("events")
        for ev in events if isinstance(events, list) else []:
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "tls":
                self.gateway.on_equipment(ev, self.t, tls)
            elif ev.get("type") == "edge":
                self.gateway.audit.append(self.t, "device-refused", "-", "-", str(ev.get("device")),
                                          str(ev.get("reason")), actor="equipment", mode="approval")

    def routed_through(self, district: str) -> set[str]:
        """Junction ids whose unit connects through this district server: the junctions it is trusted for."""
        return {j for j, d in self.dev.items() if d.get("k", "unit") == "unit" and d.get("via") == district}

    def _district_report(self, rec: dict, report: dict):
        """A district is trusted for numbers only, and only for the junctions routed through it. A report that
        arrives late (kept through an outage) goes to history at its own time and leaves the live reading alone."""
        raw = report.get("queues")
        mine = set(rec["junctions"]) if isinstance(rec.get("junctions"), list) else self.routed_through(rec["id"])
        try:
            at = int(report.get("t", self.t))
        except (TypeError, ValueError):
            at = self.t
        per = {}
        if isinstance(raw, dict):
            for cam in self.cameras:
                if cam["id"] not in mine:
                    continue
                queues = {}
                for a in cam["approaches"]:
                    try:
                        queues[a["id"]] = max(0.0, float(raw[a["id"]]))
                    except (KeyError, TypeError, ValueError):
                        pass
                if queues:
                    per[cam["id"]] = queues
        if per:
            if at < self.t - LATE and self.store is not None:
                self.store.add_queues(per, at=at)
            else:
                for j, queues in per.items():
                    self.sensors[j] = (at, queues)
        events = report.get("events")
        for ev in events if isinstance(events, list) else []:
            if isinstance(ev, dict) and ev.get("type") == "edge":
                self.gateway.audit.append(self.t, "device-refused", "-", "-", str(ev.get("device")),
                                          str(ev.get("reason")), actor="equipment", mode="approval")

    def _respond(self, ev: dict) -> list[dict]:
        return []  # city events answered by a record only; the green corridor is a real proposal now (corridor.py)

    def audit(self) -> dict:
        return {"count": self.gateway.count, "head": self.gateway.head, "applied": self.gateway.applied}

    def snapshot(self) -> dict:
        return {"t": self.t, "events": self.events[-100:], "shadow": self.shadow[-100:],
                "alerts": self.alerts[-100:], "infra": self.infra, "audit": self.audit(),
                "cmd": self.gateway.public(), "link": self.gateway.link_up,
                "log": self.gateway.records[-20:], "dev": self.dev, "devalerts": self.dev_alerts[-50:]}
