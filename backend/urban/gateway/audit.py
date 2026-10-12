"""Append-only audit log with a hash chain: each record commits to the previous one,
so editing or deleting any retained record breaks verification."""
import hashlib
import json

GENESIS = "0" * 64


def _digest(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AuditLog:
    def __init__(self, keep: int = 300):
        self.records: list[dict] = []
        self.count = 0
        self.head = GENESIS
        self._keep = keep

    def append(self, t, event, ref, command, target, note="", actor="platform", executed=False, mode="shadow") -> dict:
        rec = {"seq": self.count + 1, "t": t, "event": event, "actor": actor, "mode": mode, "executed": executed,
               "ref": ref, "command": command, "target": target, "note": note, "prev": self.head}
        rec["hash"] = _digest(rec)
        self.count, self.head = rec["seq"], rec["hash"]
        self.records = (self.records + [rec])[-self._keep:]
        return rec

    def verify(self) -> bool:
        """True if the retained part of the chain is intact (each record matches its hash and its predecessor)."""
        prev = self.records[0]["prev"] if self.records else self.head
        for rec in self.records:
            if rec["prev"] != prev or rec["hash"] != _digest(rec):
                return False
            prev = rec["hash"]
        return prev == self.head
