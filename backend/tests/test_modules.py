"""The City OS core side of modules: the registry of built-in modules (MODULES, an unknown name refused), the report
router (a device's report reaches only the module of its kind), GET /api/modules's body, the traffic module over the
existing platform, and lighting cabinets and pump stations as devices (enrolment, kit, CLI, hub: no traffic plans,
OS updates offered). The lighting and water modules themselves are tested in test_lighting.py and test_water.py; here
fakes stand in for them."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, WebSocketDisconnect

from urban import config as c
from urban import provision as provision_cli
from urban import modules as modules_pkg
from urban.devices import crypto
from urban.devices.enrolment import make_kit
from urban.devices.hub import DeviceHub
from urban.devices.registry import ACTIVE, REVOKED, Registry, kind_of, provision
from urban.modules import (ModuleError, describe, load_modules, mount_routes, report_router, startup_all, tick_all)
from urban.modules.base import Core
from urban.modules.traffic import TrafficModule
from urban.platform import Platform
from urban.release import ReleaseStore

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"}]
CAM = {"id": "J-01", "tls": "T1", "camera": False, "lon": 71.78, "lat": 40.38, "name": "x",
       "approaches": [], "phases": [], "plans": []}


def new_pub() -> str:
    return crypto.public_hex(crypto.new_key())


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path, demo_devices=False)
    return tmp_path


def make_core(**extra) -> Core:
    return Core(send=lambda device, body: True, online=lambda device: False, devices=lambda kind: [],
                audit=lambda *a, **k: None, now=lambda: 0.0, extra=extra)


class Fake:
    """A module that records what the core gives it."""
    title = "Фальшивый"
    device_kind = None

    def __init__(self):
        self.core, self.reports, self.ticks, self.started, self.router = None, [], [], False, None

    def attach(self, core):
        self.core = core

    def on_report(self, rec, report):
        self.reports.append((rec["id"], report))

    def routes(self):
        return self.router

    def tick(self, now):
        self.ticks.append(now)

    def summary(self):
        return {"devices": len(self.reports), "online": 0, "alerts": 0}

    async def startup(self):
        self.started = True


class FakeTraffic(Fake):
    id, title, device_kind = "traffic", "Светофоры и пробки", "unit"


class FakeLighting(Fake):
    id, title, device_kind = "lighting", "Уличное освещение", "lighting"


@pytest.fixture
def fakes(monkeypatch):
    """The built-in table with fakes instead of the real modules, so these tests do not depend on them."""
    here = sys.modules[__name__].__name__
    monkeypatch.setattr(modules_pkg, "BUILTIN", {"traffic": (here, "FakeTraffic"), "lighting": (here, "FakeLighting")})


# ---------------------------------------------------------------- the registry of modules

def test_modules_are_loaded_in_the_order_named_and_attached(fakes):
    core = make_core()
    mods = load_modules(["lighting", "traffic"], core)
    assert [m.id for m in mods] == ["lighting", "traffic"] and all(m.core is core for m in mods)
    assert [m.id for m in load_modules(["traffic"], core)] == ["traffic"]
    assert load_modules([], core) == [] and [m.id for m in load_modules([" traffic ", ""], core)] == ["traffic"]


def test_an_unknown_or_repeated_module_name_is_refused_with_a_clear_error(fakes):
    with pytest.raises(ModuleError, match="unknown module 'power'.*lighting, traffic"):
        load_modules(["traffic", "power"], make_core())
    with pytest.raises(ModuleError, match="named twice"):
        load_modules(["traffic", "traffic"], make_core())


def test_a_module_that_cannot_be_imported_is_a_module_error(monkeypatch):
    monkeypatch.setattr(modules_pkg, "BUILTIN", {"lighting": ("urban.modules.no_such_module_here", "X")})
    with pytest.raises(ModuleError, match="lighting.*cannot be loaded"):
        load_modules(["lighting"], make_core())


class Twin(FakeLighting):
    id = "twin"
    device_kind = "unit"


def test_two_modules_cannot_own_the_same_kind_of_device(monkeypatch, fakes):
    monkeypatch.setitem(modules_pkg.BUILTIN, "twin", (sys.modules[__name__].__name__, "Twin"))
    with pytest.raises(ModuleError, match="both own 'unit'"):
        load_modules(["traffic", "twin"], make_core())


def test_the_built_ins_are_traffic_lighting_and_water_and_all_are_on_by_default():
    assert set(modules_pkg.BUILTIN) == {"traffic", "lighting", "water"}
    assert modules_pkg.BUILTIN["water"] == ("urban.modules.water", "WaterModule")
    assert c.MODULES == ["traffic", "lighting", "water"]  # MODULES in .env or the environment overrides this on purpose


def test_the_real_built_ins_load_together_each_owning_its_own_kind():
    mods = load_modules(["lighting", "water"], make_core())
    assert [(m.id, m.device_kind) for m in mods] == [("lighting", "lighting"), ("water", "water")]
    assert mods[1].title == "Водоканал"


# ---------------------------------------------------------------- reports go to the module of the device's kind

def test_a_report_reaches_only_the_module_of_its_kind(fakes):
    traffic, lighting = FakeTraffic(), FakeLighting()
    route = report_router([traffic, lighting])
    route({"id": "edge-J-01", "kind": "unit"}, {"n": 1})
    route({"id": "edge-J-02"}, {"n": 2})                      # an old record without a kind is a unit
    route({"id": "light-C-01", "kind": "lighting"}, {"n": 3})
    assert traffic.reports == [("edge-J-01", {"n": 1}), ("edge-J-02", {"n": 2})]
    assert lighting.reports == [("light-C-01", {"n": 3})]


def test_a_district_servers_report_goes_to_traffic_as_before(fakes):
    traffic, lighting = FakeTraffic(), FakeLighting()
    report_router([traffic, lighting])({"id": "district-1", "kind": "district"}, {"queues": {}})
    assert [r[0] for r in traffic.reports] == ["district-1"] and lighting.reports == []


def test_a_kind_nobody_owns_is_ignored_with_one_log_line(fakes):
    traffic, lines = FakeTraffic(), []
    route = report_router([traffic], log=lines.append)
    for _ in range(3):
        route({"id": "light-C-01", "kind": "lighting"}, {})   # lighting is not loaded here
    route({"id": "x-1", "kind": "power"}, {})
    assert traffic.reports == [] and len(lines) == 2 and "'lighting'" in lines[0] and "'power'" in lines[1]


def test_the_hub_hands_a_lighting_cabinets_report_to_the_lighting_module_only(pki, fakes):
    reg = Registry(pki / "devices.json")
    key = crypto.new_key()
    reg.enroll_lighting("light-C-01", "C-01", crypto.public_hex(key), "URB-L1", 71.78, 40.38)
    traffic, lighting = FakeTraffic(), FakeLighting()
    hub = DeviceHub(reg, crypto.new_key(), on_report=report_router([traffic, lighting]))
    ws = LightWS(key, "light-C-01", [{"type": "report", "t": 5, "health": {"os": 1}, "lighting": {"cabinet": "C-01"}}])
    asyncio.run(hub.serve(ws))
    assert traffic.reports == [] and [r[0] for r in lighting.reports] == ["light-C-01"]
    assert lighting.reports[0][1]["lighting"] == {"cabinet": "C-01"}


# ---------------------------------------------------------------- /api/modules and the router mounting

def test_describe_lists_every_module_with_its_summary(fakes):
    traffic, lighting = FakeTraffic(), FakeLighting()
    lighting.reports.append(("light-C-01", {}))
    assert describe([traffic, lighting]) == [
        {"id": "traffic", "title": "Светофоры и пробки", "device_kind": "unit",
         "summary": {"devices": 0, "online": 0, "alerts": 0}},
        {"id": "lighting", "title": "Уличное освещение", "device_kind": "lighting",
         "summary": {"devices": 1, "online": 0, "alerts": 0}}]


def test_a_failing_summary_does_not_break_the_list(fakes):
    class Broken(FakeLighting):
        def summary(self):
            raise RuntimeError("boom")
    lines = []
    out = describe([FakeTraffic(), Broken()], log=lines.append)
    assert [m["id"] for m in out] == ["traffic", "lighting"] and out[1]["summary"] == {} and "boom" in lines[0]


def test_module_routes_are_mounted_under_api_id(fakes):
    traffic, lighting = FakeTraffic(), FakeLighting()
    router = APIRouter()

    @router.get("/state")
    def state():
        return {"cabinets": []}
    lighting.router = router                    # traffic has no routes: its endpoints stay where they are
    app = FastAPI()
    mount_routes(app, [traffic, lighting])
    paths = set(app.openapi()["paths"])
    assert "/api/lighting/state" in paths and not any(p.startswith("/api/traffic") for p in paths)


def test_tick_reaches_every_module_even_if_one_fails_and_startup_runs(fakes):
    class Bad(FakeLighting):
        def tick(self, now):
            raise RuntimeError("nope")

        async def startup(self):
            raise RuntimeError("no start")
    traffic, lines = FakeTraffic(), []
    mods = [Bad(), traffic]
    tick_all(mods, 12.5, log=lines.append)
    asyncio.run(startup_all(mods, log=lines.append))
    assert traffic.ticks == [12.5] and traffic.started and len(lines) == 2


# ---------------------------------------------------------------- traffic as a module, nothing rewritten

def test_traffic_module_wraps_the_platform_and_delegates_reports():
    got = []
    platform = SimpleNamespace(on_report=lambda rec, report: got.append((rec, report)), dev={}, cameras=[],
                               gateway=SimpleNamespace(proposals={}))
    m = TrafficModule()
    m.attach(make_core(platform=platform))
    assert (m.id, m.title, m.device_kind, m.routes()) == ("traffic", "Светофоры и пробки", "unit", None)
    m.on_report({"id": "edge-J-01"}, {"queues": {}})
    m.tick(1.0)
    asyncio.run(m.startup())
    assert got == [({"id": "edge-J-01"}, {"queues": {}})]
    with pytest.raises(ValueError):
        TrafficModule().attach(make_core())     # no platform, no traffic module


def test_traffic_summary_counts_units_districts_alerts_and_pending_proposals():
    p = Platform([71.78, 40.38], 1000, [CAM])
    rows = [{"id": "edge-J-01", "junction": "J-01", "tls": "T1", "status": "active", "online": True, "age": 1,
             "health": {"door": "open"}, "fingerprint": "AAAA-BBBB-CCCC-DDDD"},
            {"id": "edge-J-02", "junction": "J-02", "tls": None, "status": "active", "online": False, "age": 400,
             "health": {}, "fingerprint": "1111-2222-3333-4444"},
            {"id": "district-1", "kind": "district", "junction": None, "tls": None, "status": "active", "online": True,
             "age": 1, "health": {}, "fingerprint": "5555-6666-7777-8888", "address": "10.1.0.2"},
            {"id": "light-C-01", "kind": "lighting", "cabinet": "C-01", "junction": None, "tls": None,
             "status": "active", "online": True, "age": 1, "health": {}, "fingerprint": "9999-0000-1111-2222"}]
    p.devices(rows, 10)
    p.gateway.proposals["p1"] = {"status": "pending"}
    p.gateway.proposals["p2"] = {"status": "active"}
    m = TrafficModule()
    m.attach(make_core(platform=p))
    s = m.summary()
    assert s["junctions"] == 1 and s["units"] == s["devices"] == 2 and s["online"] == 1 and s["districts"] == 1
    assert s["alerts"] >= 2 and s["pending"] == 1                    # an open door, a unit without a link
    assert p.dev["light-C-01"]["k"] == "lighting" and p.dev["light-C-01"]["cab"] == "C-01"  # shown, not a junction's


def test_the_platform_ignores_a_lighting_report():
    p = Platform([71.78, 40.38], 1000, [CAM])
    p.on_report({"id": "light-C-01", "kind": "lighting", "tls": None}, {"queues": {"a": 1}, "lighting": {}})
    assert p.sensors == {}


# ---------------------------------------------------------------- lighting cabinets in the registry

def test_enroll_lighting_makes_a_lighting_record_that_is_nobodys_junction(pki):
    reg = Registry(pki / "devices.json")
    pub = new_pub()
    rec = reg.enroll_lighting("light-C-01", "C-01", pub, "URB-L1", 71.78, 40.38)
    assert rec["kind"] == "lighting" == kind_of(rec) and rec["cabinet"] == "C-01" and rec["status"] == ACTIVE
    assert (rec["lon"], rec["lat"], rec["serial"], rec["junction"], rec["tls"], rec.get("district")) == \
        (71.78, 40.38, "URB-L1", None, None, None)
    assert reg.by_tls("") is None and reg.by_tls(None) is None
    assert Registry(pki / "devices.json").devices["light-C-01"]["cabinet"] == "C-01"     # saved, another process sees it
    assert reg.assign("light-C-01", None) is None                                       # never routed through a district
    plain = reg.enroll_lighting("light-C-02", "C-02", new_pub())                         # no place, a serial is made
    assert plain["lon"] is None and plain["serial"].startswith("URB-")


@pytest.mark.parametrize("device", ["edge-C-01", "light-", "light-a b", "light-a/b", "light-" + "x" * 55, "C-01", ""])
def test_enroll_lighting_refuses_a_bad_device_id(pki, device):
    with pytest.raises(ValueError, match="light-<cabinet>"):
        Registry(pki / "devices.json").enroll_lighting(device, "C-01", new_pub())


@pytest.mark.parametrize("cabinet", ["", "C 01", "C/01", "C-01;x", "x" * 41, "Ш-1"])
def test_enroll_lighting_refuses_a_bad_cabinet(pki, cabinet):
    with pytest.raises(ValueError, match="cabinet"):
        Registry(pki / "devices.json").enroll_lighting("light-C-01", cabinet, new_pub())


@pytest.mark.parametrize("kw", [{"lon": 181, "lat": 40}, {"lon": 71, "lat": -91}, {"lon": float("nan"), "lat": 1},
                                {"lon": "71", "lat": 40}, {"lon": 71}, {"lat": 40}])
def test_enroll_lighting_refuses_a_bad_place(pki, kw):
    with pytest.raises(ValueError):
        Registry(pki / "devices.json").enroll_lighting("light-C-01", "C-01", new_pub(), **kw)


def test_enroll_lighting_refuses_a_bad_key_serial_and_a_key_in_use(pki):
    reg = Registry(pki / "devices.json")
    with pytest.raises(ValueError):
        reg.enroll_lighting("light-C-01", "C-01", "not hex")
    with pytest.raises(ValueError, match="serial"):
        reg.enroll_lighting("light-C-01", "C-01", new_pub(), serial="a b")
    pub = new_pub()
    reg.enroll_lighting("light-C-01", "C-01", pub)
    with pytest.raises(ValueError, match="already enrolled as light-C-01"):
        reg.enroll_lighting("light-C-02", "C-02", pub)         # one key, one device
    with pytest.raises(ValueError, match="already enrolled as"):
        reg.enroll("edge-J-01", JUNCTIONS[0], pub)


def test_one_active_unit_per_cabinet_a_revoked_one_frees_it(pki):
    reg = Registry(pki / "devices.json")
    reg.enroll_lighting("light-C-01", "C-01", new_pub())
    with pytest.raises(ValueError, match="cabinet C-01 already has unit light-C-01"):
        reg.enroll_lighting("light-C-01b", "C-01", new_pub())
    old = reg.devices["light-C-01"]["public_key"]
    again = reg.enroll_lighting("light-C-01", "C-01", new_pub())      # the same unit, a replaced box: allowed
    assert again["public_key"] != old and "first_seen" not in again
    reg.set_status("light-C-01", REVOKED)
    reg.enroll_lighting("light-C-01b", "C-01", new_pub())             # the cabinet is free once the unit is revoked
    assert reg.remove("light-C-01")["id"] == "light-C-01" and "light-C-01" not in reg.devices


def test_revoke_status_remove_work_for_lighting_as_for_units(pki):
    reg = Registry(pki / "devices.json")
    key = crypto.new_key()
    reg.enroll_lighting("light-C-01", "C-01", crypto.public_hex(key))
    sig = crypto.sign(key, {"device": "light-C-01", "nonce": "n"})
    assert reg.authenticate("light-C-01", "n", sig) is not None
    with pytest.raises(ValueError, match="revoke"):
        reg.remove("light-C-01")                                       # never the first step
    assert reg.set_status("light-C-01", REVOKED)["status"] == REVOKED
    assert reg.authenticate("light-C-01", "n", sig) is None
    reg.remove("light-C-01")
    assert reg.devices == {}


def test_a_light_id_cannot_take_over_a_unit_or_a_district_record(pki):
    reg = Registry(pki / "devices.json")
    reg.enroll("edge-J-01", JUNCTIONS[0], new_pub())
    reg.devices["light-X"] = {**reg.devices["edge-J-01"], "id": "light-X", "kind": "district", "public_key": new_pub()}
    with pytest.raises(ValueError, match="already enrolled as a district"):
        reg.enroll_lighting("light-X", "C-09", new_pub())


# ---------------------------------------------------------------- the kit and the CLI

def test_a_lighting_units_kit_has_the_unit_format_and_no_junction(pki):
    reg = Registry(pki / "devices.json")
    rec = reg.enroll_lighting("light-C-01", "C-01", new_pub(), "URB-L1", 71.78, 40.38)
    kit = make_kit(rec, pki, "centre.city", 8443)
    assert kit["urban_kit"] == 1 and kit["unit"] == "light-C-01" and kit["kind"] == "lighting"
    assert kit["junction"] is None and kit["approaches"] == [] and kit["via"] is None
    assert kit["platform"] == "centre.city:8443" and kit["fingerprint"] == crypto.fingerprint(rec["public_key"])
    assert kit["ca_pem"].startswith("-----BEGIN CERTIFICATE-----") and kit["serial"] == "URB-L1"
    assert (kit["cabinet"], kit["lon"], kit["lat"]) == ("C-01", 71.78, 40.38)
    unit = make_kit(Registry(pki / "devices.json").enroll("edge-J-01", JUNCTIONS[0], new_pub()), pki, "centre.city", 8443)
    assert "cabinet" not in unit                                         # a junction unit's kit is unchanged


def run_cli(monkeypatch, pki, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["provision", *argv])
    monkeypatch.setattr(c, "PKI", pki)
    monkeypatch.setattr(c, "SOURCE", "field")
    monkeypatch.setattr(provision_cli, "load_all_junctions", lambda: JUNCTIONS)
    try:
        provision_cli.main()
        code = 0
    except SystemExit as err:
        code = err.code if isinstance(err.code, str) else 0
    out = capsys.readouterr().out
    return code, out


def test_provision_enroll_lighting_list_and_kit(monkeypatch, pki, capsys):
    pub = new_pub()
    code, out = run_cli(monkeypatch, pki, capsys, "enroll-lighting", "light-C-01", "--cabinet", "C-01", "--public", pub,
                        "--serial", "URB-L1", "--lon", "71.78", "--lat", "40.38")
    assert code == 0 and "enrolled lighting light-C-01 for cabinet C-01 (URB-L1)" in out and crypto.fingerprint(pub) in out
    rec = Registry(pki / "devices.json").devices["light-C-01"]
    assert rec["kind"] == "lighting" and (rec["lon"], rec["lat"]) == (71.78, 40.38)

    code, out = run_cli(monkeypatch, pki, capsys, "list")
    line = next(ln for ln in out.splitlines() if ln.startswith("light-C-01"))
    assert "lighting" in line and "cabinet C-01" in line and "active" in line and crypto.fingerprint(pub) in line

    code, out = run_cli(monkeypatch, pki, capsys, "kit", "light-C-01")
    kit = json.loads(out)
    assert kit["unit"] == "light-C-01" and kit["kind"] == "lighting" and kit["junction"] is None
    assert kit["fingerprint"] == crypto.fingerprint(pub) and kit["approaches"] == []


def test_provision_enroll_lighting_refusals_are_plain_messages(monkeypatch, pki, capsys):
    ok = ("enroll-lighting", "light-C-01", "--cabinet", "C-01", "--public", new_pub())
    assert run_cli(monkeypatch, pki, capsys, *ok)[0] == 0
    code, _ = run_cli(monkeypatch, pki, capsys, "enroll-lighting", "light-C-02", "--cabinet", "C-01", "--public", new_pub())
    assert "cabinet C-01 already has unit light-C-01" in code
    code, _ = run_cli(monkeypatch, pki, capsys, "enroll-lighting", "edge-X", "--cabinet", "C-03", "--public", new_pub())
    assert "light-<cabinet>" in code
    code, _ = run_cli(monkeypatch, pki, capsys, "enroll-lighting", "light-C-03", "--cabinet", "C-03", "--public", "zz")
    assert code


# ---------------------------------------------------------------- the hub and a lighting device

class LightWS:
    """A cabinet's end of the socket: answers the challenge with its key, sends the given reports, then drops."""

    def __init__(self, key, device, reports):
        self.key, self.device, self.todo = key, device, list(reports)
        self.sent, self.closed = [], None

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def receive_text(self):
        if len(self.sent) == 1:
            return json.dumps({"type": "hello", "device": self.device, "version": "0.1.0", "epoch": 1,
                               "sig": crypto.sign(self.key, {"device": self.device, "nonce": self.sent[0]["nonce"]})})
        if self.todo:
            return json.dumps(self.todo.pop(0))
        raise WebSocketDisconnect()

    async def close(self, code=1000):
        self.closed = code


