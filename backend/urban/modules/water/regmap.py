"""The register map of a pump station's automation: which Modbus table and address holds each reading, how to decode
it, and the limits the centre accepts for each value (docs/water-module.md). No I/O here.

A map is JSON (map-template.json beside this file). Every field is optional: a reading that is not in the map is null
("unknown"), never made up. The whole map is checked at start; a wrong one is an error with a clear message, not a
station that reads the wrong registers quietly.

    {"pressure_out": {"table": "input", "address": 1, "type": "u16", "scale": 0.01},
     "energy": {"table": "holding", "address": 10, "type": "u32", "scale": 1, "word_order": "big"},
     "door": {"table": "discrete", "address": 0, "open": true},
     "pumps": [{"run": {"table": "discrete", "address": 8}, "fault": {"table": "discrete", "address": 9}}]}
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path

# table -> the Modbus function that reads it; there are no others (a write function has no table here at all)
TABLES = {"coil": 1, "discrete": 2, "holding": 3, "input": 4}
REGISTER_TABLES, BIT_TABLES = ("holding", "input"), ("coil", "discrete")
TYPES = {"u16": 1, "i16": 1, "u32": 2, "i32": 2, "f32": 2}  # type -> registers it takes
WORD_ORDERS = ("big", "little")
VALUES = ("pressure_in", "pressure_out", "flow", "power", "energy", "level")  # register readings, by map name
MAX_PUMPS = 16
MAX_ADDRESS = 0xFFFF

# what the centre accepts in a report (urban/modules/water/__init__.py validate_report), by report name; the station
# reports null rather than a value outside these, so that one odd register never costs the whole report
LIMITS = {"pressure_in": (-1.0, 100.0), "pressure_out": (-1.0, 100.0), "flow": (0.0, 100_000.0),
          "power_kw": (0.0, 100_000.0), "energy_kwh": (0.0, 1e13), "level": (-100.0, 1000.0),
          "kwh_per_m3": (0.0, 100.0), "night_min_flow": (0.0, 100_000.0), "night_baseline": (0.0, 100_000.0)}
REPORT_NAME = {"pressure_in": "pressure_in", "pressure_out": "pressure_out", "flow": "flow", "power": "power_kw",
               "energy": "energy_kwh", "level": "level"}  # map name -> report name


@dataclass(frozen=True)
class Register:
    table: str          # holding | input
    address: int
    type: str = "u16"
    scale: float = 1.0
    word_order: str = "big"

    @property
    def count(self) -> int:
        return TYPES[self.type]


@dataclass(frozen=True)
class Bit:
    table: str          # coil | discrete
    address: int
    true_means: bool = True  # door: which bit value means "open"


@dataclass(frozen=True)
class RegisterMap:
    values: dict           # map name -> Register
    door: Bit | None
    pumps: tuple           # (run Bit | None, fault Bit | None) per pump

    def points(self) -> list[tuple[str, int, int]]:
        """Every (table, address, count) the station reads, for grouping into requests."""
        out = [(r.table, r.address, r.count) for r in self.values.values()]
        bits = [self.door] + [b for pump in self.pumps for b in pump]
        out += [(b.table, b.address, 1) for b in bits if b is not None]
        return out


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _address(where: str, spec: dict, count: int) -> int:
    address = spec.get("address")
    if not _int(address) or not 0 <= address <= MAX_ADDRESS - count + 1:
        raise ValueError(f"map: {where}: address must be a whole number 0..{MAX_ADDRESS - count + 1}")
    return address


def _keys(where: str, spec, allowed: tuple[str, ...]) -> dict:
    if not isinstance(spec, dict):
        raise ValueError(f"map: {where}: must be an object like {{\"table\": \"input\", \"address\": 0}}")
    extra = sorted(set(spec) - set(allowed))
    if extra:
        raise ValueError(f"map: {where}: unknown key {extra[0]!r} (allowed: {', '.join(allowed)})")
    return spec


def _register(where: str, spec) -> Register:
    spec = _keys(where, spec, ("table", "address", "type", "scale", "word_order"))
    table = spec.get("table")
    if table not in REGISTER_TABLES:
        raise ValueError(f"map: {where}: table must be holding (function 03) or input (04), not {table!r}")
    kind = spec.get("type", "u16")
    if kind not in TYPES:
        raise ValueError(f"map: {where}: type must be one of {', '.join(TYPES)}, not {kind!r}")
    scale = spec.get("scale", 1)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale == 0:
        raise ValueError(f"map: {where}: scale must be a finite number other than 0")
    order = spec.get("word_order", "big")
    if order not in WORD_ORDERS:
        raise ValueError(f"map: {where}: word_order must be big or little, not {order!r}")
    return Register(table, _address(where, spec, TYPES[kind]), kind, float(scale), order)


def _bit(where: str, spec, door: bool = False) -> Bit:
    spec = _keys(where, spec, ("table", "address", "open") if door else ("table", "address"))
    table = spec.get("table")
    if table not in BIT_TABLES:
        raise ValueError(f"map: {where}: table must be coil (function 01) or discrete (02), not {table!r}")
    means = spec.get("open", True)
    if not isinstance(means, bool):
        raise ValueError(f"map: {where}: open must be true or false")
    return Bit(table, _address(where, spec, 1), means)


def parse_map(data) -> RegisterMap:
    """The map from its JSON value; ValueError naming the field and what is wrong."""
    if not isinstance(data, dict):
        raise ValueError("map: must be a JSON object with fields such as pressure_out, flow, door, pumps")
    extra = sorted(set(data) - set(VALUES) - {"door", "pumps"})
    if extra:
        raise ValueError(f"map: unknown field {extra[0]!r} (fields: {', '.join(VALUES)}, door, pumps)")
    values = {name: _register(name, data[name]) for name in VALUES if data.get(name) is not None}
    door = _bit("door", data["door"], door=True) if data.get("door") is not None else None
    pumps_raw = data.get("pumps") or []
    if not isinstance(pumps_raw, list):
        raise ValueError("map: pumps must be a list of {\"run\": ..., \"fault\": ...}")
    if len(pumps_raw) > MAX_PUMPS:
        raise ValueError(f"map: at most {MAX_PUMPS} pumps, the map has {len(pumps_raw)}")
    pumps = []
    for n, pump in enumerate(pumps_raw, start=1):
        pump = _keys(f"pumps[{n}]", pump, ("run", "fault"))
        pumps.append(tuple(_bit(f"pumps[{n}].{part}", pump[part]) if pump.get(part) is not None else None
                           for part in ("run", "fault")))
    return RegisterMap(values, door, tuple(pumps))


def load_map(path) -> RegisterMap:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as err:
        raise ValueError(f"map: cannot read {path}: {err.strerror or err}")
    except ValueError as err:
        raise ValueError(f"map: {path} is not JSON: {err}")
    return parse_map(data)


def decode(words: list[int], reg: Register) -> float | None:
    """The value of a register reading, scaled; None for a float that is not a number."""
    if len(words) != reg.count or any(not _int(w) or not 0 <= w <= 0xFFFF for w in words):
        raise ValueError(f"{reg.type} needs {reg.count} register(s) of 0..65535")
    if reg.count == 2 and reg.word_order == "little":
        words = [words[1], words[0]]  # the low word first on the wire
    raw = struct.pack(f">{reg.count}H", *words)
    value = struct.unpack({"u16": ">H", "i16": ">h", "u32": ">I", "i32": ">i", "f32": ">f"}[reg.type], raw)[0]
    if not math.isfinite(value):
        return None
    out = value * reg.scale
    return out if math.isfinite(out) else None


def within(name: str, value) -> float | None:
    """A report value as the centre will accept it, or None: finite and inside LIMITS[name]."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    lo, hi = LIMITS[name]
    return float(value) if lo <= value <= hi else None
