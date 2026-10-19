"""District servers at the centre: enrolled with a code and an address, kits through a district, reports trusted
for the district's own junctions only, and reports kept through an outage written to history at their own time."""
import asyncio
import json
import math

import pytest

from urban.devices import crypto
from urban.devices.enrolment import install_kit, make_code, make_kit, parse_code
from urban.devices.hub import DeviceHub
from urban.devices.registry import Registry, kind_of, provision
from urban.platform import BUFFER, Platform
from urban.sources import clock
from urban.store import Store

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"}]


def cam(jid, tls, *approaches):
    return {"id": jid, "tls": tls, "camera": False, "lon": 71.78, "lat": 40.38, "name": jid,
            "approaches": [{"id": a, "name": a, "zone": 100.0} for a in approaches],
            "phases": [{"index": i, "duration": 40, "min_green": 10, "edges": [a], "streets": [a]}
                       for i, a in enumerate(approaches)],
            "plans": [{"id": 0, "splits": {str(i): 40 for i in range(len(approaches))}}]}


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path, demo_devices=False)
    return tmp_path


def new_key():
    k = crypto.new_key()
    return k, crypto.public_hex(k)


def test_a_district_enrols_with_a_code_and_an_address(pki):
    key, public = new_key()
    assert parse_code(make_code("district-1", "SRV-1", public))["device"] == "district-1"
    reg = Registry(pki / "devices.json")
    rec = reg.enroll_district("district-1", "10.1.0.2", public, "SRV-1")
    assert kind_of(rec) == "district" and rec["address"] == "10.1.0.2" and rec["tls"] is None and rec["junction"] is None
    assert reg.districts() == [rec]
    sig = crypto.sign(key, {"device": "district-1", "nonce": "abc"})  # it authenticates like a unit
    assert reg.authenticate("district-1", "abc", sig)["id"] == "district-1"
    with pytest.raises(ValueError):
        reg.enroll_district("edge-J-01", "10.1.0.2", public)
    with pytest.raises(ValueError):
        reg.enroll_district("district-2", " ", public)
    assert kind_of(Registry(pki / "devices.json").devices["district-1"]) == "district"  # survives the file
    assert Registry(pki / "devices.json").by_tls("T1") is None  # a record without a junction is no junction's unit


def test_a_unit_assigned_to_a_district_gets_a_kit_that_points_there(pki, tmp_path):
    reg = Registry(pki / "devices.json")
    district = reg.enroll_district("district-1", "10.1.0.2", new_key()[1])
    ukey, upub = new_key()
    with pytest.raises(ValueError):
        reg.enroll("edge-J-01", JUNCTIONS[0], upub, "U-1", district="district-9")
    rec = reg.enroll("edge-J-01", JUNCTIONS[0], upub, "U-1", district="district-1")
    assert rec["district"] == "district-1"
    kit = make_kit(rec, pki, "urban-center", 8443, [], via=district)
    assert kit["platform"] == "10.1.0.2:8443" and kit["server_name"] == "urban-center" and kit["via"] == "district-1"
    direct = make_kit(rec, pki, "urban-center", 8443, [])
    assert direct["platform"] == "urban-center:8443" and direct["via"] is None
    district["address"] = "10.1.0.2:18443"  # an address with a port is taken as it is
    assert make_kit(rec, pki, "urban-center", 8443, [], via=district)["platform"] == "10.1.0.2:18443"
    assert reg.assign("edge-J-01", None).get("district") is None  # routing can change; the next kit says so
    assert reg.assign("edge-J-01", "district-1")["district"] == "district-1"
    assert reg.assign("district-1", None) is None
    with pytest.raises(ValueError):
        reg.assign("edge-J-01", "edge-J-02")
    # on the unit: the env names the district's address, the certificate name stays the centre's
    unit_pki = tmp_path / "unit" / "pki"
    (unit_pki / "edge").mkdir(parents=True)
    (unit_pki / "edge" / "edge-J-01.key").write_text(crypto.private_hex(ukey))
    env = tmp_path / "unit" / "edge-edge-J-01.env"
    install_kit(kit, "edge-J-01", unit_pki / "edge" / "edge-J-01.key", unit_pki, env)
    text = env.read_text()
    assert "PLATFORM=10.1.0.2:8443\n" in text and "SERVER_NAME=urban-center\n" in text


