"""The street-lighting module at the centre (urban/modules/lighting/__init__.py), on a fake core: an operator's
override is sent signed (through Core.send), renewed every 40 s while wanted and released at its end, all audited;
a viewer cannot set one; bad input is refused; the alerts; and a cabinet's report is accepted only for its own
cabinet and only with sane values."""
import asyncio
import json
import math

import pytest
from fastapi import FastAPI, Header, HTTPException

import urban.modules.lighting as lighting_module
from urban.devices import crypto
from urban.modules.base import Core
from urban.modules.lighting import LightingModule, validate_report
from urban.modules.lighting.device import EmulatedAdapter, LightingCabinet

T0 = 1_800_000_000.0
RECORDS = [{"id": "light-C-01", "kind": "lighting", "cabinet": "C-01", "lon": 71.78, "lat": 40.39, "status": "active"},
           {"id": "light-C-02", "kind": "lighting", "cabinet": "C-02", "lon": 71.80, "lat": 40.40, "status": "active"},
           {"id": "light-C-09", "kind": "lighting", "cabinet": "C-09", "lon": 71.9, "lat": 40.5, "status": "revoked"}]


def good(cabinet="C-01", **change):
    return {"cabinet": cabinet, "mode": "schedule", "on": True, "level": 100, "lamps": 24, "failed": 0,
            "power_w": 2160.0, "door": "closed", "lease_until": None, "schedule": "astro", "dark": True, **change}


class Rig:
    def __init__(self, demo=False, extra=None):
        self.t = T0
        self.sent, self.audited, self.online_ids, self.delivered = [], [], {"light-C-01", "light-C-02"}, True
        self.records = [dict(r) for r in RECORDS]

        def send(device, body):
            self.sent.append((self.t, device, body))
            return self.delivered and device in self.online_ids

        def viewer(x_user: str | None = Header(None)):
            role, _, name = (x_user or "").partition(":")
            if role not in ("viewer", "operator", "admin"):
                raise HTTPException(401, "sign in")
            return name

        def operator(x_user: str | None = Header(None)):
            role, _, name = (x_user or "").partition(":")
            if role not in ("operator", "admin"):
                raise HTTPException(403, "operators only")
            return name

        self.core = Core(send=send, online=lambda d: d in self.online_ids, devices=lambda kind: [
            r for r in self.records if r["kind"] == kind], audit=lambda event, target, note="", actor="platform":
            self.audited.append((event, target, note, actor)), now=lambda: self.t, viewer=viewer, operator=operator,
            demo=demo, extra=extra or {})
        self.mod = LightingModule()
        self.mod.attach(self.core)
        self.app = FastAPI()
        self.app.include_router(self.mod.routes(), prefix="/api/lighting")

    def report(self, device="light-C-01", cabinet="C-01", **change):
        rec = next(r for r in self.records if r["id"] == device)
        self.mod.on_report(rec, {"type": "report", "t": int(self.t), "health": {}, "lighting": good(cabinet, **change)})

    def at(self, seconds):
        self.t = T0 + seconds
        self.mod.tick(self.t)

    def sends(self, kind=None):
        return [(t - T0, d, b["lighting"]) for t, d, b in self.sent if kind in (None, b["lighting"]["action"])]

    def call(self, method, path, body=None, user="operator:ann"):
        return call(self.app, method, path, body, user)

    def events(self, name):
        return [a for a in self.audited if a[0] == name]


def call(app, method, path, body=None, user="operator:ann"):
    """One request straight into the ASGI app (the test environment has no HTTP client library)."""
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
        return out

    res = asyncio.run(run())
    return res["status"], (json.loads(res["body"]) if res["body"] else None)


def override_body(**change):
    return {"cabinets": ["C-01"], "level": 60, "minutes": 2, "reason": "road works", **change}


def cabinet_of(state, ident):
    return next(c for c in state["cabinets"] if c["cabinet"] == ident)


# ---------------------------------------------------------------- the override: sent, renewed, released, audited

