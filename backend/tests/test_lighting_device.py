"""The lighting cabinet's program (urban/modules/lighting/device.py): it obeys only a verified command for its own
cabinet with a sane level and lease; forged, replayed, stale and foreign commands are refused and the reason is
reported once; a lease that runs out (also with no link) or a release returns it to its astronomical schedule;
sunset and sunrise for Fergana are right within a few minutes; and its session to the centre works end to end."""
import asyncio
import calendar
import datetime
import json
import time

import pytest
import websockets
from websockets.asyncio.server import serve

from urban.devices import crypto
from urban.devices.health import clean
from urban.modules.lighting import solar
from urban.modules.lighting.device import EmulatedAdapter, LightingCabinet, RelayAdapter
from urban.modules.lighting.session import CabinetSession

LON, LAT = 71.78, 40.39  # Fergana


def utc(*args) -> float:
    return float(calendar.timegm(datetime.datetime(*args).timetuple()))


DAY = utc(2026, 6, 21, 8, 0)     # 13:00 local: light
NIGHT = utc(2026, 6, 21, 20, 0)  # 01:00 local: dark


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t


class Rig:
    """A cabinet with its own key, the centre's gateway key, a clock and an emulated bank of lamps."""

    def __init__(self, tmp_path, t=DAY, cabinet="C-01", device="light-C-01", adapter=None, **kw):
        self.gateway, self.key = crypto.new_key(), crypto.new_key()
        self.clock, self.seq, self.device = Clock(t), 1000, device
        self.state = tmp_path / "light.seq"
        self.adapter = adapter or EmulatedAdapter()
        self.cab = LightingCabinet(device, cabinet, self.key, crypto.load_public(crypto.public_hex(self.gateway)),
                                   self.adapter, LON, LAT, state_path=self.state, wall=self.clock, **kw)

    def env(self, body, device=None, seq=None, t=None, signer=None):
        """What DeviceHub.envelope makes: device, a rising number, the time, the body, the gateway's signature."""
        self.seq += 1
        env = {"device": device or self.device, "seq": self.seq if seq is None else seq,
               "t": int(self.clock.t if t is None else t), "body": body}
        return {**env, "sig": crypto.sign(signer or self.gateway, env)}

    def set(self, level=60, ttl=120, cabinet="C-01", **kw):
        return self.env({"lighting": {"cabinet": cabinet, "action": "set", "level": level, "ttl": ttl}}, **kw)

    def release(self, cabinet="C-01", **kw):
        return self.env({"lighting": {"cabinet": cabinet, "action": "release"}}, **kw)

    def report(self):
        return self.cab.report()["lighting"]

    def advance(self, seconds):
        self.clock.t += seconds
        self.cab.tick()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- refused commands, with their reasons

def _other_key():
    return crypto.new_key()


REFUSALS = [
    ("forged signature", lambda r: r.set(signer=_other_key()), "bad signature"),
    ("stale time", lambda r: r.set(t=r.clock.t - 60), "stale command"),
    ("time from the future", lambda r: r.set(t=r.clock.t + 60), "stale command"),
    ("another device", lambda r: r.set(device="light-C-02"), "addressed to another device"),
    ("another cabinet", lambda r: r.set(cabinet="C-02"), "not my cabinet"),
    ("level 101", lambda r: r.set(level=101), "bad level"),
    ("level -1", lambda r: r.set(level=-1), "bad level"),
    ("level as text", lambda r: r.set(level="50"), "bad level"),
    ("level as float", lambda r: r.set(level=50.5), "bad level"),
    ("level as bool", lambda r: r.set(level=True), "bad level"),
    ("ttl 5", lambda r: r.set(ttl=5), "bad ttl"),
    ("ttl 29", lambda r: r.set(ttl=29), "bad ttl"),
    ("ttl 301", lambda r: r.set(ttl=301), "bad ttl"),
    ("ttl 1000", lambda r: r.set(ttl=1000), "bad ttl"),
    ("ttl as text", lambda r: r.set(ttl="120"), "bad ttl"),
    ("unknown action", lambda r: r.env({"lighting": {"cabinet": "C-01", "action": "explode", "level": 1, "ttl": 60}}),
     "unknown action"),
    ("release for another cabinet", lambda r: r.release(cabinet="C-02"), "not my cabinet"),
    ("not a lighting command", lambda r: r.env({"tls": "J1", "op": "lease"}), "not a lighting command"),
    ("body is not an object", lambda r: r.env("set"), "not a lighting command"),
    ("lighting is not an object", lambda r: r.env({"lighting": "set"}), "not a lighting command"),
    ("malformed", lambda r: {"device": "light-C-01"}, "malformed command"),
    ("not even a dict", lambda r: "set 100", "malformed command"),
]