def test_a_district_kit_installs_on_the_district(pki, tmp_path):
    reg = Registry(pki / "devices.json")
    dkey, dpub = new_key()
    district = reg.enroll_district("district-1", "10.1.0.2", dpub)
    dkit = make_kit(district, pki, "urban-center", 8443)
    assert dkit["kind"] == "district" and dkit["platform"] == "urban-center:8443" and dkit["junction"] is None
    d_pki = tmp_path / "d" / "pki"
    (d_pki / "edge").mkdir(parents=True)
    (d_pki / "edge" / "district-1.key").write_text(crypto.private_hex(dkey))
    env = tmp_path / "d" / "edge-district-1.env"
    written = install_kit(dkit, "district-1", d_pki / "edge" / "district-1.key", d_pki, env)
    assert str(env) in written and not (tmp_path / "d" / "district-1.junction.json").exists()
    assert "PLATFORM=urban-center:8443\n" in env.read_text() and (d_pki / "ca.pem").exists()
    with pytest.raises(ValueError):  # a unit's kit does not go on a district
        install_kit({**dkit, "unit": "edge-J-01"}, "district-1", d_pki / "edge" / "district-1.key", d_pki, env)


def platform_with_devices(pki, store=None):
    """Two junctions; J-01's unit goes through district-1, J-02's unit is direct."""
    reg = Registry(pki / "devices.json")
    reg.enroll_district("district-1", "10.1.0.2", new_key()[1])
    reg.enroll("edge-J-01", JUNCTIONS[0], new_key()[1], district="district-1")
    reg.enroll("edge-J-02", JUNCTIONS[1], new_key()[1])
    hub = DeviceHub(reg, crypto.load_private((pki / "gateway.key").read_text()), on_report=lambda rec, r: None)
    p = Platform([71.78, 40.38], 1000, [cam("J-01", "T1", "a1", "b1"), cam("J-02", "T2", "a2")], store=store)
    t0 = 1_800_000_000
    p.handle(clock.frame(t0))
    p.devices(hub.status(), t0)
    return reg, hub, p, t0


def test_the_hub_and_the_platform_show_a_district_as_a_device(pki):
    reg, hub, p, t0 = platform_with_devices(pki)
    row = next(r for r in hub.status() if r["id"] == "district-1")
    assert row["kind"] == "district" and row["address"] == "10.1.0.2" and row["junction"] is None
    assert next(r for r in hub.status() if r["id"] == "edge-J-01")["district"] == "district-1"
    assert p.dev["district-1"]["k"] == "district" and p.dev["district-1"]["addr"] == "10.1.0.2"
    assert p.dev["J-01"]["via"] == "district-1" and p.dev["J-02"].get("via") is None
    assert p.routed_through("district-1") == {"J-01"}
    reg.set_status("district-1", "revoked")
    reg.remove("district-1")
    p.devices(hub.status(), t0 + 2)
    assert "district-1" not in p.dev and p.routed_through("district-1") == set()


def test_a_district_is_trusted_for_its_own_junctions_and_for_numbers_only(pki):
    reg, hub, p, t0 = platform_with_devices(pki)
    rec = reg.devices["district-1"]
    p.on_report(rec, {"t": t0, "queues": {"a1": 30.0, "b1": "7", "a2": 99.0, "zz": 1}, "events": []})
    assert p.sensors["J-01"] == {"a1": (t0, 30.0), "b1": (t0, 7.0)}  # per approach, dated by arrival
    assert "J-02" not in p.sensors  # J-02 is not routed through this district: its number is ignored
    p.on_report(rec, {"t": t0, "queues": {}, "events": [
        {"type": "edge", "status": "refused", "device": "district-1", "reason": "a district server takes no commands"},
        {"type": "tls", "id": "x", "status": "applied", "tls": "T1"}]})  # a lease event from a district means nothing
    assert any(r["event"] == "device-refused" and r["target"] == "district-1" for r in p.gateway.audit.records[-3:])
    assert not any(r["event"].startswith("lease") for r in p.gateway.audit.records[-3:])
    p.on_report("T2", {"t": t0, "queues": {"a2": 12.0}, "events": []})  # units still report by tls, as before
    assert p.sensors["J-02"] == {"a2": (t0, 12.0)}