def test_a_lighting_device_authenticates_and_gets_no_plans(pki):
    reg = Registry(pki / "devices.json")
    key = crypto.new_key()
    reg.enroll_lighting("light-C-01", "C-01", crypto.public_hex(key))
    seen = []
    hub = DeviceHub(reg, crypto.load_private((pki / "gateway.key").read_text()), on_report=lambda rec, r: seen.append(rec),
                    plans={"T1": {1, 2}, None: {7}, "": {8}}, clock=lambda: 100)
    ws = LightWS(key, "light-C-01", [{"type": "report", "t": 1, "health": {}}])
    asyncio.run(hub.serve(ws))
    welcome = ws.sent[1]
    assert welcome["type"] == "welcome" and welcome["plans"] == [] and welcome["tls"] is None
    assert welcome["cabinet"] == "C-01" and welcome["t"] == 100
    assert [r["id"] for r in seen] == ["light-C-01"] and kind_of(seen[0]) == "lighting"
    assert [m["type"] for m in ws.sent] == ["challenge", "welcome"]        # nothing else is ever sent down on its own
    assert reg.devices["light-C-01"]["first_seen"]                          # marked seen like any device


def test_a_revoked_lighting_device_cannot_connect_and_a_stranger_is_refused(pki):
    reg = Registry(pki / "devices.json")
    key = crypto.new_key()
    reg.enroll_lighting("light-C-01", "C-01", crypto.public_hex(key))
    hub = DeviceHub(reg, crypto.new_key(), on_report=lambda rec, r: pytest.fail("a report was accepted"))
    stranger = LightWS(crypto.new_key(), "light-C-01", [{"type": "report"}])
    asyncio.run(hub.serve(stranger))
    assert stranger.closed == 4401 and "light-C-01" not in hub.sessions
    asyncio.run(hub.set_status("light-C-01", REVOKED))
    revoked = LightWS(key, "light-C-01", [{"type": "report"}])
    asyncio.run(hub.serve(revoked))
    assert revoked.closed == 4401


