"""The centre's version floor (G1). An admin sets the minimum system version (`python -m urban.release floor N`, or
POST /api/os-floor); it is kept next to the releases, shown, cleared, and every change is audited as os-floor-set
once, whoever made it. A unit, district server or lighting cabinet that does not show at least that version in its
current session (a known `os` below it, or no `os` at all while a floor is set) gets no commands: the gateway's
leases and a module's commands are dropped, the first refusal of a session is audited, the gateway cannot approve
anything at its junction and a lighting override naming it is refused with 409. It still gets update offers, and
it is commanded again as soon as it reports a version at or above the floor. Without a floor nothing changes."""
import asyncio
import json
import re

import pytest
from fastapi import FastAPI, Header, HTTPException, WebSocketDisconnect

from urban import release
from urban.devices import crypto
from urban.devices.hub import DeviceHub, floor_words
from urban.devices.registry import Registry, provision
from urban.gateway import CommandGateway
from urban.gateway.gateway import POLICY, SILENCE_MARGIN
from urban.modules.base import Core
from urban.modules.lighting import LightingModule
from urban.platform import Platform
from urban.release import ReleaseError, ReleaseStore

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"}]
WALL = 1_800_000_000
DEVICES = ("edge-J-01", "edge-J-02", "district-1", "light-C-01", "light-C-02")


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path / "pki")
    return tmp_path / "pki"


@pytest.fixture
def store(tmp_path):
    return ReleaseStore(tmp_path / "releases")


class Recorder:
    """A device's session: records what the hub sends it."""
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        pass


class Rig:
    """A hub with two junction units, a district server and two lighting cabinets, all with a session."""
    def __init__(self, pki, store):
        registry = Registry(pki / "devices.json")
        registry.enroll_district("district-1", "10.1.0.2", crypto.public_hex(crypto.new_key()))
        registry.enroll_lighting("light-C-01", "C-01", crypto.public_hex(crypto.new_key()), "URB-L1", 71.78, 40.38)
        registry.enroll_lighting("light-C-02", "C-02", crypto.public_hex(crypto.new_key()), "URB-L2", 71.79, 40.39)
        self.audited = []
        self.hub = DeviceHub(registry, crypto.load_private((pki / "gateway.key").read_text()),
                             on_report=lambda rec, rep: None, wall=lambda: WALL, releases=store,
                             audit=lambda event, target, note="", actor="platform":
                                 self.audited.append((event, target, note, actor)))
        self.sockets = {}
        for device in DEVICES:
            self.sockets[device] = self.hub.sessions[device] = Recorder()

    def events(self, name):
        return [a for a in self.audited if a[0] == name]

    def cmds(self, device):
        return [f["body"] for f in self.sockets[device].sent if f["type"] == "cmd"]


@pytest.fixture
def rig(pki, store):
    return Rig(pki, store)


def on_loop(fn):
    """Run fn() on an event loop, as the platform does, and let the deliveries it scheduled go out."""
    async def go():
        out = fn()
        for _ in range(3):
            await asyncio.sleep(0)
        return out
    return asyncio.run(go())


def lease(tls="T1"):
    return {"op": "lease", "id": "cmd-1", "tls": tls, "plan": 1, "ttl": 180}


def call(app, method, path, body=None, user="admin:ann"):
    """One request straight into the ASGI app (the test environment has no HTTP client library); the deliveries the
    request scheduled go out before the loop ends."""
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    if user:
        headers.append((b"x-user", user.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 1), "server": ("test", 80)}

    async def run():
        out, sent_body = {"status": None, "body": b""}, [False]

        async def receive():
            if sent_body[0]:
                await asyncio.sleep(3600)
            sent_body[0] = True
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
            elif message["type"] == "http.response.body":
                out["body"] += message.get("body", b"")

        await app(scope, receive, send)
        for _ in range(3):
            await asyncio.sleep(0)
        return out

    res = asyncio.run(run())
    return res["status"], (json.loads(res["body"]) if res["body"] else None)


def role(*allowed):
    def check(x_user: str | None = Header(None)):
        kind, _, name = (x_user or "").partition(":")
        if kind not in allowed:
            raise HTTPException(403, "no role for this")
        return name
    return check