def test_an_override_is_sent_at_once_renewed_every_forty_seconds_released_at_the_end_and_audited():
    rig = Rig()
    status, state = rig.call("POST", "/api/lighting/override", override_body(cabinets=["C-01", "C-02"], minutes=3))
    assert status == 200
    want = {"cabinet": "C-01", "action": "set", "level": 60, "ttl": 120}
    assert rig.sends() == [(0, "light-C-01", want), (0, "light-C-02", {**want, "cabinet": "C-02"})]
    for cab in ("C-01", "C-02"):
        assert cabinet_of(state, cab)["override"] == {"level": 60, "until": T0 + 180, "actor": "ann", "reason": "road works"}
    rig.sent.clear()
    rig.at(39)
    assert rig.sends() == []
    rig.at(40)
    assert [(t, d) for t, d, _ in rig.sends()] == [(40, "light-C-01"), (40, "light-C-02")]
    assert all(b["action"] == "set" and b["level"] == 60 and b["ttl"] == 120 for _, _, b in rig.sends())
    rig.at(79)
    rig.at(80)
    rig.at(120)
    rig.at(160)
    assert [t for t, d, _ in rig.sends("set") if d == "light-C-01"] == [40, 80, 120, 160]
    assert rig.sends("release") == [] and rig.mod.summary()["overrides"] == 2
    rig.at(180)  # the three minutes are over: release, and no renewal in the same breath
    assert rig.sends("release") == [(180, "light-C-01", {"cabinet": "C-01", "action": "release"}),
                                    (180, "light-C-02", {"cabinet": "C-02", "action": "release"})]
    assert [t for t, d, _ in rig.sends("set") if d == "light-C-01"] == [40, 80, 120, 160]
    assert rig.mod.summary()["overrides"] == 0
    rig.at(300)
    assert len(rig.sends("release")) == 2
    assert [e[1] for e in rig.events("lighting-expired")] == ["C-01", "C-02"]
    assert all(e[3] == "platform" for e in rig.events("lighting-expired"))
    assert cabinet_of(rig.mod.state(), "C-01")["override"] is None


def test_the_override_is_audited_per_cabinet_with_level_minutes_reason_and_who():
    rig = Rig()
    rig.call("POST", "/api/lighting/override", override_body(cabinets=["C-01", "C-02"], level=35, minutes=90,
                                                             reason="Парад на площади"), user="operator:ann")
    events = rig.events("lighting-override")
    assert [e[1] for e in events] == ["C-01", "C-02"] and all(e[3] == "ann" for e in events)
    for e in events:
        assert "35" in e[2] and "90" in e[2] and "Парад на площади" in e[2]


def test_the_ends_of_the_ranges_are_accepted_and_a_second_override_replaces_the_first():
    rig = Rig()
    for level, minutes in ((0, 1), (100, 720)):
        status, state = rig.call("POST", "/api/lighting/override", override_body(level=level, minutes=minutes))
        assert status == 200 and cabinet_of(state, "C-01")["override"]["until"] == T0 + minutes * 60
    assert rig.sends("set")[-1][2]["level"] == 100
    rig.at(40)
    assert rig.sends("set")[-1][2]["level"] == 100 and len(rig.mod.overrides) == 1  # the newer wish is the one renewed
    rig.at(720 * 60)
    assert rig.sends("release")[-1][0] == 720 * 60


def test_a_cabinet_that_cannot_be_reached_is_tried_again_soon_not_forty_seconds_later():
    rig = Rig()
    rig.online_ids.discard("light-C-01")  # the link is down
    status, state = rig.call("POST", "/api/lighting/override", override_body(minutes=5))
    assert status == 200 and cabinet_of(state, "C-01")["override"] is not None  # still wanted
    rig.sent.clear()
    rig.at(4)
    assert rig.sends() == []
    rig.at(5)
    assert [t for t, *_ in rig.sends()] == [5]
    rig.online_ids.add("light-C-01")
    rig.at(10)
    assert [t for t, *_ in rig.sends()] == [5, 10]
    rig.at(49)
    assert [t for t, *_ in rig.sends()] == [5, 10]  # delivered at 10: the next renewal is 40 s after it
    rig.at(50)
    assert [t for t, *_ in rig.sends()] == [5, 10, 50]