def test_reports_kept_through_an_outage_go_to_history_at_their_own_time(pki, tmp_path):
    now = {"t": 1_800_000_000.0}
    store = Store(tmp_path / "h.db", mode="field", clock=lambda: now["t"])
    reg, hub, p, t0 = platform_with_devices(pki, store)
    rec = reg.devices["district-1"]
    p.on_report(rec, {"t": t0, "queues": {"a1": 10.0}, "events": []})
    live = p.sensors["J-01"]
    p.on_report(rec, {"t": t0 - 300, "queues": {"a1": 44.0}, "events": []})  # made during the outage, replayed now
    assert p.sensors["J-01"] == live  # the live reading is not dragged into the past
    p.handle(clock.frame(t0 + 1))  # the replayed rows are written with the next frame, in one transaction
    rows = store.queues("J-01", t0 - 400)
    assert [(r["at"], r["approach"], r["metres"]) for r in rows] == [(t0 - 300, "a1", 44.0)]


def test_the_hub_tells_the_platform_which_junctions_a_district_is_trusted_for(pki):
    """Through a session: the record handed to the platform with a district's report carries the junctions whose
    units connect through it, as the registry says at that moment (so no frame has to have run first)."""
    import asyncio

    from fastapi import WebSocketDisconnect

    reg = Registry(pki / "devices.json")
    dkey, dpub = new_key()
    reg.enroll_district("district-1", "10.1.0.2", dpub)
    reg.enroll("edge-J-01", JUNCTIONS[0], new_key()[1], district="district-1")
    reg.enroll("edge-J-02", JUNCTIONS[1], new_key()[1])
    seen = []
    hub = DeviceHub(reg, crypto.load_private((pki / "gateway.key").read_text()),
                    on_report=lambda rec, report: seen.append((rec, report)))

    class FakeWS:
        def __init__(self):
            self.sent, self.closed, self.reports = [], None, 0

        async def accept(self):
            pass

        async def send_text(self, text):
            self.sent.append(json.loads(text))

        async def receive_text(self):
            if len(self.sent) == 1:  # the challenge came: answer it with the district's key
                nonce = self.sent[0]["nonce"]
                return json.dumps({"type": "hello", "device": "district-1", "version": "0.1.0", "epoch": 1,
                                   "sig": crypto.sign(dkey, {"device": "district-1", "nonce": nonce})})
            if len(self.sent) == 2 and self.reports == 0:  # welcomed: one report, then the link drops
                self.reports += 1
                return json.dumps({"type": "report", "t": 5, "queues": {"a1": 1.0}, "events": [], "health": {}})
            raise WebSocketDisconnect()

        async def close(self, code=1000):
            self.closed = code

    asyncio.run(hub.serve(FakeWS()))
    assert len(seen) == 1
    rec, report = seen[0]
    assert rec["id"] == "district-1" and rec["junctions"] == ["J-01"] and report["queues"] == {"a1": 1.0}




def test_a_light_units_empty_report_does_not_wipe_the_districts_reading(pki):
    """Found by the three-machine check: the light unit reported {} every second right after the district's 42 m,
    and the centre's history never saw the district's number. (Adapted: readings are now kept per approach and
    dated by their arrival, so the old whole-entry (time, queues) tuple is gone.)"""
    reg, hub, p, t0 = platform_with_devices(pki)
    p.on_report(reg.devices["district-1"], {"t": t0, "queues": {"a1": 42.0}, "events": []})
    p.on_report(reg.devices["edge-J-01"], {"t": t0, "queues": {}, "events": []})
    assert p.sensors["J-01"] == {"a1": (t0, 42.0)}
    assert p.handle(clock.frame(t0))["cams"]["J-01"][2] == 42
    p.handle(clock.frame(t0 + 1))
    p.on_report(reg.devices["edge-J-01"], {"t": t0 + 1, "queues": {"a1": 3.0}, "events": []})  # a unit with cameras wins
    assert p.sensors["J-01"] == {"a1": (t0 + 1, 3.0)}
    p.on_report(reg.devices["edge-J-01"], {"t": t0 + 60, "queues": {}, "events": []})  # nobody measures any more
    assert p.handle(clock.frame(t0 + 60))["cams"]["J-01"][2] is None  # no data, not "no queue"