viewer, operator, admin = role("viewer", "operator", "admin"), role("operator", "admin"), role("admin")


def package(tmp_path, name="urban-edge-5.raucb", data=b"a signed bundle, as far as this test cares"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------- no floor: nothing changes

def test_without_a_floor_every_device_is_commanded_as_before_and_nothing_is_audited(rig):
    hub = rig.hub
    hub.heard("edge-J-01", {"os": 1})
    hub.heard("light-C-01", {})  # reports no version at all
    assert hub.floor() is None
    assert on_loop(lambda: hub.send(lease())) is True
    assert on_loop(lambda: hub.send_device("light-C-01", {"lighting": {"cabinet": "C-01", "action": "release"}}))
    assert [c["op"] for c in rig.cmds("edge-J-01")] == ["lease"] and len(rig.cmds("light-C-01")) == 1
    assert hub.reachable("T1") and hub.reachable("T2") and hub.why_unreachable("T1") is None
    rows = {r["id"]: r for r in on_loop(hub.status)}
    assert all(r["below_floor"] is False and r["floor"] is None for r in rows.values())
    assert hub.floor_refusal("light-C-01") is None and rig.audited == []


# ---------------------------------------------------------------- the floor: stored, shown, cleared

def test_the_store_sets_shows_and_clears_the_floor_and_another_process_sees_it(store):
    assert store.floor() is None and store.floor_record() is None
    rec = store.set_floor(5, by="ann")
    assert (rec["floor"], rec["was"], rec["by"]) == (5, None, "ann")
    assert re.fullmatch(r"[0-9a-f]{16}", rec["id"]) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rec["at"])
    assert store.floor() == 5 and ReleaseStore(store.dir).floor() == 5  # another process (the platform) sees it
    again = store.set_floor(7, by="bob")
    assert (again["floor"], again["was"]) == (7, 5) and again["id"] != rec["id"]
    cleared = store.set_floor(None, by="ann")
    assert (cleared["floor"], cleared["was"]) == (None, 7)
    assert store.floor() is None and ReleaseStore(store.dir).floor() is None
    assert ReleaseStore(store.dir).floor_record()["by"] == "ann"  # who cleared it is kept
    for bad in (0, -1, True, "5", 2.0):
        with pytest.raises(ReleaseError):
            store.set_floor(bad)
    assert store.floor() is None
    assert not list(store.dir.glob("*.tmp"))  # written whole or not at all


def test_a_floor_file_that_is_not_a_record_we_write_is_ignored(store):
    store.set_floor(5)
    assert store.floor() == 5
    for junk in ("not json", json.dumps({"floor": "6", "id": "x"}), json.dumps({"floor": 0, "id": "x"}),
                 json.dumps({"floor": 6}), json.dumps([6])):
        (store.dir / release.FLOOR).write_text(junk, encoding="utf-8")
        assert store.floor() == 5  # the last good record stays
        assert ReleaseStore(store.dir).floor() is None  # a fresh reader has no good record


