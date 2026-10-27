"""The water module at the centre (urban/modules/water/__init__.py), on a fake core: it is read only (one route, a
GET, and nothing ever sent); a station's report is accepted only for its own station and only whole and sane; the
alerts, with the pressure limits from the registry record and the leak rule; the start and the end of each alert are
audited once; the state and the summary; and the demo stations."""
import asyncio
import json
import math

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.routing import APIRoute

import urban.modules.water as water_module
from urban.modules.base import Core
from urban.modules.water import WaterModule, validate_report

T0 = 1_800_000_000.0
RECORDS = [{"id": "pump-S-01", "kind": "water", "site": "S-01", "lon": 71.78, "lat": 40.39, "p_min": 2.0, "p_max": 6.0,
            "status": "active"},
           {"id": "pump-S-02", "kind": "water", "site": "S-02", "lon": None, "lat": None, "p_min": None, "p_max": None,
            "status": "active"},
           {"id": "pump-S-09", "kind": "water", "site": "S-09", "lon": 71.9, "lat": 40.5, "status": "revoked"}]


def good(site="S-01", **change):
    return {"site": site, "pressure_in": 0.8, "pressure_out": 4.2, "flow": 118.5, "power_kw": 37.2,
            "energy_kwh": 1203344, "level": 3.1, "pumps": [{"run": True, "fault": False}, {"run": False, "fault": False}],
            "door": "closed", "kwh_per_m3": 0.31, "night_min_flow": 41.0, "night_baseline": 38.5,
            "source_error": None, "refused": None, **change}


class Rig:
    def __init__(self, demo=False, extra=None, refusal=None):
        self.t = T0
        self.sent, self.audited, self.online_ids = [], [], {"pump-S-01", "pump-S-02"}
        self.records = [dict(r) for r in RECORDS]

        def send(device, body):
            self.sent.append((device, body))
            return True

        def viewer(x_user: str | None = Header(None)):
            role, _, name = (x_user or "").partition(":")
            if role not in ("viewer", "operator", "admin"):
                raise HTTPException(401, "sign in")
            return name

        self.core = Core(send=send, online=lambda d: d in self.online_ids, devices=lambda kind: [
            r for r in self.records if r["kind"] == kind], audit=lambda event, target, note="", actor="platform":
            self.audited.append((event, target, note, actor)), now=lambda: self.t, viewer=viewer,
            operator=viewer, admin=viewer, demo=demo, extra=extra or {},
            refusal=refusal or (lambda device: None))
        self.mod = WaterModule()
        self.mod.attach(self.core)
        self.app = FastAPI()
        self.app.include_router(self.mod.routes(), prefix="/api/water")

    def report(self, device="pump-S-01", site="S-01", **change):
        rec = next(r for r in self.records if r["id"] == device)
        self.mod.on_report(rec, {"type": "report", "t": int(self.t), "health": {}, "water": good(site, **change)})

    def call(self, method, path, body=None, user="viewer:vic"):
        return call(self.app, method, path, body, user)

    def events(self, *names):
        return [a for a in self.audited if a[0] in names]


def call(app, method, path, body=None, user="viewer:vic"):
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


def site_of(state, ident):
    return next(s for s in state["sites"] if s["site"] == ident)


def kinds(rig, site="S-01"):
    return sorted(a["kind"] for a in site_of(rig.mod.state(), site)["alerts"])


# ---------------------------------------------------------------- read only: one GET, nothing sent

def test_the_module_has_one_route_a_get_of_the_state_and_no_post_of_any_kind():
    rig = Rig()
    routes = [r for r in rig.mod.routes().routes if isinstance(r, APIRoute)]
    assert [(r.path, sorted(r.methods)) for r in routes] == [("/state", ["GET"])]
    paths = rig.app.openapi()["paths"]
    assert list(paths) == ["/api/water/state"] and list(paths["/api/water/state"]) == ["get"]
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in ("/api/water/state", "/api/water/override", "/api/water/command", "/api/water/set"):
            assert rig.call(method, path, {"sites": ["S-01"]}, user="admin:root")[0] in (404, 405)


def test_nothing_is_ever_sent_to_a_station_whatever_happens():
    rig = Rig()
    for step in range(30):
        rig.report(pressure_out=1.0 if step % 2 else 9.0, pumps=[{"run": False, "fault": True}], door="open",
                   source_error="timeout" if step % 3 else None, refused="станция только читает: команд нет")
        rig.mod.tick(rig.t)
        rig.t += 1
    rig.mod.state()
    rig.mod.summary()
    assert rig.sent == []


