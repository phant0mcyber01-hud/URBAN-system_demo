"""Emulated traffic-light controllers (stand-in for the equipment side).

This is where the safety guarantees live, deliberately independent of the platform:
- every change is a lease that expires on its own; no renewal means the base program returns;
- the controller enforces its own hard bounds whatever the platform asks for;
- the base program is restored exactly as it was before the first change.
"""
import traci
import traci.constants as tc
from traci import _trafficlight as tl

MAX_EXTENSION = 15   # seconds a phase may be lengthened, hard limit of the controller
MAX_LEASE_TTL = 120  # seconds a single lease may live without renewal


class SignalControl:
    def __init__(self):
        self.leases = {}  # tls id -> {"id", "approach", "expires", "base"}

    # ---- messages from the gateway ----
    def handle(self, msg: dict, now: int) -> list[dict]:
        op = msg["op"]
        if op == "lease":
            return self._lease(msg, now)
        if op == "release":
            return self._revert(lambda lease: lease["id"] == msg["id"], "released")
        if op == "revoke_all":
            return self._revert(lambda lease: True, "revoked")
        return []

    def tick(self, now: int) -> list[dict]:
        """Expire leases that were not renewed: the platform being silent is the safe state."""
        return self._revert(lambda lease: now >= lease["expires"], "expired")

    # ---- internals ----
    def _lease(self, msg, now):
        tls_id, pid = msg["tls"], msg["id"]
        ttl = min(int(msg["ttl"]), MAX_LEASE_TTL)
        held = self.leases.get(tls_id)
        if held and held["id"] == pid:
            held["expires"] = now + ttl
            return [{"type": "tls", "id": pid, "status": "renewed", "tls": tls_id}]
        if held:
            return [self._rejected(pid, tls_id, "junction is already under another lease")]
        if tls_id not in traci.trafficlight.getIDList():
            return [self._rejected(pid, tls_id, "unknown signal controller")]

        seconds = min(int(msg["seconds"]), MAX_EXTENSION)
        logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        phase = self._serving_phase(tls_id, logic, msg["approach"])
        if phase is None:
            return [self._rejected(pid, tls_id, "approach is not served by any phase")]

        current = traci.trafficlight.getPhase(tls_id)
        phases = [tl.Phase(p.duration + (seconds if i == phase else 0), p.state, p.minDur, p.maxDur, p.next, p.name)
                  for i, p in enumerate(logic.phases)]
        traci.trafficlight.setProgramLogic(
            tls_id, tl.Logic(logic.programID, logic.type, current, phases=phases, subParameter=logic.subParameter))
        traci.trafficlight.setPhase(tls_id, current)
        self.leases[tls_id] = {"id": pid, "approach": msg["approach"], "expires": now + ttl,
                               "base": logic, "phase": current}
        return [{"type": "tls", "id": pid, "status": "applied", "tls": tls_id, "seconds": seconds, "phase": phase}]

    def _revert(self, pick, reason):
        events = []
        for tls_id in [t for t, lease in self.leases.items() if pick(lease)]:
            lease = self.leases.pop(tls_id)
            current = traci.trafficlight.getPhase(tls_id)
            base = lease["base"]
            traci.trafficlight.setProgramLogic(
                tls_id, tl.Logic(base.programID, base.type, current, phases=base.phases, subParameter=base.subParameter))
            traci.trafficlight.setPhase(tls_id, current)
            events.append({"type": "tls", "id": lease["id"], "status": "reverted", "tls": tls_id, "reason": reason})
        return events

    @staticmethod
    def _rejected(pid, tls_id, why):
        return {"type": "tls", "id": pid, "status": "rejected", "tls": tls_id, "reason": why}

    @staticmethod
    def _serving_phase(tls_id, logic, approach):
        """The phase that gives green to most links coming from the approach edge."""
        links = traci.trafficlight.getControlledLinks(tls_id)
        best, best_count = None, 0
        for i, p in enumerate(logic.phases):
            count = sum(1 for j, ch in enumerate(p.state)
                        if ch in "Gg" and j < len(links) and links[j] and links[j][0][0].rsplit("_", 1)[0] == approach)
            if count > best_count:
                best, best_count = i, count
        return best
