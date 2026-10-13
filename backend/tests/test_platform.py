from urban.analytics.queues import LOADED_MIN, WINDOW, QueueMonitor, pick_plan, shift_seconds
from urban.core.events import Vehicle
from urban.platform import SETTLE, Platform

PLANS = [{"id": 0, "splits": {"0": 40, "2": 40}},
         {"id": 1, "give": 0, "take": 2, "shift": 5, "splits": {"0": 45, "2": 35}},
         {"id": 2, "give": 0, "take": 2, "shift": 10, "splits": {"0": 50, "2": 30}},
         {"id": 3, "give": 0, "take": 2, "shift": 15, "splits": {"0": 55, "2": 25}},
         {"id": 4, "give": 2, "take": 0, "shift": 5, "splits": {"0": 35, "2": 45}}]
CAM = {"id": "CAM-01", "camera": True, "lon": 71.785, "lat": 40.385, "tls": "J1", "name": "Crossing",
       "approaches": [{"id": "e1", "name": "Main street", "zone": 100.0}, {"id": "e2", "name": "Side street", "zone": 100.0}],
       "phases": [{"index": 0, "duration": 40, "min_green": 10, "edges": ["e1"], "streets": ["Main street"]},
                  {"index": 2, "duration": 40, "min_green": 10, "edges": ["e2"], "streets": ["Side street"]}],
       "plans": PLANS}
VEHICLES = [Vehicle("car1", 71.785, 40.385, 90, 10, "car", "e9"), Vehicle("far", 72.5, 40.385, 0, 10, "car", "e8")]


def frame(t, queues=None, **extra):
    return {"t": t, "vehicles": VEHICLES, "bus_loss": [], "queues": queues or {}, **extra}


def q(main, side=0.0):
    return {"e1": main, "e2": side}


def feed(target, queues, start=0, seconds=WINDOW + 1):
    out = None
    for t in range(start, start + seconds):
        out = target(t, queues)
    return out


def test_frame_keeps_only_vehicles_in_scene_as_local_decimetres():
    out = Platform([71.785, 40.385], 1200).handle(frame(1))
    assert out["v"] == [["car1", 0, 0, 90, 10, "car"]]
    assert out["stats"]["cars"] == 2


def test_no_decision_before_a_full_cycle_of_history():
    mon = QueueMonitor([CAM])
    assert feed(mon.update, q(90), seconds=WINDOW - 1)[1] == []
    assert mon.demand("CAM-01") == {}


def test_long_queue_with_a_free_cross_street_selects_an_approved_plan():
    mon = QueueMonitor([CAM])
    _, problems = feed(mon.update, q(80, 5))
    assert len(problems) == 1
    pr = problems[0]
    assert (pr["give"], pr["take"], pr["street"], pr["from_street"]) == (0, 2, "Main street", "Side street")
    assert pr["plan"] in {1, 2, 3} and pr["shift"] == PLANS[pr["plan"]]["shift"]
    assert (pr["queue"], pr["other"]) == (80, 5)


def test_nothing_is_proposed_when_every_direction_is_queued():
    assert feed(QueueMonitor([CAM]).update, q(80, 70))[1] == []


def test_short_queue_is_not_a_problem():
    assert feed(QueueMonitor([CAM]).update, q(LOADED_MIN - 5, 0))[1] == []


def test_short_approach_counts_as_loaded_at_a_share_of_its_zone():
    short = {**CAM, "approaches": [{"id": "e1", "name": "Main street", "zone": 30.0}, CAM["approaches"][1]]}
    assert len(feed(QueueMonitor([short]).update, q(28, 0))[1]) == 1


def test_no_proposal_when_the_engineer_approved_no_plan_for_that_direction():
    only_main = {**CAM, "plans": PLANS[:4]}                       # nothing may be moved towards the side street
    assert feed(QueueMonitor([only_main]).update, q(0, 80))[1] == []
    assert feed(QueueMonitor([{**CAM, "plans": PLANS[:1]}]).update, q(80, 0))[1] == []


