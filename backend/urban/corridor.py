"""Green corridor for an emergency unit: the signals on its route favour its approach while it comes.

From the route of a dispatched unit the platform finds the signalised junctions it will cross and,
at each, the phase its approach belongs to. For that phase it picks the approved plan with the most
green (one of the engineer's plans, as always: no timings are invented). An operator approves the
whole corridor at once; every junction is a separate lease, released as soon as the unit has passed
that junction, and all of them when the unit arrives. If the operator does nothing, nothing happens.

Several units often go to the same incident. At a junction they share:
- on the same approach, the green is held until the last of them has passed;
- on different approaches, the first one gets it; when it has passed, the junction is offered to the
  next one (a new proposal in that unit's corridor), instead of keeping the second one on red.

Where the unit is comes from the simulation in the demo. On the street it would come from the
ambulance's own tracker (AVL); without it the corridor is not offered.
"""

COMMAND = "tls.corridor"


class Corridors:
    def __init__(self, junctions: list[dict]):
        self.approach: dict[str, tuple[dict, int, str]] = {}  # approach edge -> (junction, phase index, street)
        for j in junctions:
            for ph in j.get("phases", []):
                for edge, street in zip(ph["edges"], ph.get("streets", []) + [""] * len(ph["edges"])):
                    self.approach[edge] = (j, ph["index"], street)
        self.active: dict[str, dict] = {}  # dispatch id -> {unit, route, reason, stops: [stop]}
        self.held: dict[str, dict] = {}    # tls -> {pid, phase, units: set of dispatch ids}

    @staticmethod
    def best_plan(junction: dict, phase: int) -> dict | None:
        """The approved plan that gives this phase the most green (never the base plan)."""
        plans = [p for p in junction.get("plans", []) if p.get("give") == phase and p["id"] != 0]
        return max(plans, key=lambda p: p.get("shift", 0)) if plans else None

    def stops(self, route: list[str]) -> list[tuple[int, dict, int, str, dict]]:
        """(route index, junction, phase, street, plan) for every signalised junction the route crosses, in order."""
        out, seen = [], set()
        for i, edge in enumerate(route):
            hit = self.approach.get(edge)
            if hit is None or hit[0]["tls"] in seen:
                continue
            junction, phase, street = hit
            plan = self.best_plan(junction, phase)
            if plan is not None:
                seen.add(junction["tls"])
                out.append((i, junction, phase, street, plan))
        return out

    # ---- opening
    def open(self, gateway, ev: dict, t: int, destination: str = "") -> list[dict]:
        """Propose the corridor for a dispatch event that carries its route. Returns the proposals made."""
        route = ev.get("route") or []
        rid, unit = ev["id"], ev.get("unit", "")
        c = {"unit": unit, "route": route, "destination": destination, "stops": []}
        made = []
        for i, junction, phase, street, plan in self.stops(route):
            stop = {"index": i, "junction": junction, "phase": phase, "street": street, "plan": plan, "pid": None}
            held = self.held.get(junction["tls"])
            if held is not None and held["phase"] == phase:
                held["units"].add(rid)  # same approach as a unit already favoured here: ride along
                stop["pid"] = held["pid"]
            elif held is None:
                p = self._propose(gateway, rid, c, stop, t)
                if p is not None:
                    made.append(p)
            # else: another unit holds this junction for another approach; offered when it has passed
            c["stops"].append(stop)
        if c["stops"]:
            self.active[rid] = c
        return made

    def _propose(self, gateway, rid: str, c: dict, stop: dict, t: int) -> dict | None:
        j, plan = stop["junction"], stop["plan"]
        where = f" к «{c['destination']}»" if c["destination"] else ""
        p = gateway.propose(t, COMMAND, {
            "tls": j["tls"], "cam": j["id"], "plan": plan["id"], "shift": plan.get("shift", 0),
            "give": stop["phase"], "take": plan.get("take"), "street": stop["street"], "from_street": "",
            "corridor": rid, "unit": c["unit"],
        }, f"{c['unit'] or 'экстренная служба'} едет{where}: зелёный подъезду «{stop['street'] or j['name']}», "
           f"пока она не проедет")
        if p is not None:
            stop["pid"] = p["id"]
            self.held[j["tls"]] = {"pid": p["id"], "phase": stop["phase"], "units": {rid}}
        return p

    # ---- following the units
    def follow(self, gateway, positions: dict[str, str], t: int):
        """positions: vehicle id -> current edge. A junction the unit has left behind is let go."""
        for rid, c in list(self.active.items()):
            edge = positions.get(rid)
            if edge is None or edge.startswith(":"):
                continue  # not seen this second, or inside a junction
            try:
                at = c["route"].index(edge)
            except ValueError:
                continue
            for stop in [s for s in c["stops"] if at > s["index"]]:  # past the approach: it has crossed
                c["stops"].remove(stop)
                self._leave(gateway, rid, stop, "passed", t)
            if not c["stops"]:
                del self.active[rid]

    def close(self, gateway, rid: str, why: str, t: int):
        """The unit arrived (or the scenario ended): everything still held for it goes back."""
        c = self.active.pop(rid, None)
        for stop in (c or {}).get("stops", []):
            self._leave(gateway, rid, stop, why, t)

    def _leave(self, gateway, rid: str, stop: dict, why: str, t: int):
        tls = stop["junction"]["tls"]
        held = self.held.get(tls)
        if held is None or rid not in held["units"]:
            return  # this unit never held the junction (it was waiting for another one)
        held["units"].discard(rid)
        if held["units"]:
            return  # another unit on the same approach still needs it
        del self.held[tls]
        self._end(gateway, held["pid"], why, t)
        self._next(gateway, tls, t)

    def _next(self, gateway, tls: str, t: int):
        """Offer a freed junction to the next unit that still has to cross it."""
        for rid, c in self.active.items():
            for stop in c["stops"]:
                if stop["junction"]["tls"] == tls and stop["pid"] is None:
                    self._propose(gateway, rid, c, stop, t)
                    if tls in self.held:
                        for rid2, c2 in self.active.items():  # others on that same approach ride along
                            for s2 in c2["stops"]:
                                if s2["junction"]["tls"] == tls and s2["pid"] is None and s2["phase"] == stop["phase"]:
                                    self.held[tls]["units"].add(rid2)
                                    s2["pid"] = self.held[tls]["pid"]
                        return

    @staticmethod
    def _end(gateway, pid: str, why: str, t: int):
        p = gateway.proposals.get(pid)
        if p is None:
            return
        if p["status"] in ("approved", "active"):
            gateway.release(pid, why, t)
        elif p["status"] == "pending":
            gateway.withdraw(pid, why, t)

    def reset(self):
        self.active.clear()
        self.held.clear()
