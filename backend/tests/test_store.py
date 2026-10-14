import json

from urban.gateway import CommandGateway
from urban.gateway.audit import GENESIS, AuditLog
from urban.store import Store


def test_audit_chain_continues_across_restarts(tmp_path):
    s1 = Store(tmp_path / "h.db", clock=lambda: 1000.0)
    log = AuditLog(store=s1)
    first = log.append(1, "proposed", "cmd-1", "tls.select_plan", "J: plan 2")
    assert first["prev"] == GENESIS and "at" in first
    last = log.append(2, "approved", "cmd-1", "tls.select_plan", "J: plan 2", actor="ops")
    s1.close()

    s2 = Store(tmp_path / "h.db", clock=lambda: 2000.0)
    log2 = AuditLog(keep=1, store=s2)
    assert (log2.count, log2.head) == (2, last["hash"]) and len(log2.records) == 1  # only the tail stays in memory
    third = log2.append(3, "released", "cmd-1", "tls.select_plan", "J: plan 2", "relieved")
    assert third["prev"] == last["hash"] and third["seq"] == 3
    assert log2.verify()
    assert s2.audit_verify() == {"records": 3, "intact": True, "broken_at": None, "head": third["hash"]}
    assert [r["event"] for r in s2.audit_page(limit=10)] == ["released", "approved", "proposed"]
    assert [r["run"] for r in s2.audit_page(limit=10)] == [2, 1, 1]
    assert [r["event"] for r in s2.audit_page(before=2, limit=10)] == ["proposed"]


def test_tampering_with_the_file_is_detected(tmp_path):
    s = Store(tmp_path / "h.db")
    log = AuditLog(store=s)
    for i in range(4):
        log.append(i, "proposed", f"cmd-{i}", "tls.select_plan", "J")
    row = json.loads(s.db.execute("SELECT data FROM audit WHERE seq = 2").fetchone()[0])
    row["target"] = "other junction"
    s.db.execute("UPDATE audit SET data = ? WHERE seq = 2", (json.dumps(row),))
    v = s.audit_verify()
    assert not v["intact"] and v["broken_at"] == 2
    s.db.execute("DELETE FROM audit WHERE seq = 3")
    assert s.audit_verify()["broken_at"] == 2


def test_commands_are_upserted_and_queried(tmp_path):
    clock = [100.0]
    s = Store(tmp_path / "h.db", clock=lambda: clock[0])
    p = {"id": "cmd-1", "t": 5, "tls": "J1", "plan": 2, "status": "pending"}
    s.save_command(p)
    p["status"], p["actor"] = "approved", "ops"
    clock[0] = 101.0
    s.save_command(p)
    clock[0] = 102.0
    s.save_command({"id": "cmd-2", "t": 9, "tls": "J2", "plan": 3, "status": "pending"})
    assert s.counts()["commands"] == 2
    rows = s.commands()
    assert [r["id"] for r in rows] == ["cmd-2", "cmd-1"] and rows[1]["status"] == "approved" and rows[1]["actor"] == "ops"
    assert [r["id"] for r in s.commands(tls="J1")] == ["cmd-1"]
    assert [r["id"] for r in s.commands(before=102.0)] == ["cmd-1"]
    assert s.commands(limit=1)[0]["id"] == "cmd-2"


def test_samples_and_runs(tmp_path):
    clock = [10.0]
    s = Store(tmp_path / "h.db", clock=lambda: clock[0])
    s.add_device_samples([{"id": "edge-1", "junction": "J1", "online": True, "status": "active",
                           "health": {"temp": 41.5, "door": "closed", "controller": "ok", "leases": 1}},
                          {"id": "edge-2", "junction": "J2", "online": False, "status": "active", "health": None}])
    s.add_queues({"J1": {"n": 12.34, "s": 0}, "J2": None})
    clock[0] = 20.0
    s.add_queues({"J1": {"n": 30, "s": 1}})
    assert s.device_samples("edge-1", 0)[0]["temp"] == 41.5 and s.device_samples("edge-2", 0)[0]["online"] == 0
    assert [q["metres"] for q in s.queues("J1", 0)] == [12.3, 0, 30, 1]
    assert [q["metres"] for q in s.queues("J1", 15)] == [30, 1]
    assert s.queues("J2", 0) == []
    s.start_run(note="scenario restarted")
    runs = s.runs()
    assert [r["id"] for r in runs] == [2, 1] and runs[0]["note"] == "scenario restarted" and runs[1]["mode"] == "simulation"
    assert Store(tmp_path / "h.db").runs()[0]["id"] == 3  # a new process is a new run


def test_gateway_records_who_decided(tmp_path):
    s = Store(tmp_path / "h.db")
    sent = []
    gw = CommandGateway(send=sent.append, plans={"J1": {2}}, store=s)
    p = gw.propose(10, "tls.select_plan", {"tls": "J1", "plan": 2, "give": 0, "take": 1}, "why")
    gw.decide(p["id"], True, "ops", 11)
    assert p["actor"] == "ops" and gw.audit.records[-1]["actor"] == "ops"
    assert s.audit_verify()["records"] == 2