def test_pick_plan_takes_the_largest_shift_not_above_the_wanted_one():
    assert pick_plan(PLANS, 0, 2, 12)["id"] == 2
    assert pick_plan(PLANS, 0, 2, 15)["id"] == 3
    assert pick_plan(PLANS, 2, 0, 15)["id"] == 4
    assert pick_plan(PLANS, 0, 2, 3)["id"] == 1                   # nothing that small: the smallest approved one
    assert pick_plan(PLANS[:1], 0, 2, 10) is None


def test_shift_grows_with_imbalance_and_stays_in_range():
    assert shift_seconds(50, 20, 40, 40) < shift_seconds(100, 5, 40, 40)
    assert shift_seconds(1000, 0, 40, 40) == 15 and shift_seconds(41, 12, 40, 40) >= 5


def make_platform(sent=None):
    return Platform([71.785, 40.385], 1200, [CAM], send=(sent.append if sent is not None else lambda m: None))


def test_platform_turns_imbalance_into_a_pending_plan_proposal_and_sends_nothing():
    sent = []
    p = make_platform(sent)
    out = feed(lambda t, v: p.handle(frame(t, v)), q(80, 5))
    cmds = [c for c in p.gateway.public() if c["status"] == "pending"]
    assert len(cmds) == 1 and cmds[0]["command"] == "tls.select_plan" and cmds[0]["plan"] in {1, 2, 3}
    assert sent == [] and p.gateway.applied == 0
    assert out["cams"]["CAM-01"][2] == 80 and out["cmd"][0]["status"] == "pending"
    assert cmds[0]["reason"].startswith("камера CAM-01")
    assert p.monitor.demands["CAM-01"] == {0: 80.0, 2: 5.0}


def test_bare_detector_is_labelled_as_one():
    p = Platform([71.785, 40.385], 1200, [{**CAM, "id": "J-07", "camera": False}])
    feed(lambda t, v: p.handle(frame(t, v)), q(80, 5))
    assert p.gateway.public()[0]["reason"].startswith("детектор J-07")


def approve_and_apply(p, queues):
    cmd = p.gateway.public()[0]
    p.gateway.decide(cmd["id"], True, "operator", p.t)
    out = p.handle(frame(p.t + 1, queues, events=[{"type": "tls", "id": cmd["id"], "status": "applied", "plan": cmd["plan"]}]))
    return cmd, out


def test_equipment_reports_move_the_command_forward_and_stay_out_of_the_city_feed():
    p = make_platform()
    feed(lambda t, v: p.handle(frame(t, v)), q(80, 5))
    cmd, out = approve_and_apply(p, q(80, 5))
    assert out["events"] == [] and cmd["status"] == "active" and p.gateway.applied == 1


def test_command_is_released_early_when_the_queue_is_gone_but_not_before_it_settles():
    sent = []
    p = make_platform(sent)
    feed(lambda t, v: p.handle(frame(t, v)), q(80, 5))
    cmd, _ = approve_and_apply(p, q(80, 5))
    feed(lambda t, v: p.handle(frame(t, v)), q(0, 0), start=p.t + 1, seconds=SETTLE - 2)
    assert {"op": "release", "id": cmd["id"]} not in sent         # a controller needs a cycle or two to switch
    feed(lambda t, v: p.handle(frame(t, v)), q(0, 0), start=p.t + 1, seconds=10)
    assert {"op": "release", "id": cmd["id"]} in sent and cmd["end"] == "relieved"


def test_command_is_released_early_when_the_donor_direction_starts_to_suffer():
    sent = []
    p = make_platform(sent)
    feed(lambda t, v: p.handle(frame(t, v)), q(80, 5))
    cmd, _ = approve_and_apply(p, q(80, 5))
    feed(lambda t, v: p.handle(frame(t, v)), q(50, 90), start=p.t + 1, seconds=SETTLE + 10)
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
