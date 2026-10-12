from urban.analytics.queues import LOADED_MIN, WINDOW, QueueMonitor, shift_seconds
from urban.core.events import Vehicle
from urban.platform import Platform

CAM = {"id": "CAM-01", "lon": 71.785, "lat": 40.385, "tls": "J1", "name": "Crossing",
       "approaches": [{"id": "e1", "name": "Main street"}, {"id": "e2", "name": "Side street"}],
       "phases": [{"index": 0, "duration": 40, "edges": ["e1"], "streets": ["Main street"]},
                  {"index": 2, "duration": 40, "edges": ["e2"], "streets": ["Side street"]}]}


def frame(t, vehicles=None, **extra):
    base = [Vehicle("car1", 71.785, 40.385, 90, 10, "car", "e9"), Vehicle("far", 72.5, 40.385, 0, 10, "car", "e8")]
    return {"t": t, "vehicles": vehicles if vehicles is not None else base, "bus_loss": [], **extra}


def standing(main, side=0):
    return ([Vehicle(f"m{i}", 71.785, 40.385, 0, 0.0, "car", "e1") for i in range(main)]
            + [Vehicle(f"s{i}", 71.785, 40.385, 0, 0.0, "car", "e2") for i in range(side)])


def feed(target, vehicles, start=0, seconds=WINDOW + 1):
    out = None
    for t in range(start, start + seconds):
        out = target(t, vehicles)
    return out


def test_frame_keeps_only_vehicles_in_scene_as_local_decimetres():
    out = Platform([71.785, 40.385], 1200).handle(frame(1))
    assert out["v"] == [["car1", 0, 0, 90, 10, "car"]]
    assert out["stats"]["cars"] == 2


def test_no_decision_before_a_full_cycle_of_history():
    mon = QueueMonitor([CAM])
    assert feed(mon.update, standing(12), seconds=WINDOW - 1)[1] == []
    assert mon.demand("CAM-01") == {}


def test_overloaded_direction_with_a_free_cross_street_is_a_problem():
    mon = QueueMonitor([CAM])
    _, problems = feed(mon.update, standing(10, 1))
    assert len(problems) == 1
    pr = problems[0]
    assert (pr["give"], pr["take"], pr["street"], pr["from_street"]) == (0, 2, "Main street", "Side street")
    assert 5 <= pr["seconds"] <= 15


def test_nothing_is_proposed_when_every_direction_is_queued():
    mon = QueueMonitor([CAM])
    assert feed(mon.update, standing(10, 9))[1] == []


def test_short_or_moving_queues_are_not_a_problem():
    mon = QueueMonitor([CAM])
    assert feed(mon.update, standing(int(LOADED_MIN) - 1, 0))[1] == []
    moving = [Vehicle(f"f{i}", 0, 0, 0, 12.0, "car", "e1") for i in range(20)]
    assert feed(QueueMonitor([CAM]).update, moving)[1] == []


def test_shift_grows_with_imbalance_and_stays_in_range():
    assert shift_seconds(6, 2, 40, 40) < shift_seconds(12, 1, 40, 40)
    assert shift_seconds(100, 0, 40, 40) == 15 and shift_seconds(5.1, 1.5, 40, 40) >= 5


def test_platform_turns_imbalance_into_a_pending_proposal_and_sends_nothing():
    sent = []
    p = Platform([71.785, 40.385], 1200, [CAM], send=sent.append)
    out = feed(lambda t, v: p.handle(frame(t, v)), standing(10, 1))
    cmds = [c for c in p.gateway.public() if c["status"] == "pending"]
    assert len(cmds) == 1 and (cmds[0]["give"], cmds[0]["take"], cmds[0]["tls"]) == (0, 2, "J1")
    assert sent == [] and p.gateway.applied == 0
    assert out["cams"]["CAM-01"][2] == 10 and out["cmd"][0]["status"] == "pending"


def approve_and_apply(p, vehicles):
    cmd = p.gateway.public()[0]
    p.gateway.decide(cmd["id"], True, "operator", p.t)
    out = p.handle(frame(p.t + 1, vehicles, events=[{"type": "tls", "id": cmd["id"], "status": "applied", "seconds": 10}]))
    return cmd, out


def test_equipment_reports_move_the_command_forward_and_stay_out_of_the_city_feed():
    p = Platform([71.785, 40.385], 1200, [CAM])
    feed(lambda t, v: p.handle(frame(t, v)), standing(10, 1))
    cmd, out = approve_and_apply(p, standing(10, 1))
    assert out["events"] == [] and cmd["status"] == "active" and p.gateway.applied == 1


def test_command_is_released_early_when_the_queue_is_gone():
    sent = []
    p = Platform([71.785, 40.385], 1200, [CAM], send=sent.append)
    feed(lambda t, v: p.handle(frame(t, v)), standing(10, 1))
    cmd, _ = approve_and_apply(p, standing(10, 1))
    feed(lambda t, v: p.handle(frame(t, v)), standing(0, 0), start=p.t + 1, seconds=2 * WINDOW)
    assert {"op": "release", "id": cmd["id"]} in sent and cmd["end"] == "relieved"


def test_command_is_released_early_when_the_donor_direction_starts_to_suffer():
    sent = []
    p = Platform([71.785, 40.385], 1200, [CAM], send=sent.append)
    feed(lambda t, v: p.handle(frame(t, v)), standing(10, 1))
    cmd, _ = approve_and_apply(p, standing(10, 1))
    feed(lambda t, v: p.handle(frame(t, v)), standing(6, 9), start=p.t + 1, seconds=2 * WINDOW)
    assert {"op": "release", "id": cmd["id"]} in sent and cmd["end"] == "balance"


def test_incident_dispatch_stays_shadow_only_and_restart_resets_state():
    p = Platform([71.785, 40.385], 1200)
    ev = {"id": "d1", "type": "dispatch", "status": "en_route", "t": 5, "unit": "Скорая", "from": "Клиника", "ref": "inc-5"}
    out = p.handle(frame(5, events=[ev]))
    assert out["shadow"][0]["command"] == "tls.preempt" and out["shadow"][0]["executed"] is False
    assert p.snapshot()["events"] == [ev]
    again = p.handle(frame(0))
    assert again["reset"] is True and p.snapshot()["events"] == []
    assert p.audit()["count"] == 1  # the audit log survives restarts
