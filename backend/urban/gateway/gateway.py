"""Command Gateway: the only path from the platform to city equipment.

Rules, in order of importance:
1. Only commands listed in POLICY can ever reach equipment; everything else is recorded and goes nowhere.
2. A command needs an operator's decision. The platform proposes, it never approves its own proposals.
3. A command is a lease: equipment reverts to its base program on its own when the lease is not renewed.
   The gateway renews only while the link is up and the operator's approval window is open.
4. Going back to the base program is always allowed: the platform may release a command early
   (the problem is gone, or the other direction started to suffer) without asking anyone.
5. Every step is written to the hash-chained audit log.
In this demo the equipment is emulated; the gateway has no other transport.
"""
from dataclasses import dataclass

from .audit import AuditLog

OPEN = ("pending", "approved", "active")


@dataclass(frozen=True)
class Policy:
    max_seconds: int    # largest allowed shift of green between two phases
    lease_ttl: int      # seconds equipment keeps a change without renewal
    window: int         # seconds an approval stays valid in total
    cooldown: int       # seconds before the same junction may be touched again
    pending_ttl: int    # seconds a proposal waits for an operator


# tls.rebalance: move green time from a phase with spare capacity to an overloaded one.
# The cycle length never changes.
POLICY = {"tls.rebalance": Policy(max_seconds=15, lease_ttl=60, window=300, cooldown=90, pending_ttl=120)}


