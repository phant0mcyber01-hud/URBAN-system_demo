from urban.devices.health import EXPECTED_VERSION, TEMP_ALERT, TEMP_WARN, assess, blocking
from urban.platform import Platform

CAM = {"id": "CAM-01", "camera": True, "lon": 71.785, "lat": 40.385, "tls": "J1", "name": "Crossing",
       "approaches": [{"id": "e1", "name": "Main", "zone": 100.0}], "phases": [], "plans": []}


def row(**over):
    base = {"id": "edge-CAM-01", "junction": "CAM-01", "tls": "J1", "name": "Crossing", "status": "active",
            "online": True, "age": 1, "connects": 1,
            "health": {"version": EXPECTED_VERSION, "temp": 40.0, "door": "closed", "controller": "ok", "leases": 0}}
    base.update(over)
    return base


def kinds(r):
    return [a["kind"] for a in assess(r)]


def test_healthy_unit_raises_nothing():
    assert assess(row()) == []


def test_session_and_report_age():
    assert kinds(row(online=False, age=200)) == ["offline"]
    assert "мин" in assess(row(online=False, age=200))[0]["text"]
    assert kinds(row(age=30)) == ["silent"]
    assert kinds(row(status="revoked", online=False)) == ["revoked"]


def test_temperature_door_controller_and_version():
    assert kinds(row(health={"temp": TEMP_WARN + 1})) == ["heat"] and assess(row(health={"temp": TEMP_WARN + 1}))[0]["level"] == "warn"
    assert assess(row(health={"temp": TEMP_ALERT}))[0]["level"] == "alert"
    assert kinds(row(health={"door": "open"})) == ["door"]
    assert kinds(row(health={"controller": "field link is down"})) == ["controller"]
    assert kinds(row(health={"version": "0.0.9"})) == ["version"]
    assert kinds(row(health={"temp": "hot"})) == []                  # junk is not an alert


def test_alerts_are_sorted_most_severe_first_and_blocking_is_about_trust():
    alerts = assess(row(health={"version": "0.0.9", "door": "open", "temp": TEMP_WARN + 1}))
    assert [a["level"] for a in alerts] == ["alert", "warn", "warn"]
    assert blocking(alerts) and not blocking(assess(row(health={"temp": TEMP_ALERT})))


def test_platform_reports_new_alerts_once_and_locks_an_opened_cabinet():
    sent = []
    p = Platform([71.785, 40.385], 1200, [CAM], send=sent.append)
    p.gateway.plans = {"J1": {0, 1}}
    cmd = p.gateway.propose(0, "tls.select_plan", {"tls": "J1", "cam": "CAM-01", "plan": 1}, "demo")
    p.gateway.decide(cmd["id"], True, "operator", 1)
    assert p.devices([row()], 2) == []
    new = p.devices([row(health={"door": "open", "temp": 40.0})], 3)
    assert [a["kind"] for a in new] == ["door"] and p.dev["CAM-01"]["lock"] is True
    assert cmd["status"] == "revoking" and cmd["end"] == "locked"   # the plan is pulled back at once
    assert sent[-1]["op"] == "release"
    assert p.devices([row(health={"door": "open"})], 4) == []        # still open: not reported again
    assert p.gateway.propose(5, "tls.select_plan", {"tls": "J1", "cam": "CAM-01", "plan": 1}, "demo") is None
    assert p.devices([row()], 6) == [] and "J1" not in p.gateway.locked
    events = [r["event"] for r in p.gateway.records]
    assert "junction-locked" in events and "junction-unlocked" in events and "device-clear" in events


def test_overheating_is_reported_but_does_not_lock():
    p = Platform([71.785, 40.385], 1200, [CAM])
    new = p.devices([row(health={"temp": TEMP_ALERT + 1})], 1)
    assert new[0]["level"] == "alert" and "J1" not in p.gateway.locked
    assert p.dev["CAM-01"]["t"] == TEMP_ALERT + 1 and p.snapshot()["devalerts"] == new


def test_worsening_trouble_is_reported_again_at_the_new_level():
    p = Platform([71.785, 40.385], 1200, [CAM])
    assert [a["level"] for a in p.devices([row(health={"temp": TEMP_WARN + 1})], 1)] == ["warn"]
    assert p.devices([row(health={"temp": TEMP_WARN + 2})], 2) == []
    assert [a["level"] for a in p.devices([row(health={"temp": TEMP_ALERT + 1})], 3)] == ["alert"]
    assert p.devices([row()], 4) == [] and p.gateway.records[-1]["event"] == "device-clear"