def test_the_hub_offers_an_os_update_to_a_lighting_device_but_never_to_an_unknown_kind(pki, tmp_path):
    reg = Registry(pki / "devices.json")
    key = crypto.new_key()
    reg.enroll_lighting("light-C-01", "C-01", crypto.public_hex(key))
    store = ReleaseStore(tmp_path / "releases")
    pack = tmp_path / "bundle.raucb"
    pack.write_bytes(b"bytes of a bundle")
    store.add(pack, 2)
    hub = DeviceHub(reg, crypto.load_private((pki / "gateway.key").read_text()), on_report=lambda rec, r: None,
                    releases=store)
    ws = LightWS(key, "light-C-01", [{"type": "report", "t": 1, "health": {"os": 1}}])
    asyncio.run(hub.serve(ws))
    offer = next(m for m in ws.sent if m["type"] == "cmd")
    assert offer["device"] == "light-C-01" and offer["body"]["update"]["version"] == 2
    assert offer["body"]["update"]["path"].startswith("/update/") and "plans" not in offer["body"]
    rec = reg.devices["light-C-01"]
    hub.seen["light-C-01"] = {"at": 0, "health": {"os": 2}}
    assert hub.pending_release("light-C-01", rec) is None                  # up to date: nothing to offer
    hub.seen["light-C-01"] = {"at": 0, "health": {"os": 1}}
    assert hub.pending_release("light-C-01", rec)["version"] == 2
    assert hub.pending_release("light-C-01", {**rec, "kind": "water"})["version"] == 2  # a pump station runs URBAN OS
    assert hub.pending_release("light-C-01", {**rec, "kind": "power"}) is None


