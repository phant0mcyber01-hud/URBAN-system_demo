from urban.analytics.queues import HOLD, QUEUE_MIN, QueueMonitor
from urban.core.events import Vehicle
from urban.platform import Platform

CAM = {"id": "CAM-01", "lon": 71.785, "lat": 40.385, "tls": "J1", "name": "Crossing",
       "approaches": [{"id": "e1", "name": "Main street"}, {"id": "e2", "name": "Side"}]}


def frame(t, vehicles=None, **extra):
    base = [Vehicle("car1", 71.785, 40.385, 90, 10, "car", "e9"), Vehicle("far", 72.5, 40.385, 0, 10, "car", "e8")]
    return {"t": t, "vehicles": vehicles if vehicles is not None else base, "bus_loss": [], **extra}


def queue_of(n, edge="e1"):
    return [Vehicle(f"q{i}", 71.785, 40.385, 0, 0.0, "car", edge) for i in range(n)]


def test_frame_keeps_only_vehicles_in_scene_as_local_decimetres():
    out = Platform([71.785, 40.385], 1200).handle(frame(1))
    assert out["v"] == [["car1", 0, 0, 90, 10, "car"]]
    assert out["stats"]["cars"] == 2


def test_queue_must_persist_before_it_becomes_a_problem():
    mon = QueueMonitor([CAM])
    raised = []
    for t in range(HOLD + 3):
        _, problems = mon.update(t, queue_of(QUEUE_MIN))
        raised += [(t, p["approach"]) for p in problems]
    assert raised == [(HOLD, "e1")]                 # once, after HOLD seconds, not again while it persists
    assert mon.update(HOLD + 5, queue_of(2))[1] == []
    assert mon.update(HOLD + 6, queue_of(QUEUE_MIN))[1] == []   # needs to persist again


def test_moving_traffic_is_not_a_queue():
    fast = [Vehicle(f"f{i}", 0, 0, 0, 12.0, "car", "e1") for i in range(20)]
    mon = QueueMonitor([CAM])
    assert all(mon.update(t, fast)[1] == [] for t in range(40))


def test_platform_turns_a_queue_into_a_pending_proposal_and_sends_nothing():
    sent = []
    p = Platform([71.785, 40.385], 1200, [CAM], send=sent.append)
    for t in range(HOLD + 2):
        out = p.handle(frame(t, queue_of(8)))
    cmds = [c for c in p.gateway.public() if c["status"] == "pending"]
    assert len(cmds) == 1 and cmds[0]["approach"] == "e1" and cmds[0]["tls"] == "J1"
    assert sent == [] and p.gateway.applied == 0
    assert out["cams"]["CAM-01"][2] == 8 and out["cmd"][0]["status"] == "pending"


def test_equipment_reports_move_the_command_forward_and_stay_out_of_the_city_feed():
    sent = []
    p = Platform([71.785, 40.385], 1200, [CAM], send=sent.append)
    for t in range(HOLD + 2):
        p.handle(frame(t, queue_of(8)))
    cmd = p.gateway.public()[0]
    p.gateway.decide(cmd["id"], True, "operator", p.t)
    out = p.handle(frame(p.t + 1, queue_of(8), events=[{"type": "tls", "id": cmd["id"], "status": "applied", "seconds": 10}]))
    assert out["events"] == [] and p.gateway.public()[0]["status"] == "active" and p.gateway.applied == 1


def test_incident_dispatch_stays_shadow_only_and_restart_resets_state():
    p = Platform([71.785, 40.385], 1200)
    ev = {"id": "d1", "type": "dispatch", "status": "en_route", "t": 5, "unit": "Скорая", "from": "Клиника", "ref": "inc-5"}
    out = p.handle(frame(5, events=[ev]))
    assert out["shadow"][0]["command"] == "tls.preempt" and out["shadow"][0]["executed"] is False
    assert p.snapshot()["events"] == [ev]
    again = p.handle(frame(0))
    assert again["reset"] is True and p.snapshot()["events"] == []
    assert p.audit()["count"] == 1  # the audit log survives restarts