def test_release_sends_release_stops_the_renewals_and_is_audited():
    rig = Rig()
    rig.call("POST", "/api/lighting/override", override_body(cabinets=["C-01", "C-02"], minutes=30))
    rig.sent.clear()
    status, state = rig.call("POST", "/api/lighting/release", {"cabinets": ["C-01"]}, user="admin:root")
    assert status == 200 and cabinet_of(state, "C-01")["override"] is None and cabinet_of(state, "C-02")["override"]
    assert rig.sends() == [(0, "light-C-01", {"cabinet": "C-01", "action": "release"})]
    assert rig.events("lighting-release") == [("lighting-release", "C-01", "released", "root")]
    rig.sent.clear()
    rig.at(40)
    assert [d for _, d, _ in rig.sends()] == ["light-C-02"]  # C-01 is no longer renewed
    rig.at(30 * 60)
    assert [e[1] for e in rig.events("lighting-expired")] == ["C-02"]  # a released override does not "expire"


# ---------------------------------------------------------------- who may do it, and what is refused

def test_a_viewer_can_look_but_not_set_or_release_and_nobody_signed_out_can_do_either():
    rig = Rig()
    assert rig.call("GET", "/api/lighting/state", user="viewer:vic")[0] == 200
    assert rig.call("POST", "/api/lighting/override", override_body(), user="viewer:vic")[0] == 403
    assert rig.call("POST", "/api/lighting/release", {"cabinets": ["C-01"]}, user="viewer:vic")[0] == 403
    assert rig.call("GET", "/api/lighting/state", user=None)[0] == 401
    assert rig.call("POST", "/api/lighting/override", override_body(), user=None)[0] in (401, 403)
    assert rig.sent == [] and rig.audited == [] and rig.mod.overrides == {}
    assert rig.call("POST", "/api/lighting/override", override_body(), user="operator:ann")[0] == 200
    assert rig.call("POST", "/api/lighting/release", {"cabinets": ["C-01"]}, user="admin:root")[0] == 200


BAD = [
    ("no cabinets", {"cabinets": []}), ("cabinets not a list", {"cabinets": "C-01"}), ("a cabinet that is no text", {"cabinets": [1]}),
    ("no cabinets field", {"cabinets": None}),
    ("level 101", {"level": 101}), ("level -1", {"level": -1}), ("level as text", {"level": "50"}),
    ("level fractional", {"level": 50.5}), ("level as bool", {"level": True}), ("level null", {"level": None}),
    ("minutes 0", {"minutes": 0}), ("minutes 721", {"minutes": 721}), ("minutes fractional", {"minutes": 1.5}),
    ("minutes as text", {"minutes": "5"}), ("minutes as bool", {"minutes": True}),
    ("empty reason", {"reason": ""}), ("blank reason", {"reason": "   "}), ("reason of control characters", {"reason": "\x00\x07"}),
    ("reason 201 characters", {"reason": "x" * 201}), ("reason as number", {"reason": 5}),
    ("unknown cabinet", {"cabinets": ["C-77"]}), ("one known, one unknown", {"cabinets": ["C-01", "C-77"]}),
    ("a revoked cabinet", {"cabinets": ["C-09"]}),
]


@pytest.mark.parametrize("what,change", BAD, ids=[b[0] for b in BAD])
def test_a_bad_override_is_refused_and_nothing_is_sent_or_audited(what, change):
    rig = Rig()
    status, _ = rig.call("POST", "/api/lighting/override", override_body(**change))
    assert status in (400, 422), status
    assert rig.sent == [] and rig.audited == [] and rig.mod.overrides == {}


@pytest.mark.parametrize("missing", ["cabinets", "level", "minutes", "reason"])
def test_every_field_of_an_override_is_required(missing):
    rig = Rig()
    body = override_body()
    del body[missing]
    assert rig.call("POST", "/api/lighting/override", body)[0] == 422
    assert rig.sent == [] and rig.audited == []
    assert rig.call("POST", "/api/lighting/override")[0] == 422  # no body at all


def test_a_bad_release_is_refused():
    rig = Rig()
    for body in ({"cabinets": []}, {"cabinets": ["C-77"]}, {}, {"cabinets": "C-01"}):
        assert rig.call("POST", "/api/lighting/release", body)[0] in (400, 422)
    assert rig.sent == [] and rig.audited == []


def test_the_same_cabinet_twice_in_one_request_is_commanded_once():
    rig = Rig()
    assert rig.call("POST", "/api/lighting/override", override_body(cabinets=["C-01", "C-01"]))[0] == 200
    assert len(rig.sends()) == 1 and len(rig.events("lighting-override")) == 1


