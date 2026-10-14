"""Persistent history: one SQLite file (data/urban.db) that outlives restarts.

What is kept and why:
  runs            every start of the platform (and every scenario restart in simulation), so records
                  from different runs can be told apart;
  audit           the whole hash-chained audit log; the chain continues across restarts and the
                  file is verified end to end at start;
  commands        every proposal with its decision, who decided and how it ended;
  device_samples  health of each URBAN Edge unit once a minute (temperature, door, controller, leases);
  queues          mean queue length per approach once a minute, the raw material for later analysis.
Queue and device samples are pruned after `retention_days`; the audit log and the commands are never
deleted. Rows are written by the analysis thread and read by the API thread; one lock guards the connection.
Each audit and command row also carries the record as JSON, so a record read back is byte-for-byte
what was hashed.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from .gateway.audit import GENESIS, _digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started REAL NOT NULL, mode TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY, run INTEGER NOT NULL, at REAL NOT NULL, event TEXT NOT NULL,
    actor TEXT NOT NULL, ref TEXT, target TEXT, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(run INTEGER NOT NULL, id TEXT NOT NULL, at REAL NOT NULL, tls TEXT NOT NULL,
    status TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '', data TEXT NOT NULL, PRIMARY KEY(run, id));
CREATE INDEX IF NOT EXISTS commands_tls ON commands(tls, at);
CREATE TABLE IF NOT EXISTS device_samples(at REAL NOT NULL, run INTEGER NOT NULL, device TEXT NOT NULL, junction TEXT,
    online INTEGER NOT NULL, status TEXT, temp REAL, door TEXT, controller TEXT, leases INTEGER);
CREATE INDEX IF NOT EXISTS device_samples_device ON device_samples(device, at);
CREATE TABLE IF NOT EXISTS queues(at REAL NOT NULL, run INTEGER NOT NULL, junction TEXT NOT NULL, approach TEXT NOT NULL, metres REAL NOT NULL);
CREATE INDEX IF NOT EXISTS queues_junction ON queues(junction, at);
"""


