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
import re
from dataclasses import dataclass

from .audit import AuditLog

OPEN = ("pending", "approved", "active")
GROUP_MAX = 32  # proposals decided in one go at most
SILENCE_MARGIN = 30  # seconds past the lease before a silent device is presumed to have reverted


@dataclass(frozen=True)
class Policy:
    lease_ttl: int      # seconds equipment keeps a change without renewal
    window: int         # seconds an approval stays valid in total
    cooldown: int       # seconds before the same junction may be touched again
    pending_ttl: int    # seconds a proposal waits for an operator


# tls.select_plan: switch a junction to one of the plans its traffic engineer approved.
# Timings follow the real signal cycle (about 90 s): a controller changes plan at a cycle
# boundary, so a lease has to outlive a cycle and the effect needs a few cycles to show.
# tls.corridor: the same kind of command (an approved plan) for an emergency unit's route: shorter
# in every way, it lives only until the unit has passed, and takes precedence over a rebalancing
# command at that junction, which is released first (going back to base is always allowed).
POLICY = {"tls.select_plan": Policy(lease_ttl=180, window=900, cooldown=180, pending_ttl=120),
          "tls.corridor": Policy(lease_ttl=90, window=300, cooldown=0, pending_ttl=60)}
CORRIDOR = "tls.corridor"


