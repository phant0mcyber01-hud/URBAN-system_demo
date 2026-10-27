"""A pump station's unit (urban/modules/water: regmap, readonly, adapters, observe, device, session, __main__): the
register map is checked whole at start with clear errors and decoded right; ReadOnlyModbus builds only functions 01-04
and refuses every other one before a byte is sent; against a fake PLC over TCP and over RTU the station uses nothing
but 01-04; every signed command is refused with its reason in the next report, once, and only an OS update is taken;
kWh per m3 and the night minimum with its baseline are right on a fake clock and survive a restart; a station whose
automation is unreachable reports null values and the reason and keeps its link; and the water module imports no
code that can write to equipment."""
import ast
import asyncio
import calendar
import datetime
import json
import math
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

import urban.modules.water as water_pkg
from urban.devices import crypto
from urban.devices.health import clean
from urban.modules.water import validate_report
from urban.modules.water import __main__ as water_main
from urban.modules.water.adapters import EmulatedStation, ModbusStation
from urban.modules.water.device import READ_ONLY, WaterStation
from urban.modules.water.observe import Observations
from urban.modules.water.readonly import (ModbusError, ReadOnlyModbus, RtuTransport, TcpTransport, WriteRefused,
                                          build_pdu, crc16, parse_pdu, with_crc)
from urban.modules.water.regmap import Register, decode, load_map, parse_map
from urban.modules.water.session import StationSession, write_status

WATER_DIR = Path(water_pkg.__file__).parent
TEMPLATE = WATER_DIR / "map-template.json"


def local(day, hour, minute=0, second=0) -> float:
    """Epoch seconds of a Fergana wall-clock time (UTC+5) in October 2026."""
    return float(calendar.timegm(datetime.datetime(2026, 10, day, hour, minute, second).timetuple()) - 5 * 3600)


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- the register map

def test_the_template_map_loads_and_says_what_it_reads():
    m = load_map(TEMPLATE)
    assert set(m.values) == {"pressure_in", "pressure_out", "flow", "power", "energy", "level"}
    assert m.values["energy"] == Register("holding", 10, "u32", 1.0, "big") and m.door.true_means is True
    assert len(m.pumps) == 2 and m.pumps[1][1].address == 11
    assert parse_map({}).values == {} and parse_map({}).door is None and parse_map({"pumps": None}).pumps == ()


BAD_MAPS = [
    ("not an object", [], "must be a JSON object"),
    ("unknown field", {"presure_out": {"table": "input", "address": 1}}, "unknown field 'presure_out'"),
    ("a write table", {"flow": {"table": "coil_write", "address": 1}}, "flow: table must be holding"),
    ("a bit table for a value", {"flow": {"table": "discrete", "address": 1}}, "flow: table must be holding"),
    ("no address", {"flow": {"table": "input"}}, "flow: address must be a whole number"),
    ("negative address", {"flow": {"table": "input", "address": -1}}, "flow: address"),
    ("address as text", {"flow": {"table": "input", "address": "1"}}, "flow: address"),
    ("address too far for 32 bits", {"energy": {"table": "holding", "address": 65535, "type": "u32"}}, "0..65534"),
    ("unknown type", {"flow": {"table": "input", "address": 1, "type": "u64"}}, "type must be one of"),
    ("zero scale", {"flow": {"table": "input", "address": 1, "scale": 0}}, "scale must be a finite number"),
    ("NaN scale", {"flow": {"table": "input", "address": 1, "scale": float("nan")}}, "scale"),
    ("bad word order", {"energy": {"table": "holding", "address": 1, "type": "u32", "word_order": "middle"}},
     "word_order must be big or little"),
    ("unknown key", {"flow": {"table": "input", "address": 1, "function": 6}}, "unknown key 'function'"),
    ("door in registers", {"door": {"table": "input", "address": 0}}, "door: table must be coil"),
    ("door open not a bool", {"door": {"table": "discrete", "address": 0, "open": 1}}, "open must be true or false"),
    ("pumps not a list", {"pumps": {"run": {"table": "discrete", "address": 1}}}, "pumps must be a list"),
    ("17 pumps", {"pumps": [{}] * 17}, "at most 16 pumps"),
    ("pump with a scale", {"pumps": [{"run": {"table": "discrete", "address": 1, "scale": 2}}]}, "pumps[1].run: unknown key"),
    ("pump run in registers", {"pumps": [{}, {"fault": {"table": "holding", "address": 1}}]}, "pumps[2].fault: table"),
]


@pytest.mark.parametrize("what,data,message", BAD_MAPS, ids=[b[0] for b in BAD_MAPS])
def test_a_wrong_map_is_an_error_that_says_where(what, data, message):
    with pytest.raises(ValueError) as err:
        parse_map(data)
    assert message in str(err.value) and str(err.value).startswith("map:")