def test_the_cli_sets_shows_and_clears_the_floor_and_refuses_with_a_clear_line(tmp_path, capsys, monkeypatch):
    # `add` asks RAUC about the package (test_rollout.py has the stand-in RAUC): here RAUC is taken to accept it as x86
    monkeypatch.setattr(release, "check_bundle", lambda path, version: ("urban-edge-x86_64", "rauc verified it"))
    folder = str(tmp_path / "r")
    release.main(["--dir", folder, "floor"])
    assert capsys.readouterr().out.startswith("no floor")
    release.main(["--dir", folder, "floor", "5"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("floor set to 5 (was none)") and "get no commands" in out[0]
    assert out[1].startswith("warning: no release offered here is 5 or newer (newest: none)")  # nothing reaches it
    release.main(["--dir", folder, "floor"])
    assert re.match(r"floor: 5, set by cli\S*", capsys.readouterr().out)
    release.main(["--dir", folder, "add", str(package(tmp_path)), "--version", "5"])
    capsys.readouterr()
    release.main(["--dir", folder, "floor", "5"])
    out = capsys.readouterr().out.splitlines()
    assert out == [out[0]] and "(was 5)" in out[0]  # release 5 is offered: no warning
    release.main(["--dir", folder, "list"])
    assert "floor: 5" in capsys.readouterr().out
    release.main(["--dir", folder, "floor", "--clear"])
    assert capsys.readouterr().out.startswith("floor cleared (was 5)")
    assert ReleaseStore(tmp_path / "r").floor() is None
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", folder, "floor", "3", "--clear"])
    assert str(stop.value) == "refused: give a version or --clear, not both"
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", folder, "floor", "0"])
    assert str(stop.value).startswith("refused: the floor is a system version")
    assert ReleaseStore(tmp_path / "r").floor() is None


def test_a_floor_set_with_the_cli_is_audited_once_by_the_platform_and_not_again_after_a_restart(pki, store):
    rig = Rig(pki, store)
    release.main(["--dir", str(store.dir), "floor", "5"])
    assert rig.hub.floor() == 5 and rig.events("os-floor-set") == []  # off the event loop the chain is not written
    on_loop(rig.hub.status)
    [(event, target, note, actor)] = rig.events("os-floor-set")
    assert (target, note) == ("os-floor", "5 (was none)") and actor.startswith("cli")
    on_loop(rig.hub.status)
    on_loop(rig.hub.floor)
    assert len(rig.events("os-floor-set")) == 1
    restarted = Rig(pki, ReleaseStore(store.dir))  # the platform starts again: the record was audited already
    on_loop(restarted.hub.floor)
    assert restarted.events("os-floor-set") == []
    release.main(["--dir", str(store.dir), "floor", "--clear"])  # while the platform is down
    after = Rig(pki, ReleaseStore(store.dir))
    on_loop(after.hub.floor)
    assert [a[2] for a in after.events("os-floor-set")] == ["none (was 5)"]


def test_the_admin_sets_and_clears_the_floor_through_the_api_and_it_is_audited_under_the_admins_name(rig, store,
                                                                                                    tmp_path):
    app = FastAPI()
    app.include_router(rig.hub.floor_routes(viewer, admin))
    status, view = call(app, "GET", "/api/os-floor", user="viewer:vic")
    assert status == 200 and view == {"floor": None, "set_by": None, "at": None, "newest": None, "below": []}
    assert call(app, "POST", "/api/os-floor", {"floor": 5}, user="operator:olga")[0] == 403
    assert call(app, "POST", "/api/os-floor", {"floor": 5}, user="")[0] == 403
    assert store.floor() is None and rig.audited == []
    status, view = call(app, "POST", "/api/os-floor", {"floor": 5})
    assert status == 200 and view["floor"] == 5 and view["set_by"] == "ann" and "warning" in view
    assert view["below"] == sorted(DEVICES)  # none of them has shown a version yet
    assert rig.events("os-floor-set") == [("os-floor-set", "os-floor", "5 (was none)", "ann")]
    assert call(app, "GET", "/api/os-floor", user="viewer:vic")[1]["floor"] == 5
    assert len(rig.events("os-floor-set")) == 1  # looking at it again is not another change
    rig.hub.heard("edge-J-01", {"os": 5})
    assert "edge-J-01" not in call(app, "GET", "/api/os-floor", user="viewer:vic")[1]["below"]
    for bad in ({"floor": 0}, {"floor": -2}):
        status, err = call(app, "POST", "/api/os-floor", bad)
        assert status == 400 and "целое число" in err["detail"]
    for bad in ({"floor": "5"}, {"floor": True}, {"floor": 5.5}, {}):
        assert call(app, "POST", "/api/os-floor", bad)[0] == 422
    assert store.floor() == 5 and len(rig.events("os-floor-set")) == 1
    store.add(package(tmp_path), 5)
    status, view = call(app, "POST", "/api/os-floor", {"floor": 5})
    assert status == 200 and view["newest"] == 5 and "warning" not in view  # release 5 is offered: it can be reached
    status, view = call(app, "POST", "/api/os-floor", {"floor": None})
    assert status == 200 and view["floor"] is None and view["below"] == []
    assert [a[2:] for a in rig.events("os-floor-set")] == [("5 (was none)", "ann"), ("5 (was 5)", "ann"),
                                                          ("none (was 5)", "ann")]


# ---------------------------------------------------------------- below the floor: no commands, the refusal audited

def test_a_unit_below_the_floor_gets_nothing_through_the_gateway_and_the_refusal_is_audited_once(rig, store):
    hub = rig.hub
    store.set_floor(5)
    hub.heard("edge-J-01", {"os": 3})
    hub.heard("edge-J-02", {"os": 5})
    gateway = CommandGateway(send=hub.send, plans={"T1": {1}, "T2": {1}}, reachable=hub.reachable)

    def operate():
        p1 = gateway.propose(0, "tls.select_plan", {"tls": "T1", "plan": 1}, "queue")
        p2 = gateway.propose(0, "tls.select_plan", {"tls": "T2", "plan": 1}, "queue")
        refused = gateway.decide(p1["id"], True, "ann", 1)
        approved = gateway.decide(p2["id"], True, "ann", 1)
        return p1, refused, approved, hub.send(lease("T1")), hub.send(lease("T1"))
    p1, refused, approved, again, third = on_loop(operate)
    assert refused is None and p1["status"] == "pending"  # not applied: nobody we may command
    assert approved["status"] == "approved" and again is False and third is False
    assert rig.cmds("edge-J-01") == [] and [c["op"] for c in rig.cmds("edge-J-02")] == ["lease"]
    [(event, target, note, actor)] = rig.events("command-refused-below-floor")
    assert target == "edge-J-01" and note.startswith("os 3 < floor 5") and "lease" in note and actor == "platform"
    assert not hub.reachable("T1") and hub.reachable("T2") and hub.online("T1")  # connected, but not commanded
    assert hub.why_unreachable("T1") == f"блок перекрёстка edge-J-01: {floor_words(5, 3)}"
    assert "ниже допустимой версии (ОС 3 < 5): команды не отправляются" in hub.why_unreachable("T1")


def test_a_running_command_gets_no_renewal_once_its_unit_is_below_a_raised_floor_and_lapses(rig, store):
    hub = rig.hub
    store.set_floor(5)
    hub.heard("edge-J-01", {"os": 5})
    gateway = CommandGateway(send=hub.send, plans={"T1": {1}}, reachable=hub.reachable)
    p = on_loop(lambda: gateway.decide(gateway.propose(0, "tls.select_plan", {"tls": "T1", "plan": 1}, "q")["id"],
                                       True, "ann", 0))
    assert p["status"] == "approved" and len(rig.cmds("edge-J-01")) == 1
    store.set_floor(6)  # the admin raises the floor: no restart, the next renewal is not sent
    ttl = POLICY["tls.select_plan"].lease_ttl
    on_loop(lambda: gateway.tick(ttl // 3, {}))
    assert len(rig.cmds("edge-J-01")) == 1 and p.get("renewed") == 0
    on_loop(lambda: gateway.tick(ttl + SILENCE_MARGIN + 1, {}))
    assert p["status"] == "done" and p["result"]["reason"] == "expired"  # its lease ran out on the device by itself
    assert len(rig.cmds("edge-J-01")) == 1


def test_a_lighting_cabinet_below_the_floor_gets_no_module_command_and_the_refusal_is_audited_once(rig, store):
    hub = rig.hub
    store.set_floor(5)
    hub.heard("light-C-01", {"os": 4})
    body = {"lighting": {"cabinet": "C-01", "action": "set", "level": 60, "ttl": 120}}
    assert on_loop(lambda: [hub.send_device("light-C-01", body) for _ in range(3)]) == [False, False, False]
    assert rig.cmds("light-C-01") == []
    [(_, target, note, _)] = rig.events("command-refused-below-floor")
    assert target == "light-C-01" and note.startswith("os 4 < floor 5") and "lighting" in note
    assert hub.floor_refusal("light-C-01") == "ниже допустимой версии (ОС 4 < 5): команды не отправляются"


def test_update_offers_still_go_to_a_device_below_the_floor(rig, store, tmp_path):
    hub = rig.hub
    store.add(package(tmp_path), 5)
    store.set_floor(5)
    hub.heard("edge-J-01", {"os": 3})
    asyncio.run(hub._offer("edge-J-01", hub.registry.devices["edge-J-01"]))
    [offer] = rig.cmds("edge-J-01")
    assert offer["update"]["version"] == 5  # that is how it gets above the floor
    token = offer["update"]["path"].rsplit("/", 1)[1]
    assert hub.update_file(token) is not None
    assert not on_loop(lambda: hub.send(lease())) and rig.events("command-refused-below-floor")


def test_reaching_the_floor_restores_commands_at_once_and_a_relapse_is_refused_and_audited_again(rig, store):
    hub = rig.hub
    store.set_floor(5)
    hub.heard("edge-J-01", {"os": 4})
    assert on_loop(lambda: hub.send(lease())) is False
    hub.heard("edge-J-01", {"os": 5})  # it installed release 5 (a new report; no restart of anything)
    assert hub.reachable("T1") and on_loop(lambda: hub.send(lease())) is True
    assert [c["op"] for c in rig.cmds("edge-J-01")] == ["lease"]
    hub.heard("edge-J-01", {"os": 4})  # back on the old system (a fallback, or the disk swapped again)
    assert on_loop(lambda: [hub.send(lease()), hub.send(lease())]) == [False, False]
    assert [a[2].split(";")[0] for a in rig.events("command-refused-below-floor")] == ["os 4 < floor 5"] * 2
    assert len(rig.cmds("edge-J-01")) == 1


def test_a_device_that_shows_no_version_is_below_a_set_floor_and_only_then(rig, store):
    hub = rig.hub
    for health in ({}, {"os": True}, {"os": "7"}, {"os": -1}, {"os": 7.0}, None):  # no whole number: no version
        hub.heard("edge-J-01", health)
        assert hub.proven.get("edge-J-01") is None and hub.floor_verdict("edge-J-01") is None  # no floor: fine
    assert on_loop(lambda: hub.send(lease())) is True
    store.set_floor(1)  # even the lowest floor: it cannot prove it runs any of our systems
    assert hub.floor_verdict("edge-J-01") == (1, None) and not hub.reachable("T1")
    assert on_loop(lambda: hub.send(lease())) is False
    [(_, _, note, _)] = rig.events("command-refused-below-floor")
    assert note.startswith("os not reported in this session, floor 1")
    assert hub.floor_refusal("edge-J-01") == "версия ОС не подтверждена (допустимая от 1): команды не отправляются"
    store.set_floor(None)
    assert hub.reachable("T1") and on_loop(lambda: hub.send(lease())) is True


def test_only_devices_that_run_urban_os_are_judged(rig, store):
    store.set_floor(5)
    rec = rig.hub.registry.devices["edge-J-01"]
    for kind in ("unit", "district", "lighting"):
        assert rig.hub.floor_verdict("edge-J-01", {**rec, "kind": kind}) == (5, None)
    assert rig.hub.floor_verdict("edge-J-01", {**rec, "kind": "gateway"}) is None
    assert rig.hub.floor_verdict("no-such-device") == (5, None)  # an unknown record: refusing is the safe side


def test_a_version_shown_in_an_earlier_session_does_not_vouch_for_a_new_one(pki, store):
    rig = Rig(pki, store)
    hub = rig.hub
    hub.sessions.clear()
    key = crypto.load_private((pki / "edge" / "edge-J-01.key").read_text())
    store.set_floor(5)
    probe = []

    class Session(Recorder):
        def __init__(self, reports):
            super().__init__()
            self.reports, self.step = list(reports), 0

        async def accept(self):
            pass

        async def receive_text(self):
            self.step += 1
            if self.step == 1:
                return json.dumps({"type": "hello", "device": "edge-J-01", "version": "0.1.0", "epoch": 1,
                                   "sig": crypto.sign(key, {"device": "edge-J-01", "nonce": self.sent[0]["nonce"]})})
            probe.append((hub.floor_verdict("edge-J-01"), hub.seen["edge-J-01"]["health"].get("os")))
            if self.reports:
                return json.dumps({"type": "report", "t": 1, "queues": {}, "events": [], "health": self.reports.pop(0)})
            raise WebSocketDisconnect()

    asyncio.run(hub.serve(Session([{"os": 5}])))
    assert probe == [((5, None), None), (None, 5)]  # unproved until its first report, then fine
    assert hub.floor_verdict("edge-J-01") is None  # offline now: the version of its last session stands
    probe.clear()
    asyncio.run(hub.serve(Session([{"os": 3}])))  # the box comes back on an old system
    # at the start of the new session its old version is still known (an update offer may use it), but it vouches
    # for nothing: unproved until it reports, then below the floor
    assert probe == [((5, None), 5), ((5, 3), 3)]


def test_status_rows_and_the_platform_brief_carry_the_floor(rig, store):
    hub = rig.hub
    store.set_floor(5)
    hub.heard("edge-J-01", {"os": 3})
    hub.heard("edge-J-02", {"os": 6})
    hub.heard("district-1", {"os": 5})
    rows = {r["id"]: r for r in on_loop(hub.status)}
    assert {d: (r["below_floor"], r["floor"], r["os_proven"]) for d, r in rows.items()} == {
        "edge-J-01": (True, 5, 3), "edge-J-02": (False, 5, 6), "district-1": (False, 5, 5),
        "light-C-01": (True, 5, None), "light-C-02": (True, 5, None)}
    platform = Platform([71.78, 40.38], 1000, [])
    platform.devices(list(rows.values()), 10)
    assert (platform.dev["J-01"]["bf"], platform.dev["J-01"]["fl"], platform.dev["J-01"]["po"]) == (1, 5, 3)
    assert (platform.dev["J-02"]["bf"], platform.dev["J-02"]["po"]) == (0, 6)
    assert platform.dev["district-1"]["bf"] == 0 and platform.dev["light-C-01"]["bf"] == 1
    store.set_floor(None)
    rows = on_loop(hub.status)
    assert all(r["below_floor"] is False and r["floor"] is None for r in rows)


# ---------------------------------------------------------------- the lighting module: a clear 409

def lighting_app(rig):
    core = Core(send=rig.hub.send_device, online=rig.hub.is_online, devices=rig.hub.devices_of,
                audit=lambda event, target, note="", actor="platform": rig.audited.append((event, target, note, actor)),
                now=lambda: WALL, viewer=viewer, operator=operator, admin=admin, refusal=rig.hub.floor_refusal)
    mod = LightingModule()
    mod.attach(core)
    app = FastAPI()
    app.include_router(mod.routes(), prefix="/api/lighting")
    return mod, app


def override(*cabinets):
    return {"cabinets": list(cabinets), "level": 60, "minutes": 2, "reason": "road works"}


def test_a_lighting_override_naming_a_cabinet_below_the_floor_is_refused_whole_with_409(rig, store):
    mod, app = lighting_app(rig)
    store.set_floor(5)
    rig.hub.heard("light-C-01", {"os": 3})
    rig.hub.heard("light-C-02", {"os": 5})
    status, err = call(app, "POST", "/api/lighting/override", override("C-02", "C-01"), user="operator:olga")
    assert status == 409
    assert "шкаф C-01: ниже допустимой версии (ОС 3 < 5): команды не отправляются" in err["detail"]
    assert "C-02" not in err["detail"].split(".")[0]  # only the cabinet that is below the floor is named
    assert rig.cmds("light-C-01") == [] and rig.cmds("light-C-02") == []  # all or nothing: nothing was sent
    assert mod.overrides == {} and rig.events("lighting-override") == []
    status, state = call(app, "GET", "/api/lighting/state", user="viewer:vic")
    cab = next(c for c in state["cabinets"] if c["cabinet"] == "C-01")
    assert {"kind": "os-floor", "level": "alert", "text": floor_words(5, 3)} in cab["alerts"]
    assert not any(a["kind"] == "os-floor" for c in state["cabinets"] if c["cabinet"] == "C-02" for a in c["alerts"])
    rig.hub.heard("light-C-01", {"os": 5})  # updated: the same override now goes through
    status, _ = call(app, "POST", "/api/lighting/override", override("C-02", "C-01"), user="operator:olga")
    assert status == 200 and set(mod.overrides) == {"C-01", "C-02"}
    assert [c["lighting"]["action"] for c in rig.cmds("light-C-01")] == ["set"]
    assert len(rig.events("lighting-override")) == 2