def test_a_viewer_can_look_and_nobody_signed_out_can():
    rig = Rig()
    rig.report()
    status, state = rig.call("GET", "/api/water/state", user="viewer:vic")
    assert status == 200 and [s["site"] for s in state["sites"]] == ["S-01", "S-02"]
    assert rig.call("GET", "/api/water/state", user="")[0] == 401
    unattached = WaterModule()  # no core: no role can be checked, so nobody gets in
    app = FastAPI()
    app.include_router(unattached.routes(), prefix="/api/water")
    assert call(app, "GET", "/api/water/state", user="admin:root")[0] == 403


def test_the_module_identifies_itself_as_the_core_expects():
    mod = WaterModule()
    assert (mod.id, mod.device_kind, mod.title) == ("water", "water", "Водоканал")
    assert asyncio.run(mod.startup()) is None  # not attached, not demo: nothing to start
    assert mod.tick(T0) is None


# ---------------------------------------------------------------- a station's report: untrusted

def _nan():
    return float("nan")


REPORTS = [
    ("another station's report", lambda: good("S-02")), ("no site", lambda: {k: v for k, v in good().items() if k != "site"}),
    ("site as number", lambda: good(site=1)),
    ("pressure NaN", lambda: good(pressure_out=_nan())), ("pressure inf", lambda: good(pressure_out=math.inf)),
    ("pressure below -1", lambda: good(pressure_in=-1.5)), ("pressure above 100", lambda: good(pressure_out=100.5)),
    ("pressure text", lambda: good(pressure_out="4.2")), ("pressure bool", lambda: good(pressure_out=True)),
    ("flow negative", lambda: good(flow=-0.1)), ("flow huge", lambda: good(flow=100_001)), ("flow -inf", lambda: good(flow=-math.inf)),
    ("power negative", lambda: good(power_kw=-1)), ("power huge", lambda: good(power_kw=1e6)),
    ("energy negative", lambda: good(energy_kwh=-1)), ("energy beyond 1e13", lambda: good(energy_kwh=1e13 + 1)),
    ("level below -100", lambda: good(level=-101)), ("level above 1000", lambda: good(level=1000.1)),
    ("kwh/m3 above 100", lambda: good(kwh_per_m3=101)), ("kwh/m3 negative", lambda: good(kwh_per_m3=-0.1)),
    ("night min negative", lambda: good(night_min_flow=-1)), ("baseline NaN", lambda: good(night_baseline=_nan())),
    ("pumps not a list", lambda: good(pumps={"run": True})), ("17 pumps", lambda: good(pumps=[{"run": True}] * 17)),
    ("pump not an object", lambda: good(pumps=[True])), ("pump run as text", lambda: good(pumps=[{"run": "yes"}])),
    ("pump fault as number", lambda: good(pumps=[{"run": True, "fault": 1}])),
    ("door unknown value", lambda: good(door="ajar")), ("door number", lambda: good(door=1)),
    ("source error a number", lambda: good(source_error=5)), ("refused a list", lambda: good(refused=["x"])),
    ("not an object", lambda: "water"), ("a list", lambda: [good()]), ("nothing", lambda: None),
]


@pytest.mark.parametrize("what,make", REPORTS, ids=[r[0] for r in REPORTS])
def test_a_report_with_anything_wrong_is_dropped_whole_without_raising(what, make):
    rig = Rig()
    rig.mod.on_report(rig.records[0], {"type": "report", "water": make()})
    assert rig.mod.reports == {} and rig.audited == []
    st = site_of(rig.mod.state(), "S-01")
    assert st["flow"] is None and st["age"] is None and st["pressure_out"] is None and not st["online"]
    assert validate_report(make(), "S-01") is None


def test_reports_that_are_not_even_reports_do_not_raise():
    rig = Rig()
    for rec, report in ((None, {}), ({}, {}), ({"id": "x"}, {"water": good()}), ({"id": 5, "site": "S-01"}, {"water": good()}),
                        ({"id": "pump-S-01", "site": 7}, {"water": good()}), ({"id": "pump-S-01", "site": "S-01"}, None),
                        ({"id": "pump-S-01", "site": "S-01"}, []), ({"id": "pump-S-01", "site": "S-01"}, {"water": 5}),
                        (rig.records[0], {"health": {}}), (rig.records[0], {"lighting": {"cabinet": "S-01"}})):
        rig.mod.on_report(rec, report)
    assert rig.mod.reports == {}
    unattached = WaterModule()  # a report before attach() must not raise either
    unattached.on_report(rig.records[0], {"water": good()})
    assert unattached.summary() == {"devices": 0, "online": 0, "alerts": 0}


