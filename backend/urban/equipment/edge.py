"""URBAN Edge: the logic that runs in our box at the junction.

This is where the safety guarantees live, deliberately independent of the platform:
- a command is a lease that expires on its own; no renewal means the base plan returns;
- only plans from the approved table of the controller can be selected, whatever is asked for;
- one lease per junction; the base plan is always reachable.
It knows nothing about protocols: that is the job of the adapter.
"""
from .adapter import BASE_PLAN, AdapterError, ControllerAdapter

MAX_LEASE_TTL = 300  # seconds a single lease may live without renewal, whatever the platform asks


class EdgeController:
    def __init__(self, adapter: ControllerAdapter):
        self.adapter = adapter
        self.leases = {}  # tls id -> {"id", "plan", "expires"}

    # ---- messages from the gateway ----
    def handle(self, msg: dict, now: int) -> list[dict]:
        op = msg["op"]
        if op == "lease":
            return self._lease(msg, now)
        if op == "release":
            return self._revert(lambda lease: lease["id"] == msg["id"], "released")
        return []

    def tick(self, now: int) -> list[dict]:
        """Expire leases that were not renewed: the platform being silent is the safe state."""
        return self._revert(lambda lease: now >= lease["expires"], "expired")

    # ---- internals ----
    def _lease(self, msg, now):
        tls, pid, plan = msg["tls"], msg["id"], msg.get("plan")
        ttl = min(int(msg["ttl"]), MAX_LEASE_TTL)
        held = self.leases.get(tls)
        if held and held["id"] == pid:
            held["expires"] = now + ttl
            try:
                self.adapter.keepalive(tls, held["plan"])
            except AdapterError as err:
                return self._revert(lambda lease: lease is held, f"controller lost: {err}")
            return [{"type": "tls", "id": pid, "status": "renewed", "tls": tls}]
        if held:
            return [self._rejected(pid, tls, "junction is already under another lease")]
        approved = self.adapter.plans(tls)
        if not approved:
            return [self._rejected(pid, tls, "unknown signal controller")]
        if plan == BASE_PLAN or plan not in approved:
            return [self._rejected(pid, tls, "plan is not in the approved table of the controller")]
        try:
            self.adapter.select(tls, plan)
        except AdapterError as err:
            return [self._rejected(pid, tls, str(err))]
        self.leases[tls] = {"id": pid, "plan": plan, "expires": now + ttl}
        return [{"type": "tls", "id": pid, "status": "applied", "tls": tls, "plan": plan}]

    def _revert(self, pick, reason):
        events = []
        for tls in [t for t, lease in self.leases.items() if pick(lease)]:
            lease = self.leases.pop(tls)
            try:
                self.adapter.base(tls)
            except AdapterError:
                pass  # the loss-of-communication timer inside the controller is the last line of defence
            events.append({"type": "tls", "id": lease["id"], "status": "reverted", "tls": tls, "reason": reason})
        return events

    @staticmethod
    def _rejected(pid, tls, why):
        return {"type": "tls", "id": pid, "status": "rejected", "tls": tls, "reason": why}