class Store:
    def __init__(self, path: Path | str, mode: str = "simulation", clock=time.time, retention_days: int = 90):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.mode = mode
        self.retention = retention_days * 86400
        self._pruned = None
        self._counts: tuple[float, dict] | None = None
        self.run = 0
        self.start_run(mode, "platform started")
        seq, head = self.audit_head()
        # what is known about the chain before anyone verified it: length and head only
        self.chain = {"records": seq, "intact": None, "broken_at": None, "head": head, "checked": None}
        self._verified = (0, GENESIS)  # (seq, hash) up to which the chain was last verified

    def close(self):
        with self.lock:
            self.db.close()

    # ---- runs
    def start_run(self, mode: str | None = None, note: str = "") -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO runs(started, mode, note) VALUES (?, ?, ?)", (self.clock(), mode or self.mode, note))
            self.run = cur.lastrowid
        return self.run

    def runs(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT r.id, r.started, r.mode, r.note, (SELECT COUNT(*) FROM commands c WHERE c.run = r.id) AS commands, "
                "(SELECT COUNT(*) FROM audit a WHERE a.run = r.id) AS audit FROM runs r ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("id", "started", "mode", "note", "commands", "audit"), r)) for r in rows]

    # ---- audit: the chain lives here, AuditLog keeps only a tail in memory
    def audit_head(self) -> tuple[int, str]:
        with self.lock:
            row = self.db.execute("SELECT seq, data FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1])["hash"]) if row else (0, GENESIS)

    def audit_tail(self, n: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT data FROM audit ORDER BY seq DESC LIMIT ?", (n,)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def audit_append(self, rec: dict):
        with self.lock:
            self.db.execute("INSERT INTO audit(seq, run, at, event, actor, ref, target, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (rec["seq"], self.run, rec.get("at", self.clock()), rec["event"], rec["actor"],
                             str(rec["ref"]), str(rec["target"]), json.dumps(rec, ensure_ascii=False)))

    def audit_after(self, seq: int, limit: int = 500) -> list[dict]:
        """Records with a higher sequence number, oldest first: what an exporter has not shipped yet."""
        with self.lock:
            rows = self.db.execute("SELECT data FROM audit WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def audit_page(self, before: int | None = None, limit: int = 100) -> list[dict]:
        """Newest first; `before` is a seq to page from."""
        with self.lock:
            rows = self.db.execute("SELECT run, data FROM audit WHERE seq < ? ORDER BY seq DESC LIMIT ?",
                                   (before if before is not None else 2 ** 62, limit)).fetchall()
        return [{**json.loads(d), "run": run} for run, d in rows]

    def _walk(self, start_seq: int, prev: str) -> dict:
        """Verify records after start_seq, continuing a chain whose hash at start_seq is `prev`."""
        n, broken = start_seq, None
        with self.lock:
            rows = self.db.execute("SELECT seq, data FROM audit WHERE seq > ? ORDER BY seq", (start_seq,)).fetchall()
        for seq, data in rows:
            rec = json.loads(data)
            if rec["seq"] != n + 1 or rec["prev"] != prev or rec["hash"] != _digest(rec):
                broken = seq
                break
            prev, n = rec["hash"], seq
        return {"records": n, "intact": broken is None, "broken_at": broken, "head": prev}

    def audit_verify(self) -> dict:
        """Walk the whole stored chain: every record must match its hash and point at its predecessor.
        Costs a pass over the table; the platform runs it in the background, not on a request."""
        result = self._walk(0, GENESIS)
        self._remember(result)
        return result

    def audit_check(self) -> dict:
        """Cheap check on request: only the records added since the last verification are walked.
        A chain already found broken stays broken until a full verification says otherwise."""
        if self.chain["intact"] is False:
            return self.chain
        seq, prev = self._verified
        result = self._walk(seq, prev)
        self._remember(result)
        return self.chain

    def _remember(self, result: dict):
        self.chain = {**result, "checked": self.clock()}
        if result["intact"]:
            self._verified = (result["records"], result["head"])

    # ---- commands
    def save_command(self, p: dict):
        with self.lock:
            self.db.execute(
                "INSERT INTO commands(run, id, at, tls, status, actor, data) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run, id) DO UPDATE SET status = excluded.status, actor = excluded.actor, data = excluded.data",
                (self.run, p["id"], self.clock(), p["tls"], p["status"], p.get("actor", ""), json.dumps(p, ensure_ascii=False)))

    def commands(self, tls: str | None = None, before: float | None = None, limit: int = 50) -> list[dict]:
        """Newest first by the time the proposal was made; `before` pages by that time."""
        where, args = ["at < ?"], [before if before is not None else 2 ** 62]
        if tls:
            where.append("tls = ?")
            args.append(tls)
        with self.lock:
            rows = self.db.execute(f"SELECT run, at, data FROM commands WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT ?",
                                   (*args, limit)).fetchall()
        return [{**json.loads(d), "run": run, "at": at} for run, at, d in rows]

    # ---- samples
    def add_device_samples(self, rows: list[dict]):
        """rows: the hub's status rows (id, junction, online, status, health{...})."""
        at = self.clock()
        with self.lock:
            self.db.executemany(
                "INSERT INTO device_samples(at, run, device, junction, online, status, temp, door, controller, leases) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(at, self.run, r["id"], r.get("junction"), int(bool(r.get("online"))), r.get("status"),
                  (r.get("health") or {}).get("temp"), (r.get("health") or {}).get("door"),
                  (r.get("health") or {}).get("controller"), (r.get("health") or {}).get("leases")) for r in rows])

    def device_samples(self, device: str, since: float) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT at, online, status, temp, door, controller, leases FROM device_samples "
                                   "WHERE device = ? AND at >= ? ORDER BY at", (device, since)).fetchall()
        return [dict(zip(("at", "online", "status", "temp", "door", "controller", "leases"), r)) for r in rows]

    def add_queues(self, queues: dict[str, dict | None]):
        """queues: junction id -> {approach id: mean metres} (None for a junction without data)."""
        at = self.clock()
        with self.lock:
            self.db.executemany("INSERT INTO queues(at, run, junction, approach, metres) VALUES (?, ?, ?, ?, ?)",
                                [(at, self.run, j, a, round(float(m), 1)) for j, per in queues.items() if per
                                 for a, m in per.items()])
        self.prune(at)

    def prune(self, now: float | None = None, every: float = 3600.0):
        """Drop samples older than the retention; runs at most once an hour. Audit and commands are kept."""
        now = self.clock() if now is None else now
        if self._pruned is not None and 0 <= now - self._pruned < every:
            return
        self._pruned = now
        with self.lock:
            for table in ("queues", "device_samples"):
                self.db.execute(f"DELETE FROM {table} WHERE at < ?", (now - self.retention,))

    def queues(self, junction: str, since: float) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT at, approach, metres FROM queues WHERE junction = ? AND at >= ? ORDER BY at",
                                   (junction, since)).fetchall()
        return [dict(zip(("at", "approach", "metres"), r)) for r in rows]

    def counts(self, max_age: float = 30.0) -> dict:
        """Row counts, cached for `max_age` seconds: COUNT(*) walks the table and the History tab asks often."""
        now = self.clock()
        if self._counts is not None and 0 <= now - self._counts[0] < max_age:
            return self._counts[1]
        with self.lock:
            counts = {name: self.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                      for name in ("runs", "audit", "commands", "device_samples", "queues")}
        self._counts = (now, counts)
        return counts