@pytest.mark.parametrize("what,make,reason", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_a_command_that_is_not_right_is_refused_with_its_reason_and_changes_nothing(rig, what, make, reason):
    rig.advance(0)
    before = (rig.cab.lease, rig.report()["level"])
    assert rig.cab.on_command(make(rig)) is False
    assert rig.cab.lease is None and (rig.cab.lease, rig.report()["level"]) == before
    rep = rig.cab.report()
    assert rep["lighting"]["refused"] == reason and rep["lighting"]["mode"] == "schedule"
    rig.cab.mark_sent(rep)
    assert "refused" not in rig.report()  # said once
    assert rig.cab.last_refusal == reason  # the screen still shows the last one


def test_a_refusal_is_not_used_up_by_a_report_that_never_left(rig):
    rig.cab.on_command(rig.set(level=101))
    first = rig.cab.report()
    assert rig.report()["refused"] == "bad level"  # no mark_sent: the link was down
    rig.cab.on_command(rig.set(ttl=5))  # a newer refusal replaces it
    assert rig.report()["refused"] == "bad ttl"
    rig.cab.mark_sent(first)  # a stale report being marked sent must not clear a different reason
    assert rig.report()["refused"] == "bad ttl"


def test_a_replayed_command_is_refused_even_if_it_was_good(rig):
    env = rig.set(level=40)
    assert rig.cab.on_command(env) is True
    assert rig.cab.on_command(env) is False
    assert rig.report()["refused"] == "replayed or out of order"
    assert rig.cab.on_command(rig.set(level=50, seq=env["seq"] - 5)) is False  # an older number is no better
    assert rig.report()["level"] == 40


def test_the_sequence_number_survives_a_restart(tmp_path):
    rig = Rig(tmp_path)
    env = rig.set(level=40)
    assert rig.cab.on_command(env) is True
    assert rig.state.read_text() == str(env["seq"])
    again = LightingCabinet(rig.device, "C-01", rig.key, rig.cab.gateway_public, EmulatedAdapter(), LON, LAT,
                            state_path=rig.state, wall=rig.clock)
    assert again.last_seq == env["seq"]
    assert again.on_command(env) is False  # captured before the restart, replayed after it
    assert again.on_command(rig.set(level=30)) is True


def test_a_verified_command_uses_up_its_number_even_when_its_body_is_refused(rig):
    bad = rig.set(level=101)
    assert rig.cab.on_command(bad) is False and rig.cab.last_seq == bad["seq"]
    assert rig.cab.on_command(bad) is False and rig.report()["refused"] == "replayed or out of order"


def test_an_unknown_gateway_key_means_every_command_is_refused():
    cab = LightingCabinet("light-C-01", "C-01", crypto.new_key(), None, EmulatedAdapter(), LON, LAT, wall=Clock(DAY))
    gw = crypto.new_key()
    env = {"device": "light-C-01", "seq": 1, "t": int(DAY), "body": {"lighting": {"cabinet": "C-01", "action": "release"}}}
    assert cab.on_command({**env, "sig": crypto.sign(gw, env)}) is False
    assert cab.report()["lighting"]["refused"] == "no gateway key yet"


# ---------------------------------------------------------------- obeying: lease, release, schedule

def test_a_good_set_is_obeyed_until_its_lease_ends_and_then_the_schedule_returns(rig):
    rig.advance(0)
    assert rig.report() == {"cabinet": "C-01", "mode": "schedule", "on": False, "level": 0, "lamps": 24, "failed": 0,
                            "power_w": 0.0, "door": "closed", "lease_until": None, "schedule": "astro", "dark": False}
    assert rig.cab.on_command(rig.set(level=60, ttl=120)) is True
    rep = rig.report()
    assert rep["mode"] == "override" and rep["level"] == 60 and rep["on"] is True
    assert rep["lease_until"] == int(DAY) + 120 and rep["power_w"] == pytest.approx(24 * 90 * 0.60)
    assert rep["dark"] is False and "refused" not in rep
    rig.advance(119)
    assert rig.report()["mode"] == "override"
    rig.advance(2)  # the centre never came back: 121 s after the command
    rep = rig.report()
    assert rep["mode"] == "schedule" and rep["level"] == 0 and rep["on"] is False and rep["lease_until"] is None


def test_a_lease_is_renewed_by_the_next_command_and_a_light_level_can_be_zero(rig):
    rig.cab.on_command(rig.set(level=30, ttl=60))
    rig.advance(40)
    rig.cab.on_command(rig.set(level=30, ttl=60))
    rig.advance(40)
    assert rig.report()["mode"] == "override"  # 80 s after the first, 40 s after the second
    rig.cab.on_command(rig.set(level=0, ttl=30))  # an operator dimming the street off in the middle of the night
    rig.advance(0)
    rep = rig.report()
    assert rep["mode"] == "override" and rep["level"] == 0 and rep["on"] is False and rep["power_w"] == 0.0


def test_without_a_link_the_lease_still_ends_and_the_night_schedule_takes_over(tmp_path):
    rig = Rig(tmp_path, t=NIGHT - 10)  # ten seconds before the test's night: already dark, lamps lit by the schedule
    rig.advance(0)
    assert rig.report()["level"] == 100 and rig.report()["dark"] is True
    rig.cab.on_command(rig.set(level=20, ttl=60))
    assert rig.report()["level"] == 20
    # nothing arrives any more; only the clock runs (tick is all the cabinet needs, no session)
    for _ in range(59):
        rig.advance(1)
    assert rig.report()["level"] == 20
    rig.advance(2)
    rep = rig.report()
    assert rep["mode"] == "schedule" and rep["level"] == 100 and rep["on"] is True and rep["lease_until"] is None


def test_release_returns_to_the_schedule_at_once(tmp_path):
    rig = Rig(tmp_path, t=NIGHT)
    rig.cab.on_command(rig.set(level=10, ttl=300))
    assert rig.report()["level"] == 10
    assert rig.cab.on_command(rig.release()) is True
    rep = rig.report()
    assert rep["mode"] == "schedule" and rep["level"] == 100 and rep["lease_until"] is None
    assert rig.cab.on_command(rig.release()) is True  # nothing to release is not an error


def test_a_clock_that_jumps_back_cannot_stretch_a_lease(rig):
    rig.cab.on_command(rig.set(level=50, ttl=300))
    rig.clock.t -= 3600  # a bad clock: the lease would now seem to last an hour more
    rig.cab.tick()
    assert rig.report()["mode"] == "schedule"


def test_an_open_door_and_failed_lamps_are_reported_and_cost_no_power(tmp_path):
    ad = EmulatedAdapter(failed=2, door="open")
    rig = Rig(tmp_path, adapter=ad)
    rig.cab.on_command(rig.set(level=100))
    rep = rig.report()
    assert rep["failed"] == 2 and rep["door"] == "open" and rep["lamps"] == 24
    assert rep["power_w"] == pytest.approx(22 * 90)


def test_the_emulated_failed_lamp_can_come_from_a_seed():
    seen = {EmulatedAdapter(seed=s).failed for s in range(40)}
    assert seen == {0, 1}
    assert EmulatedAdapter(seed=7).failed == EmulatedAdapter(seed=7).failed
    assert EmulatedAdapter().failed == 0
    assert EmulatedAdapter(lamps=3, failed=9).failed == 3


def test_a_cabinet_needs_a_real_position():
    for lon, lat in ((float("nan"), 40), (71, 91), (200, 40), ("71", 40)):
        with pytest.raises(ValueError):
            LightingCabinet("light-C-01", "C-01", crypto.new_key(), None, EmulatedAdapter(), lon, lat)


def test_the_report_is_what_the_hub_and_the_module_accept(rig):
    rep = rig.cab.report()
    assert rep["type"] == "report" and rep["t"] == int(DAY)
    h = clean(rep["health"])
    assert h["controller"] == "ok" and h["version"]  # the same cleaning every device's health goes through
    from urban.modules.lighting import validate_report
    assert validate_report(rep["lighting"], "C-01") is not None
    hello = rig.cab.hello("n0")
    assert crypto.verify(crypto.load_public(crypto.public_hex(rig.key)), {"device": "light-C-01", "nonce": "n0"}, hello["sig"])


def test_an_update_offer_goes_to_the_updater_and_never_to_the_lamps(tmp_path):
    class Updater:
        def __init__(self):
            self.offers = []

        def offer(self, spec):
            self.offers.append(spec)

        def health(self):
            return {"os": 2}

    up = Updater()
    rig = Rig(tmp_path, updater=up)
    assert rig.cab.on_command(rig.env({"update": {"version": 3}})) is True
    assert up.offers == [{"version": 3}] and rig.cab.lease is None
    assert rig.cab.on_command(rig.env({"update": {"version": 3}}, signer=_other_key())) is False  # forged: not even offered
    assert len(up.offers) == 1 and rig.cab.report()["health"]["os"] == 2


# ---------------------------------------------------------------- the relay adapter

def test_a_relay_writes_one_or_zero_and_reports_on_off_not_the_level(tmp_path):
    out = tmp_path / "gpio17"
    ad = RelayAdapter(out, lamps=10, door_path=tmp_path / "door")
    rig = Rig(tmp_path, adapter=ad)
    assert out.read_text() == "0" and rig.report()["door"] == "unknown"  # started in daylight, door file not there
    (tmp_path / "door").write_text("0")
    assert rig.report()["door"] == "closed"
    rig.cab.on_command(rig.set(level=35))
    assert out.read_text() == "1"  # no dimming on a contactor
    rep = rig.report()
    assert rep["level"] == 100 and rep["on"] is True and rep["power_w"] == pytest.approx(900.0)
    (tmp_path / "door").write_text("1")
    assert rig.report()["door"] == "open"
    rig.cab.on_command(rig.set(level=0, ttl=30))
    assert out.read_text() == "0" and rig.report()["level"] == 0


def test_a_relay_with_dimming_writes_the_level(tmp_path):
    out = tmp_path / "dim"
    rig = Rig(tmp_path, adapter=RelayAdapter(out, dimming=True))
    rig.cab.on_command(rig.set(level=35))
    assert out.read_text() == "35" and rig.report()["level"] == 35


def test_a_relay_that_cannot_be_written_says_so_and_keeps_its_last_state(tmp_path):
    out = tmp_path / "gpio"
    ad = RelayAdapter(out)
    rig = Rig(tmp_path, adapter=ad, t=NIGHT)
    assert out.read_text() == "1" and rig.report()["level"] == 100
    ad.path = tmp_path / "missing-dir" / "gpio"
    rig.cab.on_command(rig.release())
    rig.cab.on_command(rig.set(level=0, ttl=30))
    rep = rig.cab.report()
    assert rep["health"]["controller"].startswith("relay:") and str(tmp_path) not in rep["health"]["controller"]
    assert rep["lighting"]["level"] == 100  # what the contactor really still does
    ad.path = out  # repaired
    rig.cab.tick()
    assert out.read_text() == "0" and rig.cab.report()["health"]["controller"] == "ok"


def test_a_photocell_decides_when_there_is_one_and_the_astronomical_timer_when_it_is_unreadable(tmp_path):
    cell = tmp_path / "cell"
    cell.write_text("1")
    rig = Rig(tmp_path, adapter=RelayAdapter(tmp_path / "out", photocell_path=cell), t=DAY)  # day by the sun, dark by the cell
    rep = rig.report()
    assert rep["schedule"] == "photocell" and rep["dark"] is True and rep["level"] == 100
    cell.write_text("0")
    rig.advance(1)
    assert rig.report()["level"] == 0 and rig.report()["dark"] is False
    cell.unlink()  # a dead photocell: back to the timer, which says day
    rig.advance(1)
    assert rig.report()["schedule"] == "astro" and rig.report()["level"] == 0
    rig.clock.t = NIGHT
    rig.cab.tick()
    assert rig.report()["schedule"] == "astro" and rig.report()["level"] == 100


# ---------------------------------------------------------------- sunset and sunrise for Fergana
# Reference: api.sunrise-sunset.org (USNO-style solar tables), lat 40.39, lng 71.78, formatted=0, fetched 2026-10-19:
#   2026-06-21: sunrise 2026-06-20T23:41:18Z, sunset 2026-06-21T14:48:01Z (solar noon 07:14:39Z, day 15 h 06 m)
#   2026-12-21: sunrise 2026-12-21T02:30:38Z, sunset 2026-12-21T11:51:02Z (solar noon 07:10:50Z, day  9 h 20 m)
# (local time in Fergana is UTC+5, so the June sunrise is about 04:41 and the sunset about 19:48; December about
#  07:31 and 16:51). The program must agree within 5 minutes.
REFERENCE = {
    datetime.date(2026, 6, 21): (utc(2026, 6, 20, 23, 41, 18), utc(2026, 6, 21, 14, 48, 1)),
    datetime.date(2026, 12, 21): (utc(2026, 12, 21, 2, 30, 38), utc(2026, 12, 21, 11, 51, 2)),
}


@pytest.mark.parametrize("day", sorted(REFERENCE))
def test_sunrise_and_sunset_for_fergana_agree_with_the_reference_within_five_minutes(day):
    rise, sets = solar.sun_times(day, LAT, LON)
    ref_rise, ref_set = REFERENCE[day]
    assert abs(rise - ref_rise) <= 300, (rise - ref_rise) / 60
    assert abs(sets - ref_set) <= 300, (sets - ref_set) / 60
    assert 9.0 * 3600 < sets - rise < 15.5 * 3600


@pytest.mark.parametrize("day", sorted(REFERENCE))
def test_the_lamps_are_on_after_sunset_and_off_after_sunrise(day, tmp_path):
    """Decided against the reference times (not the program's own), a quarter of an hour either side."""
    ref_rise, ref_set = REFERENCE[day]
    rig = Rig(tmp_path, t=ref_set - 900)
    rig.advance(0)
    assert rig.report()["level"] == 0 and rig.report()["dark"] is False  # the sun is still up
    rig.advance(1800)  # a quarter of an hour after sunset
    assert rig.report()["level"] == 100 and rig.report()["dark"] is True and rig.report()["on"] is True
    rig.clock.t = ref_rise - 900
    rig.cab.tick()
    assert rig.report()["level"] == 100  # still night
    rig.advance(1800)
    assert rig.report()["level"] == 0 and rig.report()["dark"] is False
    rig.clock.t = (ref_rise + ref_set) / 2  # noon and midnight, for good measure
    rig.cab.tick()
    assert rig.report()["dark"] is False
    rig.clock.t = (ref_rise + ref_set) / 2 + 12 * 3600
    rig.cab.tick()
    assert rig.report()["dark"] is True


def test_the_sun_at_the_poles_gives_no_sunrise_and_the_dark_follows(tmp_path):
    assert solar.sun_times(datetime.date(2026, 6, 21), 80.0, 20.0) is None  # midnight sun
    assert solar.sun_times(datetime.date(2026, 12, 21), 80.0, 20.0) is None  # polar night
    assert solar.is_dark(utc(2026, 12, 21, 12), 80.0, 20.0) is True
    assert solar.is_dark(utc(2026, 6, 21, 0), 80.0, 20.0) is False


def test_the_seasons_move_sunset_the_right_way():
    june = solar.sun_times(datetime.date(2026, 6, 21), LAT, LON)
    dec = solar.sun_times(datetime.date(2026, 12, 21), LAT, LON)
    assert june[1] % 86400 > dec[1] % 86400 + 2 * 3600  # sunset is hours later in June
    assert (june[1] - june[0]) > (dec[1] - dec[0]) + 5 * 3600  # and the day is much longer


# ---------------------------------------------------------------- the session to the centre

class Hub:
    """A local stand-in for the platform's device listener: challenge, hello check, welcome, reports in, commands out."""

    def __init__(self, gateway, device_public, device="light-C-01"):
        self.gateway, self.public, self.device = gateway, device_public, device
        self.reports, self.sessions, self.ws, self.welcome = [], 0, None, {"type": "welcome", "t": 0, "tls": None}
        self.server = None

    async def handler(self, ws):
        await ws.send(json.dumps({"type": "challenge", "nonce": "abc123"}))
        hello = json.loads(await ws.recv())
        if not crypto.verify(self.public, {"device": self.device, "nonce": "abc123"}, hello["sig"]):
            await ws.close(code=4401)
            return
        self.sessions += 1
        await ws.send(json.dumps(self.welcome))
        self.ws = ws
        try:
            async for text in ws:
                self.reports.append(json.loads(text))
        finally:
            self.ws = None

    async def start(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def command(self, body, seq, signer=None, t=None):
        env = {"device": self.device, "seq": seq, "t": int(t if t is not None else time.time()), "body": body}
        return json.dumps({"type": "cmd", **env, "sig": crypto.sign(signer or self.gateway, env)})

    async def until(self, test, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = next((r for r in reversed(self.reports) if test(r)), None)
            if found is not None:
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"no such report in {timeout} s; last: {self.reports[-1:]}")


def test_the_session_reports_obeys_signed_commands_refuses_forged_ones_and_survives_the_centre_going_away(tmp_path):
    offset = [0.0]
    gateway, key = crypto.new_key(), crypto.new_key()
    status = tmp_path / "run" / "lighting.json"
    cab = LightingCabinet("light-C-01", "C-01", key, crypto.load_public(crypto.public_hex(gateway)), EmulatedAdapter(),
                          LON, LAT, state_path=tmp_path / "seq", wall=lambda: time.time() + offset[0])
    logs = []

    async def scenario():
        hub = Hub(gateway, crypto.load_public(crypto.public_hex(key)))
        port = await hub.start()
        session = CabinetSession(cab, f"ws://127.0.0.1:{port}", None, None, status=status, report_every=0.2,
                                 log=lambda *a, **k: logs.append(a))
        runner = asyncio.ensure_future(session.run())
        try:
            first = await hub.until(lambda r: r.get("type") == "report")
            assert first["lighting"]["cabinet"] == "C-01" and first["health"]["controller"] == "ok" and hub.sessions == 1
            seq = int(time.time() * 1000)
            await hub.ws.send(hub.command({"lighting": {"cabinet": "C-01", "action": "set", "level": 70, "ttl": 90}}, seq))
            held = await hub.until(lambda r: r["lighting"]["mode"] == "override")
            assert held["lighting"]["level"] == 70 and held["lighting"]["lease_until"] >= int(time.time()) + 88
            # forged, and meant for another cabinet: refused, said once, and the lease is not touched
            await hub.ws.send(hub.command({"lighting": {"cabinet": "C-01", "action": "release"}}, seq + 1, signer=crypto.new_key()))
            bad = await hub.until(lambda r: r["lighting"].get("refused") == "bad signature")
            assert bad["lighting"]["mode"] == "override"
            await hub.ws.send(hub.command({"lighting": {"cabinet": "C-02", "action": "release"}}, seq + 2))
            await hub.until(lambda r: r["lighting"].get("refused") == "not my cabinet")
            await asyncio.sleep(0.6)
            count = sum(1 for r in hub.reports if r["lighting"].get("refused") == "not my cabinet")
            assert count == 1  # once, not at every report
            state = json.loads(status.read_text())
            assert state["unit"] == "light-C-01" and state["link"] == "online" and state["mode"] == "override"
            assert state["level"] == 70 and state["last_refused"] == "not my cabinet" and state["cabinet"] == "C-01"
            assert not list(status.parent.glob("*.tmp"))  # written atomically
            # the centre goes away: the lease runs out by the cabinet's own clock, with no link at all
            await hub.stop()
            if hub.ws is not None:
                await hub.ws.close()
            offset[0] += 100
            for _ in range(80):
                await asyncio.sleep(0.1)
                state = json.loads(status.read_text())
                if state["mode"] == "schedule" and state["link"] == "offline":
                    break
            assert state["mode"] == "schedule" and state["link"] == "offline" and state["lease_until"] is None
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())
    assert any("online" in str(a) for a in logs)


def test_the_session_does_not_work_with_a_centre_whose_certificate_it_cannot_trust(tmp_path):
    root = crypto.new_key()
    gateway, key = crypto.new_key(), crypto.new_key()
    cab = LightingCabinet("light-C-01", "C-01", key, None, EmulatedAdapter(), LON, LAT, wall=Clock(NIGHT),
                          root_public=crypto.load_public(crypto.public_hex(root)))
    assert cab.on_welcome({"type": "welcome", "gateway": None}) is False and "no gateway certificate" in cab.refused
    forged = {"gateway": crypto.public_hex(gateway), "epoch": 1, "issued": "2026-01-01", "sig": "00" * 64}
    assert cab.on_welcome({"type": "welcome", "gateway": forged}) is False and "not signed by the root" in cab.refused
    from urban.devices import trust
    cert = trust.issue(root, crypto.public_hex(gateway), 1)
    assert cab.on_welcome({"type": "welcome", "gateway": cert}) is True and cab.trust.epoch == 1
    # and the lamps keep their schedule whoever the centre is
    assert cab.report()["lighting"]["level"] == 100
