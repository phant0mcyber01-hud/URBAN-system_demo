import pytest

from urban.equipment.adapter import AdapterError, ControllerAdapter
from urban.equipment.edge import MAX_LEASE_TTL, EdgeController
from urban.equipment.ntcip import STANDBY, SYSTEM_PATTERN_CONTROL, UNIT_BACKUP_TIME, NtcipAdapter
from urban.equipment.relay import RelayAdapter
from urban.sources.measure import lane_queue, queue_lengths


class FakeController(ControllerAdapter):
    """Records what it was asked to do; `broken` makes every call fail like a dead link."""

    def __init__(self, tables):
        self.tables, self.calls, self.broken = tables, [], False

    def plans(self, tls):
        return set(self.tables.get(tls, ()))

    def _call(self, *what):
        if self.broken:
            raise AdapterError("no answer")
        self.calls.append(what)

    def select(self, tls, plan):
        self._call("select", tls, plan)

    def base(self, tls):
        self._call("base", tls)

    def keepalive(self, tls, plan):
        self._call("keepalive", tls, plan)


def lease(pid="cmd-1", tls="J1", plan=2, ttl=180):
    return {"op": "lease", "id": pid, "tls": tls, "plan": plan, "ttl": ttl}


def edge():
    ctl = FakeController({"J1": {0, 1, 2, 3}})
    return EdgeController(ctl), ctl


# ---------------------------------------------------------------- URBAN Edge

def test_edge_selects_an_approved_plan_and_reverts_when_the_lease_is_not_renewed():
    e, ctl = edge()
    assert e.handle(lease(), 0)[0]["status"] == "applied"
    assert ctl.calls == [("select", "J1", 2)]
    assert e.tick(179) == []
    ev = e.tick(180)
    assert ev[0]["status"] == "reverted" and ev[0]["reason"] == "expired"
    assert ctl.calls[-1] == ("base", "J1") and e.leases == {}


def test_edge_renewal_extends_the_lease_and_confirms_the_plan_to_the_controller():
    e, ctl = edge()
    e.handle(lease(), 0)
    assert e.handle(lease(), 60)[0]["status"] == "renewed"
    assert ctl.calls[-1] == ("keepalive", "J1", 2)
    assert e.tick(200) == [] and e.tick(240)[0]["reason"] == "expired"


def test_edge_refuses_what_the_engineer_did_not_approve():
    e, ctl = edge()
    assert e.handle(lease(plan=9), 0)[0]["status"] == "rejected"
    assert e.handle(lease(plan=0), 0)[0]["status"] == "rejected"      # the base plan needs no lease
    assert e.handle(lease(tls="UNKNOWN"), 0)[0]["status"] == "rejected"
    assert ctl.calls == []


def test_edge_keeps_one_lease_per_junction_and_caps_its_lifetime():
    e, _ = edge()
    e.handle(lease(ttl=10 ** 6), 0)
    assert e.handle(lease(pid="cmd-2", plan=1), 5)[0]["status"] == "rejected"
    assert e.tick(MAX_LEASE_TTL - 1) == [] and e.tick(MAX_LEASE_TTL)[0]["reason"] == "expired"


def test_edge_release_returns_the_base_plan_and_unknown_ops_do_nothing():
    e, ctl = edge()
    e.handle(lease(), 0)
    assert e.handle({"op": "release", "id": "cmd-1"}, 5)[0]["reason"] == "released"
    e.handle(lease(pid="cmd-3"), 10)
    assert e.handle({"op": "revoke_all"}, 11) == []                   # the kill switch releases per command
    assert e.handle({"op": "release", "id": "cmd-3"}, 12)[0]["reason"] == "released"
    assert [c for c in ctl.calls if c[0] == "base"] == [("base", "J1"), ("base", "J1")]


def test_edge_drops_the_lease_when_the_controller_stops_answering():
    e, ctl = edge()
    e.handle(lease(), 0)
    ctl.broken = True
    ev = e.handle(lease(), 60)
    assert ev[0]["status"] == "reverted" and "controller lost" in ev[0]["reason"] and e.leases == {}
    assert e.handle(lease(pid="cmd-4"), 61)[0]["status"] == "rejected"


# ---------------------------------------------------------------- adapters