def test_every_field_but_the_site_may_be_null_or_absent_and_the_ends_of_the_ranges_pass():
    assert validate_report({"site": "S-01"}, "S-01") == {
        "pressure_in": None, "pressure_out": None, "flow": None, "power_kw": None, "energy_kwh": None, "level": None,
        "kwh_per_m3": None, "night_min_flow": None, "night_baseline": None, "pumps": [], "door": "unknown",
        "source_error": None, "refused": None}
    nulls = {k: None for k in good() if k != "site"}
    assert validate_report(good(**nulls), "S-01")["pumps"] == []
    edge = good(pressure_in=-1, pressure_out=100, flow=0, power_kw=100_000, energy_kwh=10 ** 13, level=-100,
                kwh_per_m3=100, night_min_flow=100_000, night_baseline=0, pumps=[{"run": None, "fault": None}] * 16)
    out = validate_report(edge, "S-01")
    assert out is not None and out["energy_kwh"] == 1e13 and len(out["pumps"]) == 16
    assert validate_report(good(pumps=[{"run": True, "fault": False, "rpm": 3000}]), "S-01")["pumps"] == [
        {"run": True, "fault": False}]  # only what the module knows is kept


def test_a_good_report_is_taken_and_its_texts_are_cleaned():
    rig = Rig()
    rig.report(flow=120.04, source_error=None)
    st = site_of(rig.mod.state(), "S-01")
    assert (st["pressure_out"], st["flow"], st["power_kw"], st["energy_kwh"], st["level"]) == (4.2, 120.04, 37.2, 1203344.0, 3.1)
    assert st["online"] and st["age"] == 0 and st["lon"] == 71.78 and st["p_min"] == 2.0 and st["p_max"] == 6.0
    assert st["pumps"] == [{"run": True, "fault": False}, {"run": False, "fault": False}] and st["door"] == "closed"
    odd = validate_report(good(source_error="<b>x</b>\x07 ‮signed‬ ошибка " + "y" * 300), "S-01")["source_error"]
    assert odd.startswith("<b>x</b>") and "\x07" not in odd and "‮" not in odd and "ошибка" in odd and len(odd) <= 120
    assert validate_report(good(source_error="  "), "S-01")["source_error"] is None


def test_a_station_is_trusted_only_for_its_own_site():
    rig = Rig()
    rig.mod.on_report(rig.records[0], {"water": good("S-02", flow=1.0)})  # pump-S-01 claims to be S-02
    rig.report("pump-S-02", "S-02", flow=50.0)
    state = rig.mod.state()
    assert site_of(state, "S-02")["flow"] == 50.0 and site_of(state, "S-01")["flow"] is None


# ---------------------------------------------------------------- the alerts

def test_each_alert_with_its_level_and_words():
    rig = Rig()
    rig.report()
    assert kinds(rig) == []
    rig.report(source_error="modbus tcp 10.0.0.5:502: timed out")
    alert = site_of(rig.mod.state(), "S-01")["alerts"][0]
    assert alert == {"kind": "source", "level": "alert", "text": "нет связи с автоматикой станции: modbus tcp 10.0.0.5:502: timed out"}
    rig.report(pressure_out=1.5)
    assert site_of(rig.mod.state(), "S-01")["alerts"] == [
        {"kind": "pressure-low", "level": "alert", "text": "давление ниже нормы: 1.5 бар (норма от 2)"}]
    rig.report(pressure_out=6.25)
    assert site_of(rig.mod.state(), "S-01")["alerts"] == [
        {"kind": "pressure-high", "level": "alert", "text": "давление выше нормы: 6.25 бар (норма до 6)"}]
    rig.report(pressure_out=2.0)                                # on the limit: no alert
    rig.report(pressure_out=6.0)
    assert kinds(rig) == []
    rig.report(pumps=[{"run": False, "fault": True}, {"run": True, "fault": False}, {"run": False, "fault": True}])
    assert [(a["kind"], a["text"]) for a in site_of(rig.mod.state(), "S-01")["alerts"]] == [
        ("pump-fault", "авария насоса 1"), ("pump-fault", "авария насоса 3")]
    rig.report(door="open")
    assert site_of(rig.mod.state(), "S-01")["alerts"] == [{"kind": "door", "level": "alert", "text": "шкаф открыт"}]
    rig.report(door="unknown")
    assert kinds(rig) == []


