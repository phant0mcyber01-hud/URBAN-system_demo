"""Emulated traffic-light controllers (stand-in for the equipment side).

This is where the safety guarantees live, deliberately independent of the platform:
- every change is a lease that expires on its own; no renewal means the base program returns;
- the controller enforces its own hard bounds whatever the platform asks for;
- the cycle length never changes: green is only moved from one phase to another;
- the base program is restored exactly as it was before the first change.
"""
import traci
from traci import _trafficlight as tl

MAX_SHIFT = 15       # seconds of green that may be moved between two phases, hard limit
MIN_GREEN = 15       # seconds of green every phase keeps whatever is asked
MIN_SHIFT = 3        # below this the change is not worth making
MAX_LEASE_TTL = 120  # seconds a single lease may live without renewal


def _is_green(phase) -> bool:
    return "y" not in phase.state and bool(set(phase.state) & set("Gg"))


class SignalControl:
    def __init__(self):
        self.leases = {}  # tls id -> {"id", "expires", "base"}

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

        logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        give, take = msg.get("give"), msg.get("take")
        valid = range(len(logic.phases))
        if give not in valid or take not in valid or give == take \
                or not _is_green(logic.phases[give]) or not _is_green(logic.phases[take]):
            return [self._rejected(pid, tls_id, "phases must be two different green phases")]
        seconds = min(int(msg["seconds"]), MAX_SHIFT, int(logic.phases[take].duration) - MIN_GREEN)
        if seconds < MIN_SHIFT:
            return [self._rejected(pid, tls_id, "the donor phase has no green to spare")]

        delta = {give: seconds, take: -seconds}
        current = traci.trafficlight.getPhase(tls_id)
        phases = [tl.Phase(p.duration + delta.get(i, 0), p.state, p.minDur, p.maxDur, p.next, p.name)
                  for i, p in enumerate(logic.phases)]
        traci.trafficlight.setProgramLogic(
            tls_id, tl.Logic(logic.programID, logic.type, current, phases=phases, subParameter=logic.subParameter))
        traci.trafficlight.setPhase(tls_id, current)
        self.leases[tls_id] = {"id": pid, "expires": now + ttl, "base": logic}
        return [{"type": "tls", "id": pid, "status": "applied", "tls": tls_id, "seconds": seconds}]

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