def test_relay_adapter_encodes_the_plan_in_binary_and_opens_everything_for_base():
    written = []
    relay = RelayAdapter(lambda tls, states: written.append((tls, states)), {"J1": {0, 1, 2, 5}})
    relay.select("J1", 5)
    relay.base("J1")
    assert written == [("J1", [True, False, True]), ("J1", [False, False, False])]
    with pytest.raises(AdapterError):
        relay.select("J1", 3)                                         # not approved
    with pytest.raises(AdapterError):
        RelayAdapter(lambda *a: None, {"J1": {9}}, relays=3).select("J1", 9)   # does not fit on three relays


def test_relay_adapter_reports_a_dead_board():
    def dead(tls, states):
        raise OSError("i/o error")
    with pytest.raises(AdapterError):
        RelayAdapter(dead, {"J1": {0, 1}}).select("J1", 1)


def test_ntcip_adapter_arms_the_backup_timer_before_the_first_pattern_and_keeps_it_alive():
    sets = []
    ntcip = NtcipAdapter(lambda tls, oid, value: sets.append((oid, value)), {"J1": {0, 1, 2}}, backup_time=180)
    ntcip.select("J1", 2)
    ntcip.keepalive("J1", 2)
    ntcip.select("J1", 1)
    ntcip.base("J1")
    assert sets == [(UNIT_BACKUP_TIME, 180), (SYSTEM_PATTERN_CONTROL, 2), (UNIT_BACKUP_TIME, 180), (SYSTEM_PATTERN_CONTROL, 2),
                    (UNIT_BACKUP_TIME, 180), (SYSTEM_PATTERN_CONTROL, 1), (SYSTEM_PATTERN_CONTROL, STANDBY)]
    with pytest.raises(AdapterError):
        ntcip.select("J1", 7)


def test_ntcip_adapter_maps_plan_ids_to_controller_patterns():
    sets = []
    NtcipAdapter(lambda tls, oid, value: sets.append(value), {"J1": {0, 1}}, pattern_of=lambda plan: 20 + plan).select("J1", 1)
    assert sets[-1] == 21


def test_edge_works_the_same_over_any_adapter():
    written = []
    e = EdgeController(RelayAdapter(lambda tls, states: written.append(states), {"J1": {0, 1, 2}}))
    assert e.handle(lease(plan=2), 0)[0]["status"] == "applied"
    e.tick(10 ** 4)
    assert written == [[False, True, False], [False, False, False]]


# ---------------------------------------------------------------- emulated queue sensor

def car(dist, speed=0.0, length=5.0):
    return (dist, speed, length)


def test_queue_is_measured_from_the_stop_line_to_the_tail():
    assert lane_queue([car(2), car(9.5), car(17), car(24.5)], 100) == 29.5


def test_a_gap_ends_the_queue_and_moving_vehicles_are_not_part_of_it():
    assert lane_queue([car(2), car(9.5), car(60)], 100) == 14.5
    assert lane_queue([car(2), car(9.5), car(17, speed=8)], 100) == 14.5
    assert lane_queue([car(5, speed=10), car(40, speed=12)], 100) == 0.0


def test_a_lone_stopped_vehicle_far_from_the_stop_line_is_not_a_queue():
    assert lane_queue([car(70)], 100) == 0.0                           # a bus at its stop, a parked car
    assert lane_queue([car(20)], 100) == 25.0


def test_queue_still_counts_while_its_front_is_discharging():
    # green has started: the first cars roll, the tail still stands
    assert lane_queue([car(3, speed=4), car(11, speed=3), car(30), car(37.5)], 100) == 42.5


def test_queue_is_capped_by_the_zone_the_sensor_covers():
    cars = [car(2 + 7.5 * i) for i in range(30)]
    assert lane_queue(cars, 100) == 100
    assert lane_queue(cars, 40) == 40


def test_the_longest_lane_stands_for_the_approach_and_junction_interiors_are_ignored():
    samples = [("e1_0", 2, 0, 5), ("e1_0", 9.5, 0, 5), ("e1_1", 2, 0, 5), (":junction_0", 1, 0, 5), ("e2_0", 3, 12, 5)]
    assert queue_lengths(samples) == {"e1": 14.5}
    assert queue_lengths([("e1_0", 2 + 7.5 * i, 0, 5) for i in range(20)], {"e1": 60.0}) == {"e1": 60.0}