class CommandGateway:
    mode = "approval"

    def __init__(self, send=lambda msg: None, plans: dict[str, set[int]] | None = None,
                 reachable=lambda tls: True, store=None):
        """plans: tls id -> approved plan ids. Without it every plan request is refused.
        reachable(tls): is the device of that junction connected right now.
        store: persistent history (urban.store.Store); the audit chain continues from it."""
        self.send = send
        self.plans = plans or {}
        self.reachable = reachable
        self.audit = AuditLog(store=store)
        self.proposals: dict[str, dict] = {}
        self.link_up = True
        self.applied = 0  # commands that actually reached (emulated) equipment
        self._n = 0
        self._last_done: dict[str, int] = {}  # tls -> time its last command or proposal finished
        self.muted: set[str] = set()  # junctions an operator said no to, until their problem clears
        self.locked: set[str] = set()  # junctions whose device cannot be trusted right now (opened cabinet, dead controller)
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
        """params: tls, cam, plan (approved plan id), shift, give, take, street, from_street, queue, other."""
        pol = POLICY.get(command)
        tls = params.get("tls")
        if pol is None or not tls:
            self.audit.append(t, "denied", "-", command, str(tls), "command is not in the policy")
            return None
        plan = params.get("plan")
        if not plan or plan not in self.plans.get(tls, ()):  # plan 0 is the base plan: nothing to propose
            self.audit.append(t, "denied", "-", command, str(tls), "plan is not in the approved table")
            return None
        corridor = command == CORRIDOR
        if tls in self.locked:
            return None  # an untrusted unit gets nothing, an emergency included
        if any(p["status"] in OPEN and p["tls"] == tls and (not corridor or p["command"] == CORRIDOR)
               for p in self.proposals.values()):
            return None  # one rebalancing at a time; a corridor may override rebalancing, not another corridor
        if not corridor and (tls in self.muted or t - self._last_done.get(tls, -10**9) < pol.cooldown):
            return None
        self._n += 1
        p = {"id": f"cmd-{self._n}", "t": t, "command": command, "tls": tls, "cam": params.get("cam"),
             "plan": plan, "shift": params.get("shift", 0), "give": params.get("give"), "take": params.get("take"),
             "street": params.get("street", ""), "from_street": params.get("from_street", ""),
             "status": "pending", "reason": reason,
             **({"corridor": params.get("corridor"), "unit": params.get("unit", "")} if corridor else {}),
             "queue0": params.get("queue", 0), "other0": params.get("other", 0),
             "queue": params.get("queue", 0), "other": params.get("other", 0),
             "until": None, "result": None}
        self.proposals[p["id"]] = p
        self.audit.append(t, "proposed", p["id"], command, self._target(p), reason, mode="approval")
        self.version += 1
        return p

    def decide(self, pid: str, approve: bool, actor: str, t: int) -> dict | None:
        p = self.proposals.get(pid)
        if p is None or p["status"] != "pending":
            return None
        if approve and (p["tls"] in self.locked or not (self.link_up and self.reachable(p["tls"]))):
            return None  # nobody to send it to, or nobody we trust: approving would be a promise we cannot keep
        pol = POLICY[p["command"]]
        p["actor"] = actor
        if not approve:
            p["status"] = "rejected"
            if p["command"] != CORRIDOR:  # saying no to a corridor says nothing about the junction's queues
                self._last_done[p["tls"]] = t
                self.muted.add(p["tls"])
            self.audit.append(t, "rejected", pid, p["command"], self._target(p), "", actor=actor, mode="approval")
        else:
            if p["command"] == CORRIDOR:  # the emergency goes first: rebalancing at this junction steps aside
                for other in list(self.proposals.values()):
                    if other is not p and other["tls"] == p["tls"] and other["command"] != CORRIDOR:
                        if other["status"] in ("approved", "active"):
                            self.release(other["id"], "corridor", t)
                        elif other["status"] == "pending":
                            self.withdraw(other["id"], "corridor", t)
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
        self.send({"op": "release", "id": pid, "tls": p["tls"]})
        p["status"], p["end"] = "revoking", why
        self.audit.append(t, "released", pid, p["command"], self._target(p), why, mode="approval")
        self.version += 1

    def withdraw(self, pid: str, why: str, t: int):
        """The platform takes back a proposal nobody decided on yet (its reason is gone)."""
        p = self.proposals.get(pid)
        if p is None or p["status"] != "pending":
            return
        p["status"], p["end"] = "expired", why
        self.audit.append(t, "withdrawn", pid, p["command"], self._target(p), why, mode="approval")
        self.version += 1

    def revoke_all(self, actor: str, t: int):
        """Kill switch: pull back everything now. Works even when the link is marked down."""
        for p in self.proposals.values():
            if p["status"] in ("approved", "active"):
                self.send({"op": "release", "id": p["id"], "tls": p["tls"]})
                p["status"], p["end"], p["actor"] = "revoking", "revoked", actor
            elif p["status"] == "pending":
                p["status"], p["actor"] = "rejected", actor
                self._last_done[p["tls"]] = t
                self.muted.add(p["tls"])
        self.audit.append(t, "revoked", "*", "*", "all active leases", "kill switch", actor=actor, mode="approval")
        self.version += 1

    def set_locked(self, tls: str, locked: bool, why: str, t: int):
        """A unit stops being trustworthy (cabinet opened, controller link dead) or becomes so again.
        Locking returns the junction to its base plan at once and blocks new commands there."""
        if locked == (tls in self.locked):
            return
        if locked:
            self.locked.add(tls)
            for p in list(self.proposals.values()):
                if p["tls"] != tls:
                    continue
                if p["status"] in ("approved", "active"):
                    self.release(p["id"], "locked", t)
                elif p["status"] == "pending":
                    p["status"] = "rejected"
                    self._last_done[tls] = t
            self.audit.append(t, "junction-locked", "-", "-", tls, why, actor="equipment", mode="approval")
        else:
            self.locked.discard(tls)
            self.audit.append(t, "junction-unlocked", "-", "-", tls, why, actor="equipment", mode="approval")
        self.version += 1

    def set_plans(self, tls: str, plans: set[int] | None, why: str, t: int):
        """The approved plan table of a junction changed: its passport was approved, edited or withdrawn.
        Commands made against the old table end: running ones go back to the base plan, pending ones are
        withdrawn. None: no approved table, the junction is only watched."""
        for p in list(self.proposals.values()):
            if p["tls"] != tls:
                continue
            if p["status"] in ("approved", "active"):
                self.release(p["id"], why, t)
            elif p["status"] == "pending":
                self.withdraw(p["id"], why, t)
        if plans:
            self.plans[tls] = set(plans)
        else:
            self.plans.pop(tls, None)
        self.version += 1

    def set_link(self, up: bool, actor: str, t: int):
        """Demo control: cut the platform-to-equipment link to prove that equipment reverts by itself."""
        self.link_up = up
        self.audit.append(t, "link-up" if up else "link-down", "-", "-", "platform <-> equipment",
                          "" if up else "no more renewals: equipment must revert on its own",
                          actor=actor, mode="approval")
        self.version += 1

    # ---- driven by the platform every frame ----
    def keep_muted(self, still_raised: set[str]):
        """A rejection holds only while the problem it answered is still there."""
        self.muted &= still_raised

    def tick(self, t: int, demand: dict):
        """demand: {(tls, phase index): mean queue metres}. Renews leases, expires stale proposals."""
        for p in self.proposals.values():
            st = p["status"]
            if st == "pending" and t - p["t"] > POLICY[p["command"]].pending_ttl:
                p["status"] = "expired"
                if p["command"] != CORRIDOR:  # an unanswered corridor says nothing about this junction's queues
                    self._last_done[p["tls"]] = t
                self.audit.append(t, "expired", p["id"], p["command"], p["tls"], "no operator decision", mode="approval")
                self.version += 1
            elif st in ("approved", "active"):
                p["queue"] = round(demand.get((p["tls"], p["give"]), p["queue"]))
                p["other"] = round(demand.get((p["tls"], p["take"]), p["other"]))
                pol = POLICY[p["command"]]
                connected = self.link_up and self.reachable(p["tls"])
                if t >= p["until"]:
                    self.send({"op": "release", "id": p["id"], "tls": p["tls"]})
                    p["status"], p["end"] = "revoking", "window"
                elif connected and t - p["renewed"] >= pol.lease_ttl // 3:
                    p["renewed"] = t
                    self._lease(p, pol)
                elif not connected and t - p["renewed"] > pol.lease_ttl + SILENCE_MARGIN:
                    # nothing was renewed and nothing was heard: by now the lease has run out on the device
                    p["status"] = "done"
                    p["result"] = {"reason": "expired", "queue0": p["queue0"], "queue": p["queue"],
                                   "other0": p["other0"], "other": p["other"], "t": t}
                    self._last_done[p["tls"]] = t
                    self.audit.append(t, "presumed-expired", p["id"], p["command"], self._target(p),
                                      "device unreachable past its lease", mode="approval")
                    self.version += 1

    def on_equipment(self, ev: dict, t: int, tls: str | None = None):
        """A report from equipment about one of our commands. tls: the junction the reporting device
        is enrolled for; a device may only speak about commands at its own junction."""
        p = self.proposals.get(ev.get("id"))
        if p is None:
            return
        if tls is not None and p["tls"] != tls:
            self.audit.append(t, "device-ignored", p["id"], p["command"], tls,
                              "report about another junction", actor="equipment", mode="approval")
            return
        status, reason = ev.get("status"), str(ev.get("reason", ""))
        # a closed command stays closed: a late or repeated report must not reopen it
        if status == "applied" and p["status"] == "approved":
            p["status"], p["applied_at"] = "active", t
            self.applied += 1
            self.audit.append(t, "applied", p["id"], p["command"], self._target(p), f"plan {ev.get('plan')} running",
                              actor="equipment", executed=True, mode="approval")
        elif status == "reverted" and p["status"] in ("approved", "active", "revoking"):
            reason = p.get("end") if reason == "released" and p.get("end") else reason
            p["status"] = "done"
            p["result"] = {"reason": reason, "queue0": p["queue0"], "queue": p["queue"],
                           "other0": p["other0"], "other": p["other"], "t": t}
            self._last_done[p["tls"]] = t
            self.audit.append(t, "reverted", p["id"], p["command"], self._target(p), reason,
                              actor="equipment", executed=True, mode="approval")
        elif status == "rejected" and p["status"] in ("approved", "active"):
            p["status"] = "failed"
            p["result"] = {"reason": reason}
            self._last_done[p["tls"]] = t
            self.audit.append(t, "equipment-rejected", p["id"], p["command"], self._target(p), reason,
                              actor="equipment", mode="approval")
        else:
            return
        self.version += 1

    def reset(self):
        """The scenario restarted: equipment state is gone, close what was open. The audit log stays."""
        for p in self.proposals.values():
            if p["status"] in OPEN or p["status"] == "revoking":
                p["status"] = "done"
                p["result"] = {"reason": "restart", "queue0": p["queue0"], "queue": p["queue"],
                               "other0": p["other0"], "other": p["other"]}
        self._last_done.clear()
        self.muted.clear()
        self.locked.clear()
        self.version += 1

    def public(self) -> list[dict]:
        """The recent proposals for the UI; pending ones on a shared street, or of one corridor, carry the same `group`."""
        recent = list(self.proposals.values())[-16:]
        groups = street_groups([p for p in recent if p["status"] == "pending" and p["command"] != CORRIDOR])
        corridors: dict[str, list[dict]] = {}
        for p in recent:
            if p["status"] == "pending" and p["command"] == CORRIDOR:
                corridors.setdefault(p["corridor"], []).append(p)
        for key, ps in corridors.items():
            if len(ps) < 2:
                continue  # a corridor through one junction is just one card
            for p in ps:
                groups[p["id"]] = {"group": key, "group_street": f"Зелёный коридор: {p.get('unit') or 'экстренная служба'}",
                                   "group_size": len(ps)}
        return [{**p, **groups[p["id"]]} if p["id"] in groups else p for p in recent]

    def decide_many(self, pids: list[str], approve: bool, actor: str, t: int) -> dict[str, str | None]:
        """One decision for several pending proposals (a whole street). Each is still decided on its own,
        with its own checks and audit record; returns pid -> new status, or None where it could not be done."""
        pids = list(dict.fromkeys(pids))[:GROUP_MAX]
        results = {}
        for pid in pids:
            p = self.decide(pid, approve, actor, t)
            results[pid] = p["status"] if p else None
        done = [pid for pid, st in results.items() if st]
        if done:
            command = self.proposals[done[0]]["command"]
            self.audit.append(t, "group-approved" if approve else "group-rejected", ",".join(done), command,
                              f"{len(done)} junctions", "", actor=actor, mode="approval")
        return results

    def unsettled(self) -> list[dict]:
        """Proposals whose record may still change: the recent ones and everything still open."""
        recent = self.public()
        seen = {p["id"] for p in recent}
        return recent + [p for p in self.proposals.values() if p["id"] not in seen and p["status"] in (*OPEN, "revoking")]

    @staticmethod
    def _target(p: dict) -> str:
        return f"{p['tls']}: plan {p['plan']}"

    def _lease(self, p: dict, pol: Policy):
        self.send({"op": "lease", "id": p["id"], "tls": p["tls"], "plan": p["plan"], "ttl": pol.lease_ttl})


