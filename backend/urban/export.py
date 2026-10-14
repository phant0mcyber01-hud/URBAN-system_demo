"""External copy of the audit log, with signed checkpoints.

The hash chain in the database catches an edited record, but someone with full control of the
server could rebuild the whole chain from the first record. What stops that is a copy the server
cannot take back: every audit record is shipped off the machine as it is written, and every few
minutes a checkpoint {seq, head, at} signed with the audit key goes along. A rewritten database
then disagrees with records and signatures that already left.

Sinks (configured in .env, any number of them):
  AUDIT_EXPORT=/mnt/audit/urban-audit.jsonl   a file on another disk or a network share, one JSON per line
  AUDIT_SYSLOG=tls://collector:6514           RFC 5424 syslog: tls:// (RFC 5425, AUDIT_SYSLOG_CA for a private CA),
                                              tcp:// (RFC 6587 octet counting) or udp://
The exporter reads from the store, never from the hot path: if a sink is down it catches up later,
from the last sequence number it confirmed (kept per sink).

Check a copy against the database:  python -m urban.export verify <copy.jsonl> [--db data/urban.db]
"""
import argparse
import asyncio
import datetime
import json
import os
import socket
import ssl
import sys
from pathlib import Path

from .devices import crypto
from .gateway.audit import GENESIS, _digest

CHECKPOINT_EVERY = 300  # seconds between signed checkpoints
BATCH = 5000            # records per pass (every 5 s)


def checkpoint(key, seq: int, head: str, at: float) -> dict:
    body = {"seq": seq, "head": head, "at": round(at, 3)}
    return {"type": "checkpoint", **body, "sig": crypto.sign(key, body)}


def checkpoint_ok(public, cp: dict) -> bool:
    return crypto.verify(public, {"seq": cp.get("seq"), "head": cp.get("head"), "at": cp.get("at")}, str(cp.get("sig")))