def test_a_map_file_that_is_missing_or_not_json_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        load_map(tmp_path / "none.json")
    (tmp_path / "bad.json").write_text("{flow: 1", encoding="utf-8")
    with pytest.raises(ValueError, match="is not JSON"):
        load_map(tmp_path / "bad.json")


@pytest.mark.parametrize("kind,order,words,scale,value", [
    ("u16", "big", [420], 0.01, 4.2), ("u16", "big", [65535], 1, 65535), ("i16", "big", [65535], 1, -1),
    ("i16", "big", [0xFF38], 0.01, -2.0), ("u32", "big", [0x0012, 0x5C90], 1, 1203344),
    ("u32", "little", [0x5C90, 0x0012], 1, 1203344), ("i32", "big", [0xFFFF, 0xFFFE], 1, -2),
    ("i32", "little", [0xFFFE, 0xFFFF], 0.5, -1.0), ("f32", "big", [0x4086, 0x6666], 1, 4.2),
    ("f32", "little", [0x6666, 0x4086], 10, 42.0)])
def test_registers_decode_with_type_word_order_and_scale(kind, order, words, scale, value):
    got = decode(words, Register("holding", 0, kind, float(scale), order))
    assert math.isclose(got, value, rel_tol=1e-6)


def test_a_float_that_is_not_a_number_decodes_to_none_and_wrong_words_are_an_error():
    f32 = Register("input", 0, "f32")
    assert decode([0x7FC0, 0x0000], f32) is None and decode([0x7F80, 0x0000], f32) is None  # NaN, +inf
    with pytest.raises(ValueError):
        decode([1], f32)
    with pytest.raises(ValueError):
        decode([70000], Register("input", 0))


# ---------------------------------------------------------------- ReadOnlyModbus: reads, nothing else

WRITES = [5, 6, 15, 16, 0x16, 0x17, 0x2B, 0, 7, 8, 0x14, 0x15, 255, -1, "3", 3.0, True, None]


@pytest.mark.parametrize("function", WRITES, ids=[repr(f) for f in WRITES])
def test_any_function_but_a_read_is_refused_before_anything_is_built_or_sent(function):
    with pytest.raises(WriteRefused, match="only reads"):
        build_pdu(function, 0, 1)

    class Recorder:
        def __init__(self):
            self.wrote = []

        def write(self, data):
            self.wrote.append(data)

        def read(self, n):
            return b""

    connects = []
    tcp = TcpTransport("192.0.2.1", 502, connect=lambda *a, **k: connects.append(a))
    rtu_line = Recorder()
    rtu = RtuTransport(None, 1, serial=rtu_line)
    client = ReadOnlyModbus(tcp)
    with pytest.raises(WriteRefused):
        client.read(function, 0, 1)
    raw = struct.pack(">BHH", function, 0, 1) if type(function) is int and 0 <= function <= 255 else b""
    for transport in (tcp, rtu):  # even a PDU made by hand cannot get through a transport
        with pytest.raises(WriteRefused):
            transport.exchange(raw)
    assert connects == [] and rtu_line.wrote == []
    assert not any(hasattr(client, name) for name in ("call", "write", "write_coils", "write_register", "write_registers"))


def test_reads_are_built_within_the_protocols_limits():
    assert build_pdu(3, 10, 2) == bytes([3, 0, 10, 0, 2])
    assert build_pdu(1, 65535, 1) == bytes([1, 0xFF, 0xFF, 0, 1])
    for function, address, count in ((3, 0, 0), (3, 0, 126), (1, 0, 2001), (4, 65535, 2), (4, -1, 1), (4, 0, True)):
        with pytest.raises(ValueError):
            build_pdu(function, address, count)


def test_answers_are_parsed_and_exceptions_and_short_answers_are_errors():
    req = build_pdu(3, 0, 2)
    assert parse_pdu(req, bytes([3, 4, 0, 1, 0, 2])) == [1, 2]
    assert parse_pdu(build_pdu(2, 0, 10), bytes([2, 2, 0b00000101, 0b10])) == [True, False, True] + [False] * 6 + [True]
    with pytest.raises(ModbusError, match="illegal data address"):
        parse_pdu(req, bytes([0x83, 2]))
    with pytest.raises(ModbusError, match="short"):
        parse_pdu(req, bytes([3, 4, 0, 1]))
    with pytest.raises(ModbusError, match="another function"):
        parse_pdu(req, bytes([4, 4, 0, 1, 0, 2]))
    with pytest.raises(ModbusError):
        parse_pdu(req, b"")


# ---------------------------------------------------------------- a fake PLC that records every function it is asked