def test_the_pressure_limits_are_the_centres_not_the_stations():
    rig = Rig()
    rig.report("pump-S-02", "S-02", pressure_out=0.1)          # S-02 has no limits in the registry: no alert
    assert kinds(rig, "S-02") == []
    rig.mod.on_report(rig.records[0], {"water": {**good(pressure_out=1.0), "p_min": 0.5}})  # a station's own limit
    assert kinds(rig) == ["pressure-low"]                      # ...is ignored
    rig.records[0]["p_min"] = 0.5                              # the administrator changes it
    assert kinds(rig) == []
    rig.records[0]["p_min"] = float("nan")                     # a damaged record never raises
    rig.records[0]["p_max"] = "6"
    assert kinds(rig) == [] and site_of(rig.mod.state(), "S-01")["p_min"] is None


@pytest.mark.parametrize("night,usual,leak", [(46.3, 38.5, True), (46.2, 38.5, False), (40.0, 38.5, False),
                                              (5.0, 0.0, False), (None, 38.5, False), (46.3, None, False)])
def test_a_leak_is_last_nights_minimum_above_one_point_two_times_the_usual(night, usual, leak):
    rig = Rig()
    rig.report(night_min_flow=night, night_baseline=usual)
    alerts = site_of(rig.mod.state(), "S-01")["alerts"]
    assert [a["kind"] for a in alerts] == (["leak"] if leak else [])
    if leak:
        assert alerts[0] == {"kind": "leak", "level": "warn",
                             "text": "возможна утечка: ночной минимум 46.3 м³/ч против обычных 38.5"}


def test_a_silent_station_is_offline_after_30_seconds_and_without_a_session_at_once():
    rig = Rig()
    rig.report()
    rig.t += 30
    assert site_of(rig.mod.state(), "S-01")["online"] and kinds(rig) == []
    rig.t += 0.5
    assert kinds(rig) == ["offline"] and not site_of(rig.mod.state(), "S-01")["online"]
    rig.report()
    assert kinds(rig) == []
    rig.online_ids.discard("pump-S-01")
    assert kinds(rig) == ["offline"]
    assert kinds(rig, "S-02") == ["offline"]                    # never reported


def test_a_refused_command_shows_for_five_minutes_and_is_audited_once_per_refusal():
    rig = Rig()
    rig.report(refused="станция только читает: команд нет")
    assert rig.events("water-refused") == [("water-refused", "S-01", "станция только читает: команд нет", "pump-S-01")]
    assert site_of(rig.mod.state(), "S-01")["alerts"] == [
        {"kind": "refused", "level": "warn", "text": "станция отклонила команду: станция только читает: команд нет"}]
    rig.t += 5
    rig.report()                                                # said once by the station
    assert len(rig.events("water-refused")) == 1 and kinds(rig) == ["refused"]
    rig.t += 300
    rig.report()
    assert kinds(rig) == [] and site_of(rig.mod.state(), "S-01")["refused"] is None


def test_a_station_below_the_centres_version_floor_shows_it():
    rig = Rig(refusal=lambda device: "ОС 3 ниже допустимой 5: команды не отправляются" if device == "pump-S-01" else None)
    rig.report()
    alerts = site_of(rig.mod.state(), "S-01")["alerts"]
    assert alerts == [{"kind": "os-floor", "level": "alert", "text": "ОС 3 ниже допустимой 5: команды не отправляются"}]
    rig.core.refusal = lambda device: (_ for _ in ()).throw(RuntimeError("boom"))  # a failing core never breaks the state
    assert kinds(rig) == []


# ---------------------------------------------------------------- the audit: the start and the end, once each

def test_alerts_are_audited_once_when_they_start_and_once_when_they_end():
    rig = Rig()
    rig.report()
    assert rig.events("water-alert", "water-alert-cleared") == []
    for p in (1.5, 1.4, 1.3, 1.2):                              # the value changes, the alert is the same one
        rig.report(pressure_out=p)
        rig.t += 5
    assert rig.events("water-alert") == [("water-alert", "S-01", "давление ниже нормы: 1.5 бар (норма от 2)", "pump-S-01")]
    rig.report(pressure_out=4.0)
    rig.report(pressure_out=4.1)
    assert rig.events("water-alert-cleared") == [
        ("water-alert-cleared", "S-01", "давление ниже нормы: 1.5 бар (норма от 2)", "pump-S-01")]
    rig.report(pumps=[{"run": False, "fault": True}, {"run": True, "fault": False}], door="open", source_error="timeout",
               night_min_flow=60.0, night_baseline=38.5)
    rig.report(pumps=[{"run": False, "fault": True}, {"run": False, "fault": True}], door="open", source_error="timeout",
               night_min_flow=60.0, night_baseline=38.5)
    started = [e[2] for e in rig.events("water-alert")[1:]]
    assert started == ["нет связи с автоматикой станции: timeout", "авария насоса 1", "шкаф открыт",
                       "возможна утечка: ночной минимум 60 м³/ч против обычных 38.5", "авария насоса 2"]
    rig.report()
    cleared = sorted(e[2] for e in rig.events("water-alert-cleared")[1:])
    assert len(cleared) == 5 and "авария насоса 2" in cleared and "шкаф открыт" in cleared
    rig.report()
    assert len(rig.events("water-alert")) == 6 and len(rig.events("water-alert-cleared")) == 6  # nothing more