# ---- sinks
class FileSink:
    """Append-only JSON lines. The position is the last record line in the file itself."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.name = f"file:{self.path}"

    def position(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with self.path.open("rb") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # a torn last line after a crash: rewritten on the next pass
                if "hash" in obj:
                    last = obj["seq"]
        return last

    def write(self, lines: list[dict]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            for obj in lines:
                f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())


class SyslogSink:
    """RFC 5424 to a collector. It cannot be read back, so the position is kept in a small local state file."""

    def __init__(self, target: str, state: Path, host: str | None = None, cafile: str | None = None):
        proto, _, rest = target.partition("://")
        if not rest:
            proto, rest = "udp", target
        if proto not in ("udp", "tcp", "tls"):
            raise ValueError("AUDIT_SYSLOG must be tls://, tcp:// or udp://host:port")
        self.cafile = cafile
        address, _, port = rest.rpartition(":")
        self.proto, self.address = proto, address.strip("[]") or rest
        self.port = int(port or (6514 if proto == "tls" else 514))
        self.state = Path(state)
        self.host = host or socket.gethostname()
        self.name = f"syslog:{target}"

    def position(self) -> int:
        try:
            return int(self.state.read_text())
        except (OSError, ValueError):
            return 0

    def message(self, obj: dict) -> bytes:
        at = obj.get("at") or 0
        stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat(timespec="milliseconds")
        msgid = "checkpoint" if obj.get("type") == "checkpoint" else "audit"
        # facility 13 (log audit), severity 6 (informational): PRI = 13 * 8 + 6
        return f"<110>1 {stamp} {self.host} urban - {msgid} - {json.dumps(obj, ensure_ascii=False, separators=(',', ':'))}".encode()

    def write(self, lines: list[dict]):
        if self.proto == "udp":
            with socket.socket(socket.AF_INET6 if ":" in self.address else socket.AF_INET, socket.SOCK_DGRAM) as s:
                for obj in lines:
                    s.sendto(self.message(obj), (self.address, self.port))
        else:
            with socket.create_connection((self.address, self.port), timeout=5) as raw:
                s = raw
                if self.proto == "tls":  # the collector's certificate and name are checked
                    s = ssl.create_default_context(cafile=self.cafile).wrap_socket(raw, server_hostname=self.address)
                for obj in lines:
                    m = self.message(obj)
                    s.sendall(f"{len(m)} ".encode() + m)  # octet counting (RFC 6587, RFC 5425)
                _close_gracefully(s, raw)
        last = max((o["seq"] for o in lines if "hash" in o), default=None)
        if last is not None:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            self.state.write_text(str(last))


def _close_gracefully(s, raw):
    """End the connection so the collector reads everything: a close with unread bytes (TLS 1.3 session
    tickets, for one) turns into a reset on some systems, and a reset can discard what was just sent."""
    try:
        if s is not raw:
            s.unwrap()  # close_notify both ways
        else:
            raw.shutdown(socket.SHUT_WR)
            while raw.recv(4096):
                pass
    except (OSError, ssl.SSLError):
        pass
    finally:
        s.close()


# ---- the exporter
class Exporter:
    def __init__(self, store, key, sinks: list, clock=None, every: int = CHECKPOINT_EVERY):
        import time
        self.store, self.key, self.sinks = store, key, sinks
        self.clock = clock or time.time
        self.every = every
        self.positions = {s.name: None for s in sinks}
        self.errors: dict[str, str] = {}
        self.last_checkpoint: dict | None = None
        self._checkpointed_at = None

    def run_once(self):
        """Ship what each sink has not confirmed yet, and a signed checkpoint when one is due."""
        due = self._checkpointed_at is None or self.clock() - self._checkpointed_at >= self.every
        head_seq, head = self.store.audit_head()
        cp = checkpoint(self.key, head_seq, head, self.clock()) if due and head_seq else None
        for sink in self.sinks:
            try:
                pos = self.positions[sink.name]
                if pos is None:
                    pos = sink.position()
                records = self.store.audit_after(pos, BATCH)  # a long backlog goes out over several passes
                lines = records + ([cp] if cp is not None else [])
                if lines:
                    sink.write(lines)  # one write per pass: one connection for network sinks
                if records:
                    pos = records[-1]["seq"]
                self.positions[sink.name] = pos
                self.errors.pop(sink.name, None)
            except (OSError, ValueError) as err:
                self.positions[sink.name] = None  # re-read the position next time
                self.errors[sink.name] = f"{err.__class__.__name__}: {err}"
        if cp is not None and not self.errors:
            self.last_checkpoint, self._checkpointed_at = cp, self.clock()

    def status(self) -> dict:
        return {"sinks": [{"name": s.name, "seq": self.positions.get(s.name), "error": self.errors.get(s.name)}
                          for s in self.sinks],
                "checkpoint": {k: self.last_checkpoint[k] for k in ("seq", "at")} if self.last_checkpoint else None}

    async def loop(self, period: float = 5.0):
        while True:
            await asyncio.to_thread(self.run_once)
            await asyncio.sleep(period)


def sinks_from_env(state_dir: Path) -> list:
    out = []
    if path := os.getenv("AUDIT_EXPORT", "").strip():
        out.append(FileSink(path))
    if target := os.getenv("AUDIT_SYSLOG", "").strip():
        out.append(SyslogSink(target, state_dir / "audit-syslog.pos", cafile=os.getenv("AUDIT_SYSLOG_CA") or None))
    return out


# ---- checking a copy
def verify_copy(lines: list[dict], public, db_records: dict[int, str] | None = None) -> dict:
    """lines: the copy as parsed JSON; db_records: seq -> hash from the database, to compare against."""
    problems, prev, expected, hashes, checkpoints = [], GENESIS, 1, {}, 0
    for obj in lines:
        if obj.get("type") == "checkpoint":
            checkpoints += 1
            if not checkpoint_ok(public, obj):
                problems.append(f"checkpoint at #{obj.get('seq')}: bad signature")
            elif hashes.get(obj["seq"]) not in (None, obj["head"]):
                problems.append(f"checkpoint at #{obj['seq']}: head does not match the copied record")
            elif db_records is not None and db_records.get(obj["seq"]) not in (None, obj["head"]):
                problems.append(f"checkpoint at #{obj['seq']}: the database has a different record there")
            continue
        if "hash" not in obj:
            continue
        if obj["seq"] < expected:
            continue  # sent twice after a reconnect: fine if identical
        if obj["seq"] != expected or obj["prev"] != prev or obj["hash"] != _digest(obj):
            problems.append(f"copy broken at #{obj['seq']}")
            break
        hashes[obj["seq"]] = obj["hash"]
        prev, expected = obj["hash"], obj["seq"] + 1
    if db_records is not None:
        differ = [s for s, h in hashes.items() if db_records.get(s) not in (None, h)]
        missing = [s for s in hashes if s not in db_records]
        if differ:
            problems.append(f"the database differs from the copy at {len(differ)} record(s), first #{min(differ)}")
        if missing:
            problems.append(f"{len(missing)} record(s) in the copy are missing from the database, first #{min(missing)}")
    return {"records": len(hashes), "checkpoints": checkpoints, "problems": problems, "ok": not problems}


def main():
    ap = argparse.ArgumentParser(description="URBAN audit export tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="check an exported copy, and the database against it")
    v.add_argument("copy")
    v.add_argument("--db", help="data/urban.db to compare with")
    v.add_argument("--pub", help="audit.pub (default: data/pki/audit.pub)")
    args = ap.parse_args()
    from . import config as c
    public = crypto.load_public(Path(args.pub or c.PKI / "audit.pub").read_text())
    lines = []
    for raw in Path(args.copy).read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            pass
    db = None
    if args.db:
        import sqlite3
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        db = {seq: json.loads(d)["hash"] for seq, d in conn.execute("SELECT seq, data FROM audit")}
    result = verify_copy(lines, public, db)
    print(f"copy: {result['records']} records, {result['checkpoints']} signed checkpoints")
    for p in result["problems"]:
        print("PROBLEM:", p)
    print("OK" if result["ok"] else "NOT OK")
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