class FakePlc:
    """A Modbus device: tables of values; it answers reads, refuses anything else (exception 01) and records the
    function code of every request it gets, whatever it is."""

    def __init__(self):
        self.functions: list[int] = []
        self.registers = {("input", 0): 80, ("input", 1): 420, ("input", 2): 1185, ("input", 3): 372, ("input", 4): 310,
                          ("holding", 10): 0x0012, ("holding", 11): 0x5C90}
        self.bits = {("discrete", 0): False, ("discrete", 8): True, ("discrete", 9): False, ("discrete", 10): False,
                     ("discrete", 11): True, ("coil", 3): True}
        self.down = False

    def answer(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        self.functions.append(fc)
        if fc not in (1, 2, 3, 4) or len(pdu) != 5:
            return bytes([fc | 0x80, 1])
        _, address, count = struct.unpack(">BHH", pdu)
        if fc in (3, 4):
            table = "holding" if fc == 3 else "input"
            values = [self.registers.get((table, a), 0) for a in range(address, address + count)]
            return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *values)
        table = "coil" if fc == 1 else "discrete"
        data = bytearray((count + 7) // 8)
        for i in range(count):
            if self.bits.get((table, address + i)):
                data[i // 8] |= 1 << (i % 8)
        return bytes([fc, len(data)]) + bytes(data)


class TcpPlc(FakePlc):
    def __init__(self):
        super().__init__()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.client, args=(conn,), daemon=True).start()

    def client(self, conn):
        with conn:
            while True:
                head = b""
                while len(head) < 7:
                    chunk = conn.recv(7 - len(head))
                    if not chunk:
                        return
                    head += chunk
                tid, _proto, length, unit = struct.unpack(">HHHB", head)
                pdu = b""
                while len(pdu) < length - 1:
                    pdu += conn.recv(length - 1 - len(pdu))
                if self.down:
                    return  # the line is cut
                body = self.answer(pdu)
                conn.sendall(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)

    def close(self):
        self.server.close()


def test_against_a_fake_plc_over_tcp_the_station_reads_everything_with_functions_1_to_4_only(tmp_path):
    plc = TcpPlc()
    try:
        regmap = parse_map({**json.loads(TEMPLATE.read_text(encoding="utf-8")),
                            "door": {"table": "coil", "address": 3, "open": False}})
        station = ModbusStation(ReadOnlyModbus(TcpTransport("127.0.0.1", plc.port, unit=1, timeout=2)), regmap)
        for _ in range(5):
            got = station.read(0)
        assert got == {"pressure_in": 0.8, "pressure_out": 4.2, "flow": 118.5, "power_kw": 37.2, "energy_kwh": 1203344.0,
                       "level": 3.1, "door": "closed", "pumps": [{"run": True, "fault": False},
                                                                 {"run": False, "fault": True}]}
        for function in (5, 6, 15, 16):  # nothing a caller does gets a write onto the wire
            with pytest.raises(WriteRefused):
                station.client.read(function, 0, 1)
        station.close()
        assert plc.functions and set(plc.functions) == {1, 2, 3, 4}
        assert len(plc.functions) == 5 * len(station.requests)  # close points are read together
    finally:
        plc.close()


def test_a_whole_station_polls_a_fake_plc_for_a_while_and_the_plc_sees_only_reads(tmp_path):
    plc = TcpPlc()
    try:
        transport = TcpTransport("127.0.0.1", plc.port, unit=1, timeout=2)
        rig = Rig(tmp_path, adapter=ModbusStation(ReadOnlyModbus(transport), load_map(TEMPLATE)))
        for _ in range(20):
            rig.st.poll()
            rig.st.on_command(rig.water_cmd())                   # the centre insists: still nothing is written
            rig.clock.t += 1
        rep = rig.st.report()["water"]
        assert rep["pressure_out"] == 4.2 and rep["source_error"] is None and rep["refused"] == READ_ONLY
        assert set(plc.functions) <= {1, 2, 3, 4} and len(plc.functions) == 20 * 3
    finally:
        plc.close()


class RtuLine(FakePlc):
    """The serial line of an RTU device: a request frame in, its answer out (with the CRC)."""

    def __init__(self, unit=1):
        super().__init__()
        self.unit, self.out, self.corrupt = unit, b"", False

    def write(self, frame: bytes):
        assert crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")
        answer = with_crc(bytes([frame[0]]) + self.answer(frame[1:-2]))
        if self.corrupt:
            answer = answer[:-1] + bytes([answer[-1] ^ 0xFF])
        self.out += answer

    def read(self, n: int) -> bytes:
        data, self.out = self.out[:n], self.out[n:]
        return data


def test_over_rtu_the_same_reads_and_a_bad_crc_or_silence_is_a_modbus_error():
    line = RtuLine()
    station = ModbusStation(ReadOnlyModbus(RtuTransport(None, 1, serial=line)), load_map(TEMPLATE))
    assert station.read(0)["flow"] == 118.5 and station.read(0)["energy_kwh"] == 1203344.0
    assert set(line.functions) == {2, 3, 4}
    line.corrupt = True
    with pytest.raises(ModbusError, match="CRC"):
        station.read(0)
    line.corrupt = False

    class Silent:
        def write(self, frame):
            pass

        def read(self, n):
            return b""
    with pytest.raises(ModbusError, match="timeout"):
        ReadOnlyModbus(RtuTransport(None, 1, serial=Silent())).read_holding(0, 1)
    with pytest.raises(ValueError):
        RtuTransport(None, 0)                                   # 1..247 on a serial line


def test_an_exception_from_the_plc_is_an_error_not_a_value():
    line = RtuLine()
    client = ReadOnlyModbus(RtuTransport(None, 1, serial=line))
    line.answer = lambda pdu: bytes([pdu[0] | 0x80, 4])
    with pytest.raises(ModbusError, match="device failure"):
        client.read_input(0, 1)


# ---------------------------------------------------------------- the emulated station

def test_the_emulated_station_has_a_daily_pattern_a_counter_and_a_pump_in_fault():
    st = EmulatedStation(fault_pump=1)
    night, morning, day = (st.flow_at(local(1, h)) for h in (3, 8, 15))
    assert night < 50 < 100 < min(morning, day)
    first = st.read(local(1, 12))
    later = st.read(local(1, 13))
    assert later["energy_kwh"] > first["energy_kwh"] and later["pumps"][0] == {"run": False, "fault": True}
    assert later["pumps"][1]["run"] is True                     # the other pump takes over
    healthy = EmulatedStation().read(local(1, 3))
    assert healthy["pumps"] == [{"run": True, "fault": False}, {"run": False, "fault": False}]
    assert validate_report({"site": "S-01", **healthy}, "S-01") is not None and 2 < healthy["pressure_out"] < 6


# ---------------------------------------------------------------- the station: commands refused, updates taken

class Rig:
    """A station with its own key, the centre's gateway key, a clock and an emulated (or given) station."""

    def __init__(self, tmp_path, adapter=None, t=None, **kw):
        self.gateway, self.key = crypto.new_key(), crypto.new_key()
        self.clock, self.seq = Clock(t or local(1, 12)), 1000
        self.st = WaterStation("pump-S-01", "S-01", self.key, crypto.load_public(crypto.public_hex(self.gateway)),
                               adapter or EmulatedStation(), state_path=tmp_path / "pump.seq", wall=self.clock, **kw)

    def env(self, body, device="pump-S-01", seq=None, t=None, signer=None):
        self.seq += 1
        env = {"device": device, "seq": self.seq if seq is None else seq,
               "t": int(self.clock.t if t is None else t), "body": body}
        return {**env, "sig": crypto.sign(signer or self.gateway, env)}

    def water_cmd(self, **kw):
        return self.env({"water": {"site": "S-01", "action": "start", "pump": 1}}, **kw)


COMMANDS = [
    ("a water command", lambda r: r.water_cmd(), READ_ONLY),
    ("a pump stop", lambda r: r.env({"water": {"site": "S-01", "action": "stop", "pump": 2}}), READ_ONLY),
    ("a lighting command", lambda r: r.env({"lighting": {"cabinet": "S-01", "action": "set", "level": 1, "ttl": 60}}),
     READ_ONLY),
    ("a traffic lease", lambda r: r.env({"tls": "T1", "op": "lease", "plan": 2}), READ_ONLY),
    ("an update with something else", lambda r: r.env({"update": {"version": 3}, "water": {"action": "start"}}), READ_ONLY),
    ("not even an object", lambda r: r.env("start"), READ_ONLY),
    ("forged", lambda r: r.water_cmd(signer=crypto.new_key()), "bad signature"),
    ("stale", lambda r: r.water_cmd(t=r.clock.t - 600), "stale command"),
    ("for another device", lambda r: r.water_cmd(device="pump-S-02"), "addressed to another device"),
    ("malformed", lambda r: {"device": "pump-S-01"}, "malformed command"),
]


@pytest.mark.parametrize("what,make,reason", COMMANDS, ids=[c[0] for c in COMMANDS])
def test_every_command_is_refused_and_the_reason_goes_up_once(tmp_path, what, make, reason):
    rig = Rig(tmp_path)
    rig.st.poll()
    before = rig.st.report()["water"]
    assert rig.st.on_command(make(rig)) is False
    rep = rig.st.report()
    assert rep["water"]["refused"] == reason
    assert {k: v for k, v in rep["water"].items() if k != "refused"} == {k: v for k, v in before.items() if k != "refused"}
    rig.st.mark_sent(rep)
    assert rig.st.report()["water"]["refused"] is None and rig.st.last_refusal == reason
    assert validate_report(rep["water"], "S-01")["refused"] == reason  # the centre takes the Russian words as they are


def test_a_refused_command_uses_up_its_number_and_cannot_be_replayed(tmp_path):
    rig = Rig(tmp_path)
    env = rig.water_cmd()
    rig.st.on_command(env)
    assert rig.st.on_command(env) is False and rig.st.report()["water"]["refused"] == "replayed or out of order"
    assert (tmp_path / "pump.seq").read_text() == str(env["seq"])
    again = Rig(tmp_path)                                         # restarted: the number is remembered
    assert again.st.last_seq == env["seq"]


def test_only_a_signed_os_update_is_taken_and_it_goes_to_the_updater(tmp_path):
    class Updater:
        def __init__(self):
            self.offers = []

        def offer(self, spec):
            self.offers.append(spec)

        def health(self):
            return {"os": 2}

    up = Updater()
    rig = Rig(tmp_path, updater=up)
    assert rig.st.on_command(rig.env({"update": {"version": 3}})) is True
    assert up.offers == [{"version": 3}] and rig.st.report()["water"]["refused"] is None
    assert rig.st.on_command(rig.env({"update": {"version": 4}}, signer=crypto.new_key())) is False  # forged: not offered
    assert up.offers == [{"version": 3}] and rig.st.report()["health"]["os"] == 2


def test_the_report_is_what_the_hub_and_the_module_accept(tmp_path):
    rig = Rig(tmp_path)
    rig.st.poll()
    rep = rig.st.report()
    assert rep["type"] == "report" and rep["t"] == int(rig.clock.t) and clean(rep["health"])["controller"] == "ok"
    water = rep["water"]
    assert set(water) == {"site", "pressure_in", "pressure_out", "flow", "power_kw", "energy_kwh", "level", "pumps",
                          "door", "kwh_per_m3", "night_min_flow", "night_baseline", "source_error", "refused"}
    assert validate_report(water, "S-01") is not None and water["site"] == "S-01" and water["flow"] > 0
    hello = rig.st.hello("n0")
    assert crypto.verify(crypto.load_public(crypto.public_hex(rig.key)), {"device": "pump-S-01", "nonce": "n0"}, hello["sig"])


def test_values_the_centre_would_refuse_are_reported_as_null_not_sent(tmp_path):
    class Odd:
        def read(self, now):
            return {"pressure_in": float("nan"), "pressure_out": 250.0, "flow": -3, "power_kw": "12", "energy_kwh": 1e14,
                    "level": True, "pumps": [{"run": 1, "fault": False}, "x"] + [{"run": True}] * 20, "door": "ajar"}

        def close(self):
            pass
    rig = Rig(tmp_path, adapter=Odd())
    rig.st.poll()
    water = rig.st.report()["water"]
    assert all(water[k] is None for k in ("pressure_in", "pressure_out", "flow", "power_kw", "energy_kwh", "level"))
    assert water["door"] == "unknown" and len(water["pumps"]) == 16 and water["pumps"][0] == {"run": None, "fault": False}
    assert validate_report(water, "S-01") is not None                 # the report still counts


def test_an_unreachable_automation_gives_null_values_and_a_short_reason(tmp_path):
    class Broken:
        def __init__(self):
            self.err = ModbusError("modbus tcp 10.0.0.5:502: timed out")

        def read(self, now):
            raise self.err

        def close(self):
            pass
    adapter = Broken()
    rig = Rig(tmp_path, adapter=adapter)
    rig.st.poll()
    rep = rig.st.report()
    water = rep["water"]
    assert water["source_error"] == "modbus tcp 10.0.0.5:502: timed out" and water["flow"] is None
    assert water["pumps"] == [] and water["door"] == "unknown" and rep["health"]["controller"] == water["source_error"]
    adapter.err = PermissionError(13, "denied", "/dev/ttyUSB0")       # not ours: only its class goes up, no path
    rig.st.poll()
    assert rig.st.report()["water"]["source_error"] == "PermissionError"
    with pytest.raises(ValueError):
        WaterStation("pump-S-01", "S-01", rig.key, None, adapter, lon=200, lat=0)


# ---------------------------------------------------------------- what the station works out

def feed(obs, start, end, step, flow, energy=None, per_m3=None):
    """Readings from start to end every step seconds; with per_m3 the counter rises with the volume."""
    t, e = start, energy
    while t < end:
        f = flow(t) if callable(flow) else flow
        obs.observe(t, f, e)
        if e is not None and per_m3 is not None:
            e += per_m3 * f * step / 3600.0
        t += step
    return e


def test_kwh_per_m3_over_the_last_hour_needs_a_cubic_metre_and_skips_gaps_and_counter_resets(tmp_path):
    obs = Observations(tmp_path / "pump-S-01.water.json")
    t0 = local(1, 12)
    obs.observe(t0, 120.0, 1000.0)
    obs.observe(t0 + 10, 120.0, 1000.1)
    assert obs.kwh_per_m3() is None                               # a third of a cubic metre
    e = feed(obs, t0 + 20, t0 + 1800, 10, 120.0, 1000.2, per_m3=0.3)
    assert obs.kwh_per_m3() == pytest.approx(0.3, abs=0.005)
    # a pause in reading: the energy that came meanwhile is not set against no water
    e = feed(obs, t0 + 1800 + 600, t0 + 2700, 10, 120.0, e + 50.0, per_m3=0.3)
    assert obs.kwh_per_m3() == pytest.approx(0.3, abs=0.005)
    feed(obs, t0 + 2700, t0 + 2800, 10, 120.0, 5.0, per_m3=0.3)     # a counter replaced: starts from 5
    assert obs.kwh_per_m3() == pytest.approx(0.3, abs=0.005)
    assert all(t0 + 2800 - s[0] <= 3600 for s in obs.samples)       # only the last hour is kept
    obs2 = Observations(tmp_path / "none.json")
    feed(obs2, t0, t0 + 1800, 10, 120.0, None)                     # no counter: no figure
    assert obs2.kwh_per_m3() is None


def night_flow(minimum):
    """Flow over a night window: the minimum near 03:00, more around it."""
    def flow(t):
        minutes = ((t + 5 * 3600) % 86400 - 2 * 3600) / 60.0
        return round(minimum + abs(minutes - 60) / 10.0, 2)
    return flow


def test_the_night_minimum_and_its_baseline_from_the_fergana_night_and_after_a_restart(tmp_path):
    path = tmp_path / "pump-S-01.water.json"
    obs = Observations(path)
    minimums = {1: 40.0, 2: 38.0, 3: 39.0, 4: 41.0, 5: 60.0}
    for day in (1, 2):
        feed(obs, local(day, 1, 30), local(day, 4, 30), 60, night_flow(minimums[day]))
    assert obs.night(local(2, 3, 0)) == (40.0, None)               # in the window: last night is the one before
    assert obs.night(local(2, 5, 0)) == (38.0, None)               # fewer than 3 nights before it: no baseline
    for day in (3, 4):
        feed(obs, local(day, 1, 30), local(day, 4, 30), 60, night_flow(minimums[day]))
    assert obs.night(local(4, 5, 0)) == (41.0, 39.0)               # the median of 40, 38, 39
    restarted = Observations(path)                                 # read back from the file, no close() needed
    assert restarted.night(local(4, 5, 0)) == (41.0, 39.0)
    feed(restarted, local(5, 1, 30), local(5, 4, 30), 60, night_flow(minimums[5]))
    restarted.close()
    assert Observations(path).night(local(5, 12, 0)) == (60.0, 39.5)  # median of 40, 38, 39, 41: a leak to the centre
    assert Observations(path).night(local(7, 12, 0)) == (None, 40.0)  # no reading last night: no minimum


def test_a_night_seen_for_less_than_an_hour_does_not_count_and_a_damaged_file_starts_afresh(tmp_path):
    path = tmp_path / "pump-S-01.water.json"
    obs = Observations(path)
    feed(obs, local(1, 3, 20), local(1, 4, 30), 60, 10.0)          # 40 minutes of the window
    assert obs.night(local(1, 5, 0)) == (None, None)
    path.write_text('{"samples": [[1, "x", 3]], "nights": {"bad": {"min": 1}, "2026-10-01": {"min": -1, "first": 0, '
                    '"last": 9999}}}', encoding="utf-8")
    fresh = Observations(path)
    assert fresh.samples == [] and fresh.nights == {}
    path.write_text("not json", encoding="utf-8")
    assert Observations(path).nights == {}


def test_the_station_reports_what_it_works_out(tmp_path):
    rig = Rig(tmp_path, t=local(1, 1, 0))                          # observations in memory: no file to write
    for _ in range(5 * 24 * 60):                                   # five days, a reading a minute
        rig.st.poll()
        rig.clock.t += 60
    water = rig.st.report()["water"]
    flow = EmulatedStation().flow_at(rig.clock.t)
    assert water["kwh_per_m3"] == pytest.approx(0.3 + 2.0 / flow, abs=0.02)  # 0.3 kWh/m3 and the station's own load
    assert 35 < water["night_min_flow"] < 45 and water["night_baseline"] == pytest.approx(water["night_min_flow"], abs=1)
    assert validate_report(water, "S-01") is not None


# ---------------------------------------------------------------- the session, end to end, with a stand-in centre

class Hub:
    """A local stand-in for the platform's device listener: challenge, hello check, welcome, reports in, commands out."""

    def __init__(self, gateway, device_public, device="pump-S-01"):
        self.gateway, self.public, self.device = gateway, device_public, device
        self.reports, self.sessions, self.ws, self.server = [], 0, None, None

    async def handler(self, ws):
        await ws.send(json.dumps({"type": "challenge", "nonce": "abc123"}))
        hello = json.loads(await ws.recv())
        if not crypto.verify(self.public, {"device": self.device, "nonce": "abc123"}, hello["sig"]):
            await ws.close(code=4401)
            return
        self.sessions += 1
        await ws.send(json.dumps({"type": "welcome", "t": 0, "tls": None, "site": "S-01"}))
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

    def command(self, body, seq):
        env = {"device": self.device, "seq": seq, "t": int(time.time()), "body": body}
        return json.dumps({"type": "cmd", **env, "sig": crypto.sign(self.gateway, env)})

    async def until(self, test, timeout=8.0, after=0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = next((r for r in reversed(self.reports[after:]) if test(r)), None)
            if found is not None:
                return found
            await asyncio.sleep(0.05)
        raise AssertionError(f"no such report in {timeout} s; last: {self.reports[-1:]}")


class Switchable:
    """The emulated station, with a cable that can be pulled."""

    def __init__(self):
        self.inner, self.cut = EmulatedStation(), False

    def read(self, now):
        if self.cut:
            raise ModbusError("modbus tcp 10.0.0.5:502: timed out")
        return self.inner.read(now)

    def close(self):
        pass


def test_the_session_reports_keeps_its_link_when_the_automation_is_lost_and_refuses_commands(tmp_path):
    gateway, key = crypto.new_key(), crypto.new_key()
    status = tmp_path / "run" / "water" / "water.json"
    adapter = Switchable()
    st = WaterStation("pump-S-01", "S-01", key, crypto.load_public(crypto.public_hex(gateway)), adapter,
                      state_path=tmp_path / "seq", observations=Observations(tmp_path / "pump-S-01.water.json"),
                      lon=71.78, lat=40.39)
    logs = []

    async def scenario():
        hub = Hub(gateway, crypto.load_public(crypto.public_hex(key)))
        port = await hub.start()
        session = StationSession(st, f"ws://127.0.0.1:{port}", None, None, status=status, report_every=0.2,
                                 poll_every=0.05, log=lambda *a, **k: logs.append(a))
        runner = asyncio.ensure_future(session.run())
        try:
            first = await hub.until(lambda r: r.get("type") == "report")
            assert first["water"]["site"] == "S-01" and first["water"]["flow"] is not None  # read before the first report
            assert first["water"]["source_error"] is None and hub.sessions == 1
            adapter.cut = True
            lost = await hub.until(lambda r: r["water"]["source_error"] is not None)
            assert lost["water"]["source_error"] == "modbus tcp 10.0.0.5:502: timed out" and lost["water"]["flow"] is None
            seq = int(time.time() * 1000)
            await hub.ws.send(hub.command({"water": {"site": "S-01", "action": "start", "pump": 1}}, seq))
            await hub.until(lambda r: r["water"]["refused"] == READ_ONLY)
            await asyncio.sleep(0.6)
            assert sum(1 for r in hub.reports if r["water"]["refused"] == READ_ONLY) == 1      # once
            state = json.loads(status.read_text(encoding="utf-8"))
            assert state["unit"] == "pump-S-01" and state["link"] == "online" and state["site"] == "S-01"
            assert state["fault"] == "modbus tcp 10.0.0.5:502: timed out" and state["last_refused"] == READ_ONLY
            assert (state["lon"], state["lat"]) == (71.78, 40.39) and not list(status.parent.glob("*.tmp"))
            adapter.cut = False
            back = await hub.until(lambda r: r["water"]["source_error"] is None and r["water"]["flow"] is not None,
                                   after=len(hub.reports))
            assert back["water"]["pressure_out"] is not None and hub.sessions == 1      # the link was never dropped
            await hub.stop()
            if hub.ws is not None:
                await hub.ws.close()
            for _ in range(80):
                await asyncio.sleep(0.1)
                state = json.loads(status.read_text(encoding="utf-8"))
                if state["link"] == "offline":
                    break
            assert state["link"] == "offline" and state["flow"] is not None             # it reads on without the centre
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())
    assert any("online" in str(a) for a in logs)
    assert (tmp_path / "pump-S-01.water.json").exists()              # its observations were kept on the way out


def test_write_status_never_leaves_a_half_written_file(tmp_path):
    rig = Rig(tmp_path)
    rig.st.poll()
    path = tmp_path / "water.json"
    write_status(path, rig.st.report(), rig.st, False)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["link"] == "offline" and data["unit"] == "pump-S-01" and data["fault"] is None
    write_status(None, rig.st.report(), rig.st, True)                 # no --status: nothing


# ---------------------------------------------------------------- the program's options

def args(*argv):
    return water_main.parser().parse_args(["--unit", "pump-S-01", *argv])


def test_the_cli_takes_the_documented_options_and_an_empty_value_is_not_given():
    a = args("--platform", "c:8443", "--server-name", "c", "--pki", "/p", "--site", "S-01", "--adapter", "modbus-tcp",
             "--map", "/m.json", "--host", "10.0.0.5", "--port", "1502", "--slave", "3", "--serial", "", "--baud", "",
             "--lon", "71.78", "--lat", "40.39", "--status", "/run/urban/water/water.json", "--key", "", "--ca", "",
             "--gateway-pub", "", "--root", "", "--state", "")
    assert (a.adapter, a.map, a.host, a.port, a.slave, a.serial, a.baud, a.lon, a.key) == (
        "modbus-tcp", "/m.json", "10.0.0.5", 1502, 3, None, None, 71.78, None)
    with pytest.raises(SystemExit):
        args("--adapter", "modbus-write")


def test_a_modbus_adapter_needs_its_map_and_a_wrong_map_stops_the_start_with_its_message(tmp_path, capsys):
    with pytest.raises(SystemExit, match="needs --map"):
        water_main.make_adapter(args("--adapter", "modbus-tcp", "--host", "10.0.0.5"))
    bad = tmp_path / "map.json"
    bad.write_text('{"flow": {"table": "coil", "address": 1}}', encoding="utf-8")
    with pytest.raises(SystemExit, match="map: flow: table must be holding"):
        water_main.make_adapter(args("--adapter", "modbus-tcp", "--host", "10.0.0.5", "--map", str(bad)))
    with pytest.raises(SystemExit, match="needs --host"):
        water_main.make_adapter(args("--adapter", "modbus-tcp", "--map", str(TEMPLATE)))
    with pytest.raises(SystemExit, match="needs --serial"):
        water_main.make_adapter(args("--adapter", "modbus-rtu", "--map", str(TEMPLATE)))
    with pytest.raises(SystemExit, match="0..255"):
        water_main.make_adapter(args("--adapter", "modbus-tcp", "--map", str(TEMPLATE), "--host", "h", "--slave", "300"))
    tcp = water_main.make_adapter(args("--adapter", "modbus-tcp", "--map", str(TEMPLATE), "--host", "10.0.0.5"))
    assert isinstance(tcp, ModbusStation) and tcp.client.transport.address == ("10.0.0.5", 502)
    rtu = water_main.make_adapter(args("--adapter", "modbus-rtu", "--map", str(TEMPLATE), "--serial", "/dev/ttyUSB0"))
    assert rtu.client.transport.baud == 9600 and rtu.client.transport.unit == 1
    assert isinstance(water_main.make_adapter(args()), EmulatedStation)


def test_the_program_starts_on_a_kit_like_folder(tmp_path):
    from urban.devices.registry import provision
    provision([], tmp_path, demo_devices=False)
    (tmp_path / "edge" / "pump-S-01.key").write_text(crypto.private_hex(crypto.new_key()))
    runner = water_main.build(args("--pki", str(tmp_path), "--platform", "127.0.0.1:1", "--status", ""))
    assert runner.st.site == "S-01" and runner.status is None and runner.url == "wss://127.0.0.1:1/device"
    assert runner.st.obs.path == tmp_path / "edge" / "pump-S-01.water.json"
    with pytest.raises(SystemExit, match="--site is needed"):
        water_main.build(water_main.parser().parse_args(["--unit", "S-01", "--pki", str(tmp_path)]))


# ---------------------------------------------------------------- it cannot write: no code that can

FORBIDDEN = ("urban.equipment", "urban.modules.lighting")  # the relay boards' Modbus clients, the module that switches


def _imports(path: Path) -> list[str]:
    """Every module a file imports, relative imports resolved against its package."""
    package = "urban.modules.water"
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")[:len(package.split(".")) - node.level + 1]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.append(mod)
            out += [f"{mod}.{a.name}" for a in node.names]
    return out


def test_the_water_module_imports_nothing_that_can_write_to_equipment():
    files = sorted(WATER_DIR.rglob("*.py"))
    assert {f.name for f in files} >= {"__init__.py", "__main__.py", "device.py", "readonly.py", "regmap.py",
                                       "adapters.py", "observe.py", "session.py"}
    seen = {imp for f in files for imp in _imports(f)}
    assert "urban.modules.water.readonly" in seen and "urban.devices.crypto" in seen   # the resolver works
    bad = sorted(imp for imp in seen if imp.startswith(FORBIDDEN) or imp.split(".")[-1] in ("modbus_tcp", "modbus", "relay"))
    assert bad == []
    for f in files:  # and no name of a writing function anywhere in the code
        text = f.read_text(encoding="utf-8")
        assert not any(word in text for word in ("write_coil", "write_register", "WRITE_COILS", "WRITE_REGISTER"))


def test_loading_the_water_program_loads_no_modbus_or_relay_client():
    code = ("import sys, urban.modules.water, urban.modules.water.__main__; "
            "print(sorted(m for m in sys.modules if m.startswith('urban.equipment')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=WATER_DIR.parents[2],
                         timeout=60)
    assert out.returncode == 0, out.stderr
    loaded = set(json.loads(out.stdout.strip().replace("'", '"')))
    # only the abstract controller interface the shared envelope check lives beside (edge/device.py); none of them talks
    # to a device
    assert loaded <= {"urban.equipment", "urban.equipment.adapter", "urban.equipment.edge"}
    assert not loaded & {"urban.equipment.modbus_tcp", "urban.equipment.modbus", "urban.equipment.relay"}