# ---------------------------------------------------------------- a cabinet's report: untrusted

def _nan(): return float("nan")


REPORTS = [
    ("another cabinet's report", lambda: good("C-02")),
    ("no cabinet", lambda: {k: v for k, v in good().items() if k != "cabinet"}),
    ("level 101", lambda: good(level=101)), ("level negative", lambda: good(level=-5)),
    ("level fractional", lambda: good(level=50.5)), ("level text", lambda: good(level="100")), ("level bool", lambda: good(level=True)),
    ("level NaN", lambda: good(level=_nan())),
    ("power NaN", lambda: good(power_w=_nan())), ("power inf", lambda: good(power_w=math.inf)),
    ("power -inf", lambda: good(power_w=-math.inf)), ("power negative", lambda: good(power_w=-1.0)),
    ("power huge", lambda: good(power_w=1e12)), ("power text", lambda: good(power_w="2000")), ("power bool", lambda: good(power_w=True)),
    ("lamps negative", lambda: good(lamps=-1)), ("lamps huge", lambda: good(lamps=10 ** 9)),
    ("failed negative", lambda: good(failed=-1)), ("failed more than lamps", lambda: good(failed=25)),
    ("failed text", lambda: good(failed="1")),
    ("lease NaN", lambda: good(lease_until=_nan())), ("lease inf", lambda: good(lease_until=math.inf)),
    ("lease negative", lambda: good(lease_until=-1)), ("lease text", lambda: good(lease_until="soon")),
    ("mode unknown", lambda: good(mode="turbo")), ("mode missing", lambda: {k: v for k, v in good().items() if k != "mode"}),
    ("door unknown value", lambda: good(door="ajar")), ("door number", lambda: good(door=1)),
    ("schedule unknown value", lambda: good(schedule="moon")), ("dark as text", lambda: good(dark="yes")),
    ("on as number", lambda: good(on=1)),
    ("not an object", lambda: "lighting"), ("a list", lambda: [good()]), ("nothing", lambda: None),
]


@pytest.mark.parametrize("what,make", REPORTS, ids=[r[0] for r in REPORTS])
def test_a_report_that_is_not_right_is_dropped_without_raising_and_leaves_no_trace(what, make):
    rig = Rig()
    rec = rig.records[0]
    rig.mod.on_report(rec, {"type": "report", "lighting": make()})
    assert rig.mod.reports == {} and rig.audited == []
    st = cabinet_of(rig.mod.state(), "C-01")
    assert st["level"] is None and st["age"] is None and st["on"] is None
    assert validate_report(make(), "C-01") is None


def test_reports_that_are_not_even_reports_do_not_raise():
    rig = Rig()
    for rec, report in ((None, {}), ({}, {}), ({"id": "x"}, {"lighting": good()}), ({"id": 5, "cabinet": "C-01"}, {"lighting": good()}),
                        ({"id": "light-C-01", "cabinet": "C-01"}, None), ({"id": "light-C-01", "cabinet": "C-01"}, []),
                        ({"id": "light-C-01", "cabinet": "C-01"}, {"lighting": 5}), (rig.records[0], {"health": {}})):
        rig.mod.on_report(rec, report)
    assert rig.mod.reports == {}
    unattached = LightingModule()  # a report before attach() must not raise either
    unattached.on_report(rig.records[0], {"lighting": good()})
    unattached.tick(T0)
    assert unattached.summary()["devices"] == 0


def test_a_good_report_is_taken_and_its_text_is_cleaned():
    rig = Rig()
    rig.report(mode="override", level=60, lease_until=int(T0) + 120, power_w=1296.04, door="open", failed=1)
    st = cabinet_of(rig.mod.state(), "C-01")
    assert (st["mode"], st["level"], st["lease_until"], st["power_w"], st["door"], st["failed"]) == (
        "override", 60, int(T0) + 120, 1296.0, "open", 1)
    assert st["id"] == "light-C-01" and st["lon"] == 71.78 and st["lat"] == 40.39 and st["age"] == 0 and st["online"]
    odd = validate_report(good(refused="<b>bad</b>\x07 ‮signed‬ подпись " + "x" * 300), "C-01")["refused"]
    assert odd.startswith("<b>bad</b>") and "\x07" not in odd and "‮" not in odd and "подпись" in odd and len(odd) <= 120
    assert validate_report(good(refused=5), "C-01")["refused"] is None and validate_report(good(refused=""), "C-01")["refused"] is None