def test_a_unit_and_a_district_measuring_different_approaches_both_survive(pki):
    reg, hub, p, t0 = platform_with_devices(pki)
    p.on_report(reg.devices["district-1"], {"t": t0, "queues": {"a1": 42.0}, "events": []})
    p.on_report(reg.devices["edge-J-01"], {"t": t0, "queues": {"b1": 9.0}, "events": []})
    assert p.sensors["J-01"] == {"a1": (t0, 42.0), "b1": (t0, 9.0)}
    assert p.handle(clock.frame(t0))["cams"]["J-01"][2] == 42
    p.on_report(reg.devices["edge-J-01"], {"t": t0 + 99, "queues": {}, "events": []})  # an empty report keeps both
    assert set(p.sensors["J-01"]) == {"a1", "b1"}
    # freshness is judged by arrival here, not by the device's clock: a unit whose clock is a day off still counts
    p.on_report(reg.devices["edge-J-01"], {"t": t0 - 86400, "queues": {"b1": 5.0}, "events": []})
    assert p.sensors["J-01"]["b1"] == (t0, 5.0)


def test_non_finite_and_huge_queues_do_not_stop_the_platform(pki):
    """1e999 is inf in JSON and NaN is accepted by Python's parser: both used to reach round() in handle()."""
    reg, hub, p, t0 = platform_with_devices(pki)
    bad = json.loads('{"t": 1800000000, "queues": {"a1": 1e999, "b1": NaN, "a2": -Infinity}, "events": []}')
    p.on_report(reg.devices["district-1"], bad)
    p.on_report(reg.devices["edge-J-02"], {**bad, "queues": {"a2": float("inf")}})
    p.on_report("T1", {"t": t0, "queues": {"a1": float("nan"), "b1": 1e300}})
    for who in ("district-1", "edge-J-01"):
        p.on_report(reg.devices[who], {"t": t0, "queues": {"a1": 7e9, "b1": float("-inf")}, "events": []})
    frame = p.handle(clock.frame(t0 + 1))  # must not raise
    for per in p.sensors.values():
        assert all(math.isfinite(v) and 0 <= v <= 1000 for _, v in per.values())  # clamped to 10 zones of 100 m
    assert "J-02" not in p.sensors
    assert frame["cams"]["J-01"][2] == 1000 and frame["cams"]["J-02"][2] is None


def test_an_absurd_t_is_taken_as_now_and_never_reaches_the_store(pki, tmp_path):
    store = Store(tmp_path / "h.db", mode="field", clock=lambda: 1_800_000_000.0)
    reg, hub, p, t0 = platform_with_devices(pki, store)
    for t in (float("inf"), float("nan"), 10 ** 30, "x", None, [1]):
        p.on_report(reg.devices["district-1"], {"t": t, "queues": {"a1": 5.0}, "events": []})
        p.on_report(reg.devices["edge-J-01"], {"t": t, "queues": {"b1": 5.0}, "events": []})
    assert p.sensors["J-01"] == {"a1": (t0, 5.0), "b1": (t0, 5.0)} and not p._late
    p.handle(clock.frame(t0 + 1))
    assert all(r["at"] <= 2_000_000_000 for r in store.queues("J-01", 0))


def test_late_reports_become_history_only_inside_the_outage_buffer(pki, tmp_path):
    store = Store(tmp_path / "h.db", mode="field", clock=lambda: 1_800_000_000.0)
    reg, hub, p, t0 = platform_with_devices(pki, store)
    rec = reg.devices["district-1"]
    for at in (t0 - 10, t0 - BUFFER, t0 - BUFFER - 61, t0 - 100_000, 5):  # the last three are older than a buffer holds
        p.on_report(rec, {"t": at, "queues": {"a1": 1.0}, "events": []})
    assert p.sensors == {}  # a late report never becomes the live reading
    p.handle(clock.frame(t0 + 1))
    assert sorted(r["at"] for r in store.queues("J-01", 0)) == [t0 - BUFFER, t0 - 10]
    # without a store there is nowhere to put history: dropped
    reg2, _, p2, _ = platform_with_devices(pki)
    p2.on_report(reg2.devices["district-1"], {"t": t0 - 10, "queues": {"a1": 1.0}, "events": []})
    assert p2.sensors == {} and not p2._late


def test_the_buffer_constant_matches_the_districts():
    from urban.node import district
    assert BUFFER == district.BUFFER


