"""What a pump station's unit reads: the station emulated in software, or its automation over Modbus (read only).

Both give read(now) -> {"pressure_in", "pressure_out", "flow", "power_kw", "energy_kwh", "level", "pumps", "door"}
in the units of docs/water-module.md (bar, m3/h, kW, kWh, m); a value the station does not have is None. A failure to
reach the automation raises OSError (ModbusError): the station then reports its source_error and null values.
"""
from __future__ import annotations

import math

from .readonly import (MAX_BITS, MAX_REGISTERS, READ_COILS, READ_DISCRETE, READ_HOLDING, READ_INPUT,
                       ReadOnlyModbus)
from .regmap import REPORT_NAME, RegisterMap, decode

LOCAL = 5 * 3600  # Fergana: UTC+5 all year


class EmulatedStation:
    """A station that is not there: for the demo and for checks. Two pumps, and a city's daily pattern: little water
    at night (a minimum near 03:00), peaks in the morning and the evening. The pressure falls a little as the flow
    rises, the power follows the flow, the energy counter adds it up. `fault_pump` (1-based) is a pump in fault: it
    does not run. `night_extra` adds m3/h around the clock (a leak, for checks)."""

    def __init__(self, pumps: int = 2, fault_pump: int | None = None, energy: float = 1_200_000.0,
                 night_extra: float = 0.0, door: str = "closed"):
        self.pumps, self.fault_pump, self.night_extra, self.door = max(1, min(16, pumps)), fault_pump, night_extra, door
        self.energy = float(energy)
        self._last: float | None = None

    def flow_at(self, now: float) -> float:
        hour = ((now + LOCAL) % 86400) / 3600.0
        base = 80.0 - 40.0 * math.cos(2 * math.pi * (hour - 3.0) / 24.0)  # 40 m3/h at 03:00, 120 at 15:00
        peaks = 45.0 * math.exp(-((hour - 8.0) ** 2) / 2.0) + 40.0 * math.exp(-((hour - 20.0) ** 2) / 3.0)
        return round(base + peaks + self.night_extra, 1)

    def read(self, now: float) -> dict:
        flow = self.flow_at(now)
        power = round(0.3 * flow + 2.0, 1)  # about 0.3 kWh per m3, and the station's own load
        if self._last is not None and 0 < now - self._last <= 3600:
            self.energy += power * (now - self._last) / 3600.0
        self._last = now
        # the first healthy pump always runs, the next healthy one joins above 120 m3/h; a pump in fault never runs
        healthy = [n for n in range(1, self.pumps + 1) if n != self.fault_pump]
        running = set(healthy[:2] if flow > 120.0 else healthy[:1])
        pumps = [{"run": n in running, "fault": n == self.fault_pump} for n in range(1, self.pumps + 1)]
        hour = ((now + LOCAL) % 86400) / 3600.0
        return {"pressure_in": 0.8, "pressure_out": round(4.6 - flow / 300.0, 2), "flow": flow, "power_kw": power,
                "energy_kwh": round(self.energy, 1), "level": round(3.1 + 0.3 * math.sin(2 * math.pi * hour / 24.0), 2),
                "pumps": pumps, "door": self.door}

    def close(self):
        pass


def _groups(points: list[tuple[str, int, int]], gap: int = 8) -> list[tuple[str, int, int]]:
    """Points of one table close together become one request (fewer round trips): (table, start, count)."""
    out: list[list] = []
    for table, address, count in sorted(points):
        limit = MAX_BITS if table in ("coil", "discrete") else MAX_REGISTERS
        last = out[-1] if out else None
        if (last is not None and last[0] == table and address <= last[1] + last[2] + gap
                and max(last[1] + last[2], address + count) - last[1] <= limit):
            last[2] = max(last[1] + last[2], address + count) - last[1]
        else:
            out.append([table, address, count])
    return [tuple(g) for g in out]


FUNCTION = {"coil": READ_COILS, "discrete": READ_DISCRETE, "holding": READ_HOLDING, "input": READ_INPUT}


class ModbusStation:
    """The station's automation (a PLC, a frequency drive, a meter) through ReadOnlyModbus and a register map."""

    def __init__(self, client: ReadOnlyModbus, regmap: RegisterMap):
        self.client, self.map = client, regmap
        self.requests = _groups(regmap.points())

    def read(self, now: float) -> dict:
        got: dict[tuple[str, int], object] = {}
        for table, start, count in self.requests:
            values = self.client.read(FUNCTION[table], start, count)
            for i, v in enumerate(values):
                got[(table, start + i)] = v
        out = {name: None for name in REPORT_NAME.values()}
        for name, reg in self.map.values.items():
            words = [got[(reg.table, reg.address + i)] for i in range(reg.count)]
            out[REPORT_NAME[name]] = decode(words, reg)

        def bit(b):
            return None if b is None else bool(got[(b.table, b.address)])

        door = self.map.door
        out["door"] = "unknown" if door is None else ("open" if bit(door) == door.true_means else "closed")
        out["pumps"] = [{"run": bit(run), "fault": bit(fault)} for run, fault in self.map.pumps]
        return out

    def close(self):
        self.client.close()