def test_a_cabinet_is_trusted_only_for_its_own_cabinet():
    rig = Rig()
    rig.mod.on_report(rig.records[0], {"lighting": good("C-02", level=1)})  # light-C-01 claims to be C-02
    rig.report("light-C-02", "C-02", level=50)
    state = rig.mod.state()
    assert cabinet_of(state, "C-02")["level"] == 50 and cabinet_of(state, "C-01")["level"] is None


# ---------------------------------------------------------------- the alerts

def kinds(rig, cabinet="C-01"):
    return sorted(a["kind"] for a in cabinet_of(rig.mod.state(), cabinet)["alerts"])


def test_failed_lamps_an_open_door_a_silent_cabinet_and_a_refused_command_each_raise_an_alert():
    rig = Rig()
    rig.report()
    assert kinds(rig) == [] and rig.mod.summary() == {"devices": 2, "online": 1, "alerts": 1, "overrides": 0}  # C-02 never reported
    rig.report(failed=2)
    assert kinds(rig) == ["lamps"]
    assert "2" in cabinet_of(rig.mod.state(), "C-01")["alerts"][0]["text"]
    rig.report(failed=0, door="open")
    assert kinds(rig) == ["door"] and cabinet_of(rig.mod.state(), "C-01")["alerts"][0]["level"] == "alert"
    rig.report(failed=1, door="open")
    assert kinds(rig) == ["door", "lamps"]
    rig.report()
    assert kinds(rig) == []
    rig.t += 31  # no report for 30 s
    assert kinds(rig) == ["offline"] and not cabinet_of(rig.mod.state(), "C-01")["online"]
    rig.report()
    assert kinds(rig) == []
    rig.online_ids.discard("light-C-01")  # the session is gone, though the last report is fresh
    assert kinds(rig) == ["offline"]


def test_the_offline_edge_is_30_seconds_exactly():
    rig = Rig()
    rig.report()
    rig.t += 30
    assert cabinet_of(rig.mod.state(), "C-01")["online"]
    rig.t += 0.5
    assert not cabinet_of(rig.mod.state(), "C-01")["online"]


def test_a_refused_command_is_audited_once_per_refusal_and_shows_as_an_alert_for_a_while():
    rig = Rig()
    rig.report()
    rig.report(refused="bad signature")
    assert rig.events("lighting-refused") == [("lighting-refused", "C-01", "bad signature", "light-C-01")]
    assert kinds(rig) == ["refused"] and cabinet_of(rig.mod.state(), "C-01")["refused"] == "bad signature"
    rig.t += 5
    rig.report()  # the next report carries no refusal: the cabinet said it once
    assert len(rig.events("lighting-refused")) == 1 and kinds(rig) == ["refused"]
    rig.t += 5
    rig.report(refused="not my cabinet")
    assert [e[2] for e in rig.events("lighting-refused")] == ["bad signature", "not my cabinet"]
    assert cabinet_of(rig.mod.state(), "C-01")["refused"] == "not my cabinet"
    rig.t += 400
    rig.report()
    assert kinds(rig) == [] and cabinet_of(rig.mod.state(), "C-01")["refused"] is None


def test_summary_counts_devices_online_alerts_and_overrides():
    rig = Rig()
    assert rig.mod.summary() == {"devices": 2, "online": 0, "alerts": 2, "overrides": 0}  # the revoked one is not counted
    rig.report()
    rig.report("light-C-02", "C-02", failed=1, door="open")
    rig.call("POST", "/api/lighting/override", override_body(cabinets=["C-01", "C-02"]))
    assert rig.mod.summary() == {"devices": 2, "online": 2, "alerts": 2, "overrides": 2}
    state = rig.call("GET", "/api/lighting/state", user="viewer:vic")[1]
    assert state["alerts"] == 2 and [c["cabinet"] for c in state["cabinets"]] == ["C-01", "C-02"]
    assert set(cabinet_of(state, "C-01")) == {"id", "cabinet", "lon", "lat", "online", "mode", "on", "level", "lamps", "failed",
                                              "power_w", "door", "lease_until", "schedule", "dark", "age", "refused",
                                              "alerts", "override"}