# ---- grouping proposals by street
_APOSTROPHES = re.compile(r"[ʻʼ‘’`´']")


def street_names(text: str) -> set[str]:
    """'Tadbirkorlar koʻchasi, Universitet ko'chasi' -> {'tadbirkorlar ko'chasi', 'universitet ko'chasi'}:
    OSM spells the same street with different apostrophes, and an approach may carry two names."""
    return {_APOSTROPHES.sub("'", n).strip().lower() for n in (text or "").split(",") if n.strip()}


def street_groups(pending: list[dict]) -> dict[str, dict]:
    """Pending proposals that share a street (directly or through another one) form a group.
    Returns pid -> {group, group_street, group_size} for groups of two or more."""
    parent = {p["id"]: p["id"] for p in pending}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    owner: dict[str, str] = {}
    for p in pending:
        for name in street_names(p.get("street", "")):
            if name in owner:
                parent[find(p["id"])] = find(owner[name])
            else:
                owner[name] = p["id"]
    members: dict[str, list[dict]] = {}
    for p in pending:
        members.setdefault(find(p["id"]), []).append(p)
    out = {}
    for ps in members.values():
        if len(ps) < 2:
            continue
        counts: dict[str, int] = {}
        for q in ps:
            for name in street_names(q.get("street", "")):
                counts[name] = counts.get(name, 0) + 1
        common = max(counts, key=lambda n: (counts[n], n))
        label = next(n.strip() for q in ps for n in q["street"].split(",")
                     if _APOSTROPHES.sub("'", n).strip().lower() == common)
        key = min(q["id"] for q in ps)
        for q in ps:
            out[q["id"]] = {"group": key, "group_street": label, "group_size": len(ps)}
    return out