def test_the_hub_lists_lighting_cabinets_with_their_kind_and_cabinet_and_serves_them_by_kind(pki):
    reg = Registry(pki / "devices.json")
    reg.enroll("edge-J-01", JUNCTIONS[0], new_pub())
    reg.enroll_lighting("light-C-01", "C-01", new_pub(), "URB-L1")
    hub = DeviceHub(reg, crypto.new_key(), on_report=lambda rec, r: None)
    hub.sessions["light-C-01"] = object()
    rows = {r["id"]: r for r in hub.status()}
    assert rows["light-C-01"]["kind"] == "lighting" and rows["light-C-01"]["cabinet"] == "C-01"
    assert rows["light-C-01"]["online"] and rows["light-C-01"]["junction"] is None
    assert rows["edge-J-01"]["kind"] == "unit" and rows["edge-J-01"]["cabinet"] is None
    assert [d["id"] for d in hub.devices_of("lighting")] == ["light-C-01"]
    assert [d["id"] for d in hub.devices_of("unit")] == ["edge-J-01"] and hub.devices_of("water") == []
    assert hub.is_online("light-C-01") and not hub.is_online("edge-J-01")
    assert hub.online("T1") is False                                         # online() is by junction, as before
    hub.devices_of("lighting")[0]["cabinet"] = "changed"                      # a copy: the registry is not touched
    assert reg.devices["light-C-01"]["cabinet"] == "C-01"