def test_the_interface_and_the_module_identify_themselves_as_the_core_expects():
    mod = LightingModule()
    assert (mod.id, mod.device_kind, mod.title) == ("lighting", "lighting", "Уличное освещение")
    assert mod.routes() is not None
    assert asyncio.run(mod.startup()) is None  # not attached, not demo: nothing to start


# ---------------------------------------------------------------- the demo cabinets

def test_in_the_demo_the_module_starts_emulated_cabinets_against_the_platforms_own_listener(monkeypatch, tmp_path):
    calls = []

    async def fake(platform_addr, server_name, ca_path, pki_dir, cabinets):
        calls.append((platform_addr, server_name, str(ca_path), str(pki_dir), [c["cabinet"] for c in cabinets]))
        await asyncio.sleep(3600)

    monkeypatch.setattr(lighting_module, "run_demo_cabinets", fake)
    extra = {"platform_addr": "127.0.0.1:9443", "server_name": "centre", "ca_path": tmp_path / "ca.pem", "pki_dir": tmp_path}

    async def go(rig):
        await rig.mod.startup()
        await rig.mod.startup()  # once
        await asyncio.sleep(0.05)
        rig.mod.stop()
        await asyncio.sleep(0.05)

    asyncio.run(go(Rig(demo=True, extra=extra)))
    assert calls == [("127.0.0.1:9443", "centre", str(tmp_path / "ca.pem"), str(tmp_path), ["C-01", "C-02"])]
    asyncio.run(go(Rig(demo=False, extra=extra)))
    assert len(calls) == 1  # the real city never gets emulated cabinets


# ---------------------------------------------------------------- the module and a real cabinet program, together

def test_a_real_cabinet_obeys_the_module_through_signed_envelopes_and_goes_back_to_its_schedule():
    gateway, key = crypto.new_key(), crypto.new_key()
    rig = Rig()
    seq = [0]
    cab = LightingCabinet("light-C-01", "C-01", key, crypto.load_public(crypto.public_hex(gateway)), EmulatedAdapter(),
                          71.78, 40.39, wall=lambda: rig.t)
    rig.t = 1_781_000_000.0 - (1_781_000_000 % 86400) + 8 * 3600  # an afternoon: the schedule says off
    cab.tick()

    def send(device, body):  # what DeviceHub does: number, time, sign
        seq[0] += 1
        env = {"device": device, "seq": seq[0], "t": int(rig.t), "body": body}
        if device != "light-C-01":
            return False
        return cab.on_command({**env, "sig": crypto.sign(gateway, env)})

    rig.core.send = send
    rig.mod.core = rig.core
    rig.mod.attach(rig.core)

    def pump():
        cab.tick()
        rec = rig.records[0]
        rig.mod.on_report(rec, cab.report())
        rep = cab.report()
        cab.mark_sent(rep)

    start = rig.t
    pump()
    assert cabinet_of(rig.mod.state(), "C-01")["mode"] == "schedule" and cabinet_of(rig.mod.state(), "C-01")["level"] == 0
    assert rig.call("POST", "/api/lighting/override", override_body(level=55, minutes=3))[0] == 200
    pump()
    st = cabinet_of(rig.mod.state(), "C-01")
    assert st["mode"] == "override" and st["level"] == 55 and st["lease_until"] == int(start) + 120 and st["on"]
    for step in range(1, 180):  # three minutes, a tick each second, the cabinet's own clock beside it
        rig.t = start + step
        rig.mod.tick(rig.t)
        pump()
        if step < 179:
            assert cabinet_of(rig.mod.state(), "C-01")["mode"] == "override", step  # renewals keep it held past 120 s
    rig.t = start + 180
    rig.mod.tick(rig.t)
    pump()
    st = cabinet_of(rig.mod.state(), "C-01")
    assert st["mode"] == "schedule" and st["level"] == 0 and st["override"] is None
    assert [e[1] for e in rig.events("lighting-expired")] == ["C-01"]
    assert cab.last_refusal is None and kinds(rig) == []
    # the centre then forgets the cabinet: the last lease it gave still ends by itself
    rig.call("POST", "/api/lighting/override", override_body(level=80, minutes=60))
    assert cab.report()["lighting"]["mode"] == "override"
    rig.t += 121
    cab.tick()
    assert cab.report()["lighting"]["mode"] == "schedule"