class CommandGateway:
    mode = "approval"

    def __init__(self, send=lambda msg: None):
        self.send = send
        self.audit = AuditLog()
        self.proposals: dict[str, dict] = {}
        self.link_up = True
        self.applied = 0  # commands that actually reached (emulated) equipment
        self._n = 0
        self._last_done: dict[str, int] = {}  # tls -> time its last command or proposal finished
        self.version = 0

    # ---- compatibility with the audit view ----
    @property
    def count(self):
        return self.audit.count

    @property
    def head(self):
        return self.audit.head

    @property
    def records(self):
        return self.audit.records

    def verify(self):
        return self.audit.verify()

    # ---- proposals ----
    def propose_shadow(self, t, ref, command, target, note) -> dict:
        """A command with no executor: recorded for comparison with what people do, never sent."""
        return self.audit.append(t, "proposed", ref, command, target, note, executed=False, mode="shadow")

    def propose(self, t, command, params: dict, reason: str) -> dict | None:
        """params: tls, cam, give, take (phase indexes), seconds, street, from_street, queue, other."""
        pol = POLICY.get(command)
        tls = params.get("tls")
        if pol is None or not tls:
            self.audit.append(t, "denied", "-", command, str(tls), "command is not in the policy")
            return None
        if params.get("give") is None or params.get("give") == params.get("take"):
            self.audit.append(t, "denied", "-", command, str(tls), "needs two different phases")
            return None
        if any(p["status"] in OPEN and p["tls"] == tls for p in self.proposals.values()):
            return None
        if t - self._last_done.get(tls, -10**9) < pol.cooldown:
            return None
        seconds = max(1, min(int(params.get("seconds", 10)), pol.max_seconds))
        self._n += 1
        p = {"id": f"cmd-{self._n}", "t": t, "command": command, "tls": tls, "cam": params.get("cam"),
             "give": params["give"], "take": params["take"], "seconds": seconds,
             "street": params.get("street", ""), "from_street": params.get("from_street", ""),
             "status": "pending", "reason": reason,
             "queue0": params.get("queue", 0), "other0": params.get("other", 0),
             "queue": params.get("queue", 0), "other": params.get("other", 0),
             "until": None, "result": None}
        self.proposals[p["id"]] = p
        self.audit.append(t, "proposed", p["id"], command, self._target(p), f"{seconds} s; {reason}", mode="approval")
        self.version += 1
        return p

    def decide(self, pid: str, approve: bool, actor: str, t: int) -> dict | None:
        p = self.proposals.get(pid)
        if p is None or p["status"] != "pending" or (approve and not self.link_up):
            return None
        pol = POLICY[p["command"]]
        if not approve:
            p["status"] = "rejected"
            self._last_done[p["tls"]] = t
            self.audit.append(t, "rejected", pid, p["command"], self._target(p), "", actor=actor, mode="approval")
        else:
            p["status"], p["until"], p["renewed"] = "approved", t + pol.window, t
            self._lease(p, pol)
            self.audit.append(t, "approved", pid, p["command"], self._target(p),
                              f"lease {pol.lease_ttl} s, window {pol.window} s", actor=actor, mode="approval")
        self.version += 1
        return p

    def release(self, pid: str, why: str, t: int):
        """Return one junction to its base program early. Always allowed, needs no approval."""
        p = self.proposals.get(pid)
        if p is None or p["status"] not in ("approved", "active"):
            return
        self.send({"op": "release", "id": pid})
        p["status"], p["end"] = "revoking", why
        self.audit.append(t, "released", pid, p["command"], self._target(p), why, mode="approval")
        self.version += 1

    def revoke_all(self, actor: str, t: int):
        """Kill switch: pull back everything now. Works even when the link is marked down."""
        for p in self.proposals.values():
            if p["status"] in ("approved", "active"):
                self.send({"op": "release", "id": p["id"]})
                p["status"], p["end"] = "revoking", "revoked"
            elif p["status"] == "pending":
                p["status"] = "rejected"
        self.audit.append(t, "revoked", "*", "*", "all active leases", "kill switch", actor=actor, mode="approval")
        self.version += 1

    def set_link(self, up: bool, actor: str, t: int):
        """Demo control: cut the platform-to-equipment link to prove that equipment reverts by itself."""
        self.link_up = up
        self.audit.append(t, "link-up" if up else "link-down", "-", "-", "platform <-> equipment",
                          "" if up else "no more renewals: equipment must revert on its own",
                          actor=actor, mode="approval")
        self.version += 1

    # ---- driven by the platform every frame ----
    def tick(self, t: int, demand: dict):
        """demand: {(tls, phase index): mean standing vehicles}. Renews leases, expires stale proposals."""
        for p in self.proposals.values():
            st = p["status"]
            if st == "pending" and t - p["t"] > POLICY[p["command"]].pending_ttl:
                p["status"] = "expired"
                self._last_done[p["tls"]] = t
                self.audit.append(t, "expired", p["id"], p["command"], p["tls"], "no operator decision", mode="approval")
                self.version += 1
            elif st in ("approved", "active"):
                p["queue"] = round(demand.get((p["tls"], p["give"]), p["queue"]), 1)
                p["other"] = round(demand.get((p["tls"], p["take"]), p["other"]), 1)
                pol = POLICY[p["command"]]
                if t >= p["until"]:
                    self.send({"op": "release", "id": p["id"]})
                    p["status"], p["end"] = "revoking", "window"
                elif self.link_up and t - p["renewed"] >= pol.lease_ttl // 3:
                    p["renewed"] = t
                    self._lease(p, pol)

    def on_equipment(self, ev: dict, t: int):
        p = self.proposals.get(ev["id"])
        if p is None:
            return
        if ev["status"] == "applied":
            p["status"], p["applied_at"], p["seconds"] = "active", t, ev["seconds"]
            self.applied += 1
            self.audit.append(t, "applied", p["id"], p["command"], self._target(p), f"{ev['seconds']} s moved",
                              actor="equipment", executed=True, mode="approval")
        elif ev["status"] == "reverted":
            reason = p.get("end") if ev["reason"] == "released" and p.get("end") else ev["reason"]
            p["status"] = "done"
            p["result"] = {"reason": reason, "queue0": p["queue0"], "queue": p["queue"],
                           "other0": p["other0"], "other": p["other"], "t": t}
            self._last_done[p["tls"]] = t
            self.audit.append(t, "reverted", p["id"], p["command"], self._target(p), reason,
                              actor="equipment", executed=True, mode="approval")
        elif ev["status"] == "rejected":
            p["status"] = "failed"
            p["result"] = {"reason": ev["reason"]}
            self._last_done[p["tls"]] = t
            self.audit.append(t, "equipment-rejected", p["id"], p["command"], self._target(p), ev["reason"],
                              actor="equipment", mode="approval")
        self.version += 1

    def reset(self):
        """The scenario restarted: equipment state is gone, close what was open. The audit log stays."""
        for p in self.proposals.values():
            if p["status"] in OPEN or p["status"] == "revoking":
                p["status"] = "done"
                p["result"] = {"reason": "restart", "queue0": p["queue0"], "queue": p["queue"],
                               "other0": p["other0"], "other": p["other"]}
        self._last_done.clear()
        self.version += 1

    def public(self) -> list[dict]:
        return list(self.proposals.values())[-12:]

    @staticmethod
    def _target(p: dict) -> str:
        return f"{p['tls']}: phase {p['take']} -> phase {p['give']}"

    def _lease(self, p: dict, pol: Policy):
        self.send({"op": "lease", "id": p["id"], "tls": p["tls"], "give": p["give"], "take": p["take"],
                   "seconds": p["seconds"], "ttl": pol.lease_ttl})