def test_a_lighting_device_is_never_a_junction_commands_do_not_reach_it(pki):
    reg = Registry(pki / "devices.json")
    reg.enroll_lighting("light-C-01", "C-01", new_pub())
    hub = DeviceHub(reg, crypto.new_key(), on_report=lambda rec, r: None)
    sock = SimpleNamespace(sent=[])

    async def send_text(text):
        sock.sent.append(text)
    sock.send_text = send_text
    hub.sessions["light-C-01"] = sock

    async def go():
        hub.send({"tls": "", "command": "tls.select_plan"})
        hub.send({"tls": None, "command": "tls.select_plan"})
        await asyncio.sleep(0)
    asyncio.run(go())
    assert sock.sent == []


# ---------------------------------------------------------------- a lighting cabinet's unit: code, kit, env file

def light_unit(tmp_path, pki, **place):
    """keygen on the cabinet's unit, then enrolment of the public half: (unit pki dir, key path, registry record)."""
    from urban.edge.__main__ import keygen
    unit_pki = tmp_path / "unit" / "pki"
    keygen(SimpleNamespace(unit="light-C-01", key=None, pki=str(unit_pki), serial="URB-L1"))
    key_path = unit_pki / "edge" / "light-C-01.key"
    public = crypto.public_hex(crypto.load_private(key_path.read_text()))
    rec = Registry(pki / "devices.json").enroll_lighting("light-C-01", "C-01", public, "URB-L1", **place)
    return unit_pki, key_path, rec


def test_a_lighting_unit_prints_a_urban1_code_that_parses_and_bad_ids_do_not(tmp_path, capsys):
    from urban.devices.enrolment import make_code, parse_code
    from urban.edge.__main__ import keygen
    keygen(SimpleNamespace(unit="light-C-01", key=None, pki=str(tmp_path / "pki"), serial="URB-L1"))
    out = capsys.readouterr().out
    code = parse_code(next(line for line in out.splitlines() if "URBAN1:" in line))
    key = crypto.load_private((tmp_path / "pki" / "edge" / "light-C-01.key").read_text())
    assert code == {"device": "light-C-01", "serial": "URB-L1", "public": crypto.public_hex(key)}
    for bad in ("light-", "light-a b", "lamp-C-01", "light-" + "x" * 55):
        with pytest.raises(ValueError):
            make_code(bad, "", code["public"])
    assert parse_code(make_code("edge-J-07", "", code["public"]))["device"] == "edge-J-07"       # the others still work
    assert parse_code(make_code("district-1", "", code["public"]))["device"] == "district-1"


