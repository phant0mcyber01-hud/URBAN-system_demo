"""Append-only audit log with a hash chain: each record commits to the previous one,
so editing or deleting any retained record breaks verification.

With a store the chain is persistent: the log resumes from the stored head at start, every
record is written through, and only a tail stays in memory. Records carry both the scenario
time `t` and the wall-clock time `at`."""
import hashlib
import json
import time

GENESIS = "0" * 64


def _digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AuditLog:
    def __init__(self, keep: int = 300, store=None):
        self.store = store
        self._keep = keep
        if store is not None:
            self.records = store.audit_tail(keep)
            self.count, self.head = store.audit_head()
        else:
            self.records: list[dict] = []
            self.count, self.head = 0, GENESIS

    def append(self, t, event, ref, command, target, note="", actor="platform", executed=False, mode="shadow") -> dict:
        rec = {"seq": self.count + 1, "t": t, "at": round(time.time(), 3), "event": event, "actor": actor, "mode": mode,
               "executed": executed, "ref": ref, "command": command, "target": target, "note": note, "prev": self.head}
        rec["hash"] = _digest(rec)
        self.count, self.head = rec["seq"], rec["hash"]
        self.records = (self.records + [rec])[-self._keep:]
        if self.store is not None:
            self.store.audit_append(rec)
        return rec

    def verify(self) -> bool:
        """True if the retained part of the chain is intact (each record matches its hash and its predecessor)."""
        prev = self.records[0]["prev"] if self.records else self.head
        for rec in self.records:
            if rec["prev"] != prev or rec["hash"] != _digest(rec):
                return False
            prev = rec["hash"]
        return prev == self.head