def test_a_replayed_outage_is_one_transaction(pki, tmp_path):
    store = Store(tmp_path / "h.db", mode="field", clock=lambda: 1_800_000_000.0)
    reg, hub, p, t0 = platform_with_devices(pki, store)
    rec = reg.devices["district-1"]
    statements = []
    store.db.set_trace_callback(lambda sql: statements.append(sql.strip().upper()))
    for i in range(900):
        p.on_report(rec, {"t": t0 - 900 + i, "queues": {"a1": float(i), "b1": 1.0}, "events": []})
    assert statements == []  # the burst only queues rows: nothing blocks the event loop
    p.handle(clock.frame(t0 + 1))
    assert statements.count("BEGIN") == 1 and statements.count("COMMIT") == 1
    assert len(store.queues("J-01", 0)) == 2 * 895  # the last five seconds are live, not late


def test_the_audit_names_the_authenticated_device_and_clips_its_words(pki):
    reg, hub, p, t0 = platform_with_devices(pki)
    events = [{"type": "edge", "status": "refused", "device": "edge-J-02", "reason": "x" * 500 + "\nforged line"}]
    events += [{"type": "edge", "reason": f"r{i}"} for i in range(50)]
    p.on_report(reg.devices["district-1"], {"t": t0, "queues": {}, "events": events})
    refused = [r for r in p.gateway.audit.records if r["event"] == "device-refused"]
    assert len(refused) == 10  # at most 10 events per report
    assert {r["target"] for r in refused} == {"district-1"}  # not the device the report names
    assert len(refused[0]["note"]) <= 80 and "\n" not in refused[0]["note"]
    p.on_report(reg.devices["edge-J-01"], {"t": t0, "events": [{"type": "edge", "device": "district-1", "reason": "y"}]})
    assert p.gateway.audit.records[-1]["target"] == "edge-J-01"


def test_a_revoked_unit_is_no_ones_junction_and_a_revoked_district_takes_no_units(pki):
    reg, hub, p, t0 = platform_with_devices(pki)
    reg.set_status("edge-J-01", "revoked")
    p.devices(hub.status(), t0 + 2)
    assert p.routed_through("district-1") == set()
    # through a session: the hub hands the district's junction list to the platform without the revoked unit
    assert asyncio.run(_one_district_report(reg, hub, {"t": 5, "queues": {"a1": 1.0}}))[0]["junctions"] == []
    reg.set_status("edge-J-01", "active")
    assert asyncio.run(_one_district_report(reg, hub, {"t": 5, "queues": {"a1": 1.0}}))[0]["junctions"] == ["J-01"]
    reg.set_status("district-1", "revoked")
    with pytest.raises(ValueError):
        reg.assign("edge-J-01", "district-1")
    with pytest.raises(ValueError):
        reg.enroll("edge-J-01", JUNCTIONS[0], new_key()[1], district="district-1")
    assert reg.assign("edge-J-01", None).get("district") is None  # direct is always allowed
    reg.set_status("district-1", "active")
    assert reg.assign("edge-J-01", "district-1")["district"] == "district-1"


async def _one_district_report(reg, hub, report):
    """Run one district session against the hub with a fake socket; returns [(record, report)] the platform got."""
    from fastapi import WebSocketDisconnect

    dkey = crypto.new_key()
    reg.enroll_district("district-1", "10.1.0.2", crypto.public_hex(dkey))
    seen = []
    hub.on_report = lambda rec, rep: seen.append(rec)

    class WS:
        def __init__(self):
            self.sent, self.did = [], False

        async def accept(self):
            pass

        async def send_text(self, text):
            self.sent.append(json.loads(text))

        async def receive_text(self):
            if len(self.sent) == 1:
                return json.dumps({"type": "hello", "device": "district-1", "version": "1",
                                   "sig": crypto.sign(dkey, {"device": "district-1", "nonce": self.sent[0]["nonce"]})})
            if not self.did:
                self.did = True
                return json.dumps({"type": "report", "events": [], "health": {}, **report})
            raise WebSocketDisconnect()

        async def close(self, code=1000):
            pass

    await hub.serve(WS())
    return seen


def test_set_plans_for_no_junction_leaves_a_district_connected(pki):
    reg, hub, p, t0 = platform_with_devices(pki)

    class WS:
        closed = None

        async def close(self, code=1000):
            self.closed = code

    district, unit = WS(), WS()
    hub.sessions.update({"district-1": district, "edge-J-01": unit})  # a district has no tls
    asyncio.run(hub.set_plans(None, {1}))
    assert district.closed is None and unit.closed is None
    asyncio.run(hub.set_plans("T1", {1}))
    assert unit.closed is not None and district.closed is None