def test_install_kit_writes_a_lighting_env_file_and_no_junction_file(tmp_path, pki):
    from urban.devices.enrolment import install_kit
    unit_pki, key_path, rec = light_unit(tmp_path, pki, lon=71.78, lat=40.38)
    kit = make_kit(rec, pki, "centre.city", 8443)
    env = tmp_path / "unit" / "edge-light-C-01.env"
    written = install_kit(kit, "light-C-01", key_path, unit_pki, env)
    assert written == [str(unit_pki / "ca.pem"), str(unit_pki / "root.pub"), str(env)]
    assert (unit_pki / "ca.pem").read_text() == kit["ca_pem"] and not list(env.parent.glob("*.junction.json"))
    lines = [ln for ln in env.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    assert lines == ["PLATFORM=centre.city:8443", "SERVER_NAME=centre.city", "CABINET=C-01", "LON=71.78", "LAT=40.38",
                     "ADAPTER=emulated"]
    # setting up again: the kit's lines are rewritten, what the installer chose and added stays
    env.write_text(env.read_text(encoding="utf-8").replace("ADAPTER=emulated", "ADAPTER=relay") + "RELAY_DEVICE=/dev/ttyUSB0\n",
                   encoding="utf-8")
    install_kit({**kit, "platform": "other.city:8443", "server_name": "other.city", "lat": 40.5}, "light-C-01",
                key_path, unit_pki, env)
    lines = [ln for ln in env.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    assert lines == ["PLATFORM=other.city:8443", "SERVER_NAME=other.city", "CABINET=C-01", "LON=71.78", "LAT=40.5",
                     "ADAPTER=relay", "RELAY_DEVICE=/dev/ttyUSB0"]


def test_a_lighting_kit_without_a_place_writes_no_lon_lat_and_an_old_env_gets_an_adapter(tmp_path, pki):
    from urban.devices.enrolment import install_kit
    unit_pki, key_path, rec = light_unit(tmp_path, pki)
    kit = make_kit(rec, pki, "centre.city", 8443)
    env = tmp_path / "unit" / "edge-light-C-01.env"
    install_kit(kit, "light-C-01", key_path, unit_pki, env)
    text = env.read_text(encoding="utf-8")
    assert "LON=" not in text and "LAT=" not in text and "CABINET=C-01" in text and "ADAPTER=emulated" in text
    env.write_text("PLATFORM=x\nSERVER_NAME=y\n", encoding="utf-8")                 # an env file with no ADAPTER yet
    install_kit(kit, "light-C-01", key_path, unit_pki, env)
    assert env.read_text(encoding="utf-8").splitlines() == ["PLATFORM=centre.city:8443", "SERVER_NAME=centre.city",
                                                            "CABINET=C-01", "ADAPTER=emulated"]


def test_install_kit_refuses_a_bad_lighting_kit(tmp_path, pki):
    from urban.devices.enrolment import install_kit
    unit_pki, key_path, rec = light_unit(tmp_path, pki, lon=71.78, lat=40.38)
    kit = make_kit(rec, pki, "centre.city", 8443)
    env = tmp_path / "unit" / "edge-light-C-01.env"
    for bad in ({"cabinet": None}, {"cabinet": "C 01"}, {"cabinet": "C-01\nADAPTER=x"}, {"lon": "71"}, {"lat": 95},
                {"platform": "a\nb"}):
        with pytest.raises(ValueError):
            install_kit({**kit, **bad}, "light-C-01", key_path, unit_pki, env)
    with pytest.raises(ValueError, match="for light-C-01"):
        install_kit(kit, "light-C-02", key_path, unit_pki, env)
    other = Registry(pki / "devices.json").enroll_lighting("light-C-01", "C-01", new_pub())  # the box was replaced
    with pytest.raises(ValueError, match="enrol this unit's code again"):
        install_kit(make_kit(other, pki, "centre.city", 8443), "light-C-01", key_path, unit_pki, env)
    assert not env.exists()


def test_hub_enroll_lighting_closes_the_old_session(pki):
    class WS:
        closed = None

        async def close(self, code=1000):
            self.closed = code
    hub = DeviceHub(Registry(pki / "devices.json"), crypto.new_key(), on_report=lambda rec, r: None)
    ws = hub.sessions["light-C-01"] = WS()
    rec = asyncio.run(hub.enroll_lighting("light-C-01", "C-01", new_pub(), "URB-2", 71.0, 40.0))
    assert rec["cabinet"] == "C-01" and ws.closed == 4403 and "light-C-01" not in hub.sessions


# ---------------------------------------------------------------- pump stations (the water module) in the registry

def test_enroll_water_makes_a_water_record_that_is_nobodys_junction_with_the_centres_limits(pki):
    reg = Registry(pki / "devices.json")
    rec = reg.enroll_water("pump-S-01", "S-01", new_pub(), "WTR-1", 71.78, 40.38, 2.0, 6.5)
    assert rec["kind"] == "water" == kind_of(rec) and rec["site"] == "S-01" and rec["status"] == ACTIVE
    assert (rec["lon"], rec["lat"], rec["p_min"], rec["p_max"], rec["serial"], rec["junction"], rec["tls"]) == \
        (71.78, 40.38, 2.0, 6.5, "WTR-1", None, None)
    assert Registry(pki / "devices.json").devices["pump-S-01"]["site"] == "S-01"          # saved
    assert reg.assign("pump-S-01", None) is None                                         # never through a district
    plain = reg.enroll_water("pump-S-02", "S-02", new_pub())                             # no place, no limits
    assert (plain["lon"], plain["p_min"], plain["p_max"]) == (None, None, None) and plain["serial"].startswith("URB-")
    assert reg.enroll_water("pump-S-03", "S-03", new_pub(), p_min=1)["p_min"] == 1.0     # one limit alone is fine
    assert reg.wg_report("pump-S-01", "x" * 44) == ("none", None)                        # no tunnel, ever


@pytest.mark.parametrize("device", ["edge-S-01", "pump-", "pump-a b", "pump-a/b", "pump-" + "x" * 55, "light-S-01", ""])
def test_enroll_water_refuses_a_bad_device_id(pki, device):
    with pytest.raises(ValueError, match="pump-<site>"):
        Registry(pki / "devices.json").enroll_water(device, "S-01", new_pub())


@pytest.mark.parametrize("site", ["", "S 01", "S/01", "S-01;x", "x" * 41, "Ш-1", None])
def test_enroll_water_refuses_a_bad_site(pki, site):
    with pytest.raises(ValueError, match="site"):
        Registry(pki / "devices.json").enroll_water("pump-S-01", site, new_pub())


@pytest.mark.parametrize("kw", [{"p_min": 6, "p_max": 2}, {"p_min": 3, "p_max": 3}, {"p_min": float("nan")},
                                {"p_max": float("inf")}, {"p_min": -2}, {"p_max": 101}, {"p_min": "2"}, {"p_max": True},
                                {"lon": 181, "lat": 40}, {"lon": 71}, {"lat": 40}, {"lon": float("nan"), "lat": 1}])
def test_enroll_water_refuses_bad_limits_and_a_bad_place(pki, kw):
    with pytest.raises(ValueError):
        Registry(pki / "devices.json").enroll_water("pump-S-01", "S-01", new_pub(), **kw)


def test_one_active_unit_per_site_one_key_one_device_and_no_takeover(pki):
    reg = Registry(pki / "devices.json")
    pub = new_pub()
    reg.enroll_water("pump-S-01", "S-01", pub)
    with pytest.raises(ValueError, match="site S-01 already has unit pump-S-01"):
        reg.enroll_water("pump-S-01b", "S-01", new_pub())
    with pytest.raises(ValueError, match="already enrolled as pump-S-01"):
        reg.enroll_water("pump-S-02", "S-02", pub)
    again = reg.enroll_water("pump-S-01", "S-01", new_pub(), p_min=2.5)              # a replaced box: allowed
    assert again["public_key"] != pub and again["p_min"] == 2.5
    reg.set_status("pump-S-01", REVOKED)
    reg.enroll_water("pump-S-01b", "S-01", new_pub())                                 # free once revoked
    reg.enroll_lighting("light-X", "C-09", new_pub())
    reg.devices["pump-X"] = {**reg.devices["light-X"], "id": "pump-X", "public_key": new_pub()}
    with pytest.raises(ValueError, match="already enrolled as a lighting"):
        reg.enroll_water("pump-X", "S-09", new_pub())


def test_a_water_units_kit_carries_its_site_place_and_limits_and_no_junction(pki):
    rec = Registry(pki / "devices.json").enroll_water("pump-S-01", "S-01", new_pub(), "WTR-1", 71.78, 40.38, 2.0, 6.0)
    kit = make_kit(rec, pki, "centre.city", 8443)
    assert kit["kind"] == "water" and kit["unit"] == "pump-S-01" and kit["junction"] is None and kit["via"] is None
    assert (kit["site"], kit["lon"], kit["lat"], kit["p_min"], kit["p_max"]) == ("S-01", 71.78, 40.38, 2.0, 6.0)
    assert "cabinet" not in kit and kit["approaches"] == []


def test_provision_enroll_water_list_and_kit(monkeypatch, pki, capsys):
    pub = new_pub()
    code, out = run_cli(monkeypatch, pki, capsys, "enroll-water", "pump-S-01", "--site", "S-01", "--public", pub,
                        "--serial", "WTR-1", "--lon", "71.78", "--lat", "40.38", "--p-min", "2", "--p-max", "6")
    assert code == 0 and "enrolled water pump-S-01 for site S-01 (WTR-1), pressure 2.0..6.0 bar" in out
    assert crypto.fingerprint(pub) in out
    code, out = run_cli(monkeypatch, pki, capsys, "list")
    line = next(ln for ln in out.splitlines() if ln.startswith("pump-S-01"))
    assert "water" in line and "site S-01" in line and "active" in line
    code, out = run_cli(monkeypatch, pki, capsys, "kit", "pump-S-01")
    assert json.loads(out)["site"] == "S-01"
    code, _ = run_cli(monkeypatch, pki, capsys, "enroll-water", "pump-S-02", "--site", "S-02", "--public", new_pub(),
                      "--p-min", "6", "--p-max", "2")
    assert "p_min must be below p_max" in code
    code, _ = run_cli(monkeypatch, pki, capsys, "enroll-water", "pump-S-03", "--site", "S-01", "--public", new_pub())
    assert "site S-01 already has unit pump-S-01" in code


def test_a_pump_station_authenticates_gets_its_site_no_plans_and_an_os_update(pki, tmp_path):
    reg = Registry(pki / "devices.json")
    key = crypto.new_key()
    reg.enroll_water("pump-S-01", "S-01", crypto.public_hex(key))
    store = ReleaseStore(tmp_path / "releases")
    pack = tmp_path / "bundle.raucb"
    pack.write_bytes(b"bytes of a bundle")
    store.add(pack, 2)
    seen = []
    hub = DeviceHub(reg, crypto.load_private((pki / "gateway.key").read_text()), on_report=lambda rec, r: seen.append(rec),
                    plans={"T1": {1}, None: {7}}, releases=store)
    ws = LightWS(key, "pump-S-01", [{"type": "report", "t": 1, "health": {"os": 1}, "water": {"site": "S-01"}}])
    asyncio.run(hub.serve(ws))
    welcome = ws.sent[1]
    assert welcome["plans"] == [] and welcome["tls"] is None and welcome["site"] == "S-01" and "cabinet" not in welcome
    assert [r["id"] for r in seen] == ["pump-S-01"] and kind_of(seen[0]) == "water"
    offer = next(m for m in ws.sent if m["type"] == "cmd")
    assert set(offer["body"]) == {"update"} and offer["body"]["update"]["version"] == 2   # the only thing sent down
    rows = {r["id"]: r for r in hub.status()}
    assert rows["pump-S-01"]["kind"] == "water" and rows["pump-S-01"]["site"] == "S-01" and rows["pump-S-01"]["wg"] is None
    assert hub.floor_verdict("pump-S-01") is None and [d["id"] for d in hub.devices_of("water")] == ["pump-S-01"]


def test_hub_enroll_water_closes_the_old_session(pki):
    class WS:
        closed = None

        async def close(self, code=1000):
            self.closed = code
    hub = DeviceHub(Registry(pki / "devices.json"), crypto.new_key(), on_report=lambda rec, r: None)
    ws = hub.sessions["pump-S-01"] = WS()
    rec = asyncio.run(hub.enroll_water("pump-S-01", "S-01", new_pub(), "WTR-2", 71.0, 40.0, 1.5, 5.0))
    assert rec["site"] == "S-01" and rec["p_max"] == 5.0 and ws.closed == 4403 and "pump-S-01" not in hub.sessions


def test_the_platform_shows_a_pump_station_as_a_device_of_its_own_not_a_junction():
    p = Platform([71.78, 40.38], 1000, [CAM])
    p.devices([{"id": "pump-S-01", "kind": "water", "site": "S-01", "junction": None, "tls": None, "status": "active",
                "online": True, "age": 1, "health": {}, "fingerprint": "9999-0000-1111-2222"}], 10)
    assert p.dev["pump-S-01"]["k"] == "water" and p.dev["pump-S-01"]["site"] == "S-01" and "cab" not in p.dev["pump-S-01"]
    p.on_report({"id": "pump-S-01", "kind": "water", "tls": None}, {"queues": {"a": 1}, "water": {}})
    assert p.sensors == {}


def test_the_demo_twin_gets_three_pump_stations_with_limits_next_to_its_junctions(tmp_path):
    placed = [{"id": f"J-0{n}", "tls": f"T{n}", "name": "x", "lon": 71.78 + n / 100, "lat": 40.38} for n in range(1, 6)]
    provision(placed, tmp_path)
    reg = Registry(tmp_path / "devices.json")
    water = sorted(d["id"] for d in reg.devices.values() if kind_of(d) == "water")
    assert water == ["pump-S-01", "pump-S-02", "pump-S-03"]
    rec = reg.devices["pump-S-01"]
    assert (rec["site"], rec["p_min"], rec["p_max"], rec["junction"]) == ("S-01", 2.0, 6.0, None)
    assert all((tmp_path / "edge" / f"{d}.key").exists() for d in water)
    assert provision(placed, tmp_path) is False                                            # idempotent


def water_unit(tmp_path, pki, **place):
    from urban.edge.__main__ import keygen
    unit_pki = tmp_path / "unit" / "pki"
    keygen(SimpleNamespace(unit="pump-S-01", key=None, pki=str(unit_pki), serial="WTR-1"))
    key_path = unit_pki / "edge" / "pump-S-01.key"
    public = crypto.public_hex(crypto.load_private(key_path.read_text()))
    rec = Registry(pki / "devices.json").enroll_water("pump-S-01", "S-01", public, "WTR-1", **place)
    return unit_pki, key_path, rec


def test_a_pump_unit_prints_a_urban1_code_that_parses(tmp_path, capsys):
    from urban.devices.enrolment import is_water, make_code, parse_code
    from urban.edge.__main__ import keygen
    keygen(SimpleNamespace(unit="pump-S-01", key=None, pki=str(tmp_path / "pki"), serial="WTR-1"))
    code = parse_code(next(line for line in capsys.readouterr().out.splitlines() if "URBAN1:" in line))
    assert code["device"] == "pump-S-01" and code["serial"] == "WTR-1" and is_water(code["device"])
    for bad in ("pump-", "pump-a b", "pumps-S-01", "pump-" + "x" * 55):
        with pytest.raises(ValueError):
            make_code(bad, "", code["public"])


def test_install_kit_writes_a_water_env_file_0600_and_no_junction_file_and_keeps_the_installers_lines(tmp_path, pki):
    import os
    from urban.devices.enrolment import install_kit
    unit_pki, key_path, rec = water_unit(tmp_path, pki, lon=71.78, lat=40.38, p_min=2.0, p_max=6.0)
    kit = make_kit(rec, pki, "centre.city", 8443)
    env = tmp_path / "unit" / "edge-pump-S-01.env"
    written = install_kit(kit, "pump-S-01", key_path, unit_pki, env)
    assert written == [str(unit_pki / "ca.pem"), str(unit_pki / "root.pub"), str(env)]
    assert not list(env.parent.glob("*.junction.json"))
    if os.name == "posix":
        assert env.stat().st_mode & 0o777 == 0o600
    lines = [ln for ln in env.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    assert lines == ["PLATFORM=centre.city:8443", "SERVER_NAME=centre.city", "SITE=S-01", "LON=71.78", "LAT=40.38",
                     "ADAPTER=emulated", "MAP=", "MODBUS_HOST=", "MODBUS_PORT=502", "SLAVE=1", "SERIAL=", "BAUD=9600"]
    text = env.read_text(encoding="utf-8").replace("ADAPTER=emulated", "ADAPTER=modbus-tcp").replace("MODBUS_HOST=", "MODBUS_HOST=10.0.0.5")
    env.write_text(text.replace("MAP=", "MAP=/etc/urban/water-map.json"), encoding="utf-8")
    install_kit({**kit, "platform": "other.city:8443", "server_name": "other.city", "lat": 40.5}, "pump-S-01",
                key_path, unit_pki, env)
    lines = [ln for ln in env.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    assert lines == ["PLATFORM=other.city:8443", "SERVER_NAME=other.city", "SITE=S-01", "LON=71.78", "LAT=40.5",
                     "ADAPTER=modbus-tcp", "MAP=/etc/urban/water-map.json", "MODBUS_HOST=10.0.0.5", "MODBUS_PORT=502", "SLAVE=1",
                     "SERIAL=", "BAUD=9600"]
    env.write_text("PLATFORM=x\nSERVER_NAME=y\n", encoding="utf-8")                   # an env file with no ADAPTER yet
    install_kit({**kit, "lon": None, "lat": None}, "pump-S-01", key_path, unit_pki, env)
    assert env.read_text(encoding="utf-8").splitlines() == ["PLATFORM=centre.city:8443", "SERVER_NAME=centre.city",
                                                            "SITE=S-01", "ADAPTER=emulated"]


def test_install_kit_refuses_a_bad_water_kit(tmp_path, pki):
    from urban.devices.enrolment import install_kit
    unit_pki, key_path, rec = water_unit(tmp_path, pki, lon=71.78, lat=40.38)
    kit = make_kit(rec, pki, "centre.city", 8443)
    env = tmp_path / "unit" / "edge-pump-S-01.env"
    for bad in ({"site": None}, {"site": "S 01"}, {"site": "S-01\nADAPTER=x"}, {"lon": "71"}, {"lat": 95},
                {"p_min": "2"}, {"p_max": 1000}, {"platform": "a\nb"}):
        with pytest.raises(ValueError):
            install_kit({**kit, **bad}, "pump-S-01", key_path, unit_pki, env)
    assert not env.exists()


def test_enrolment_by_code_takes_a_pump_code_with_its_site_and_limits(pki):
    """The centre's enrolment by code (api/app.py, POST /api/devices/enroll-code) for a pump station: the code the unit
    printed, the station and optionally the place and the pressure limits go to DeviceHub.enroll_water, audited. The app
    module starts a whole platform when imported, so its wiring is read here and the path it takes is run below."""
    from pathlib import Path
    import urban.api as api_pkg
    from urban.devices.enrolment import is_water, make_code, parse_code
    app = (Path(api_pkg.__file__).parent / "app.py").read_text(encoding="utf-8")
    assert "site: str = \"\"" in app and "p_min: float | None = None" in app and "p_max: float | None = None" in app
    assert "if is_water(code[\"device\"]):" in app and "body.site.strip()" in app
    assert "hub.enroll_water(code[\"device\"], body.site.strip(), code[\"public\"], code[\"serial\"]," in app
    assert "body.lon, body.lat, body.p_min, body.p_max)" in app and "\"kind\": \"water\"" in app
    key = crypto.new_key()
    code = parse_code(make_code("pump-S-01", "WTR-1", crypto.public_hex(key)))
    assert is_water(code["device"])
    hub = DeviceHub(Registry(pki / "devices.json"), crypto.new_key(), on_report=lambda rec, r: None)
    rec = asyncio.run(hub.enroll_water(code["device"], "S-01", code["public"], code["serial"], 71.78, 40.38, 2.0, 6.0))
    assert (rec["kind"], rec["site"], rec["serial"], rec["p_min"], rec["p_max"]) == ("water", "S-01", "WTR-1", 2.0, 6.0)