def test_offline_refused_and_os_floor_are_not_audited_as_station_alerts():
    rig = Rig(refusal=lambda device: "ОС не подтверждена")
    rig.report(refused="bad signature")
    rig.t += 100
    assert set(kinds(rig)) == {"offline", "refused", "os-floor"}
    assert rig.events("water-alert", "water-alert-cleared") == []


def test_two_stations_alerts_are_audited_each_for_its_own_site():
    rig = Rig()
    rig.records[1]["p_max"] = 5.0
    rig.report(door="open")
    rig.report("pump-S-02", "S-02", door="open", pressure_out=5.5)
    assert [(e[1], e[2]) for e in rig.events("water-alert")] == [
        ("S-01", "шкаф открыт"), ("S-02", "давление выше нормы: 5.5 бар (норма до 5)"), ("S-02", "шкаф открыт")]


# ---------------------------------------------------------------- the state and the summary

def test_the_state_has_the_shape_the_interface_reads():
    rig = Rig()
    rig.report()
    status, state = rig.call("GET", "/api/water/state")
    assert status == 200 and set(state) == {"sites", "alerts"} and state["alerts"] == 1  # S-02 never reported
    assert [s["site"] for s in state["sites"]] == ["S-01", "S-02"]                        # the revoked one is not shown
    st = site_of(state, "S-01")
    assert set(st) == {"id", "site", "lon", "lat", "online", "age", "p_min", "p_max", "pressure_in", "pressure_out",
                       "flow", "power_kw", "energy_kwh", "level", "pumps", "door", "kwh_per_m3", "night_min_flow",
                       "night_baseline", "source_error", "refused", "alerts"}
    assert (st["id"], st["kwh_per_m3"], st["night_min_flow"], st["night_baseline"], st["source_error"]) == (
        "pump-S-01", 0.31, 41.0, 38.5, None)
    empty = site_of(state, "S-02")
    assert empty["lon"] is None and empty["pumps"] == [] and empty["door"] is None and empty["age"] is None


def test_summary_counts_devices_online_and_alerts():
    rig = Rig()
    assert rig.mod.summary() == {"devices": 2, "online": 0, "alerts": 2}
    rig.report()
    rig.report("pump-S-02", "S-02", pumps=[{"run": False, "fault": True}], door="open")
    assert rig.mod.summary() == {"devices": 2, "online": 2, "alerts": 2}


# ---------------------------------------------------------------- the demo stations

def test_in_the_demo_the_module_starts_three_emulated_stations_against_the_platforms_own_listener(monkeypatch, tmp_path):
    calls = []

    async def fake(platform_addr, server_name, ca_path, pki_dir, stations):
        calls.append((platform_addr, server_name, str(ca_path), str(pki_dir), [s["site"] for s in stations]))
        await asyncio.sleep(3600)

    monkeypatch.setattr(water_module, "run_demo_stations", fake)
    extra = {"platform_addr": "127.0.0.1:9443", "server_name": "centre", "ca_path": tmp_path / "ca.pem", "pki_dir": tmp_path}

    async def go(rig):
        await rig.mod.startup()
        await rig.mod.startup()  # once
        await asyncio.sleep(0.05)
        rig.mod.stop()
        await asyncio.sleep(0.05)

    rig = Rig(demo=True, extra=extra)
    rig.records += [{"id": f"pump-S-0{n}", "kind": "water", "site": f"S-0{n}", "status": "active"} for n in (3, 4)]
    asyncio.run(go(rig))
    assert calls == [("127.0.0.1:9443", "centre", str(tmp_path / "ca.pem"), str(tmp_path), ["S-01", "S-02", "S-03"])]
    asyncio.run(go(Rig(demo=False, extra=extra)))
    assert len(calls) == 1  # the real city never gets emulated stations
