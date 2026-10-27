"""What a pump station's unit works out by itself, kept in <pki>/edge/<unit>.water.json (the data partition: it
survives a restart and an update).

- kwh_per_m3: energy per cubic metre over the last hour, from the rise of the energy counter and the volume pumped
  (the flow integrated over time). Only stretches where consecutive readings are at most GAP seconds apart count, for
  both the energy and the volume, so a pause in reading never puts energy against no water. None until the hour holds
  at least 1 m3, or without a counter or a flow reading.
- night_min_flow: the lowest flow between 02:00 and 04:00 Fergana time (UTC+5, no daylight saving) of last night,
  m3/h. A night counts only if the readings cover at least an hour of the window.
- night_baseline: the median of the minimums of up to 7 nights before last night; None while there are fewer than 3.
  Little water is taken at night, so a night minimum that grows against the baseline is the first sign of a leak.

The file is written whole (a new file moved over the old one) at most every SAVE_EVERY seconds and on close().
"""
from __future__ import annotations

import datetime
import json
import math
import os
from pathlib import Path
from statistics import median

LOCAL = 5 * 3600            # Fergana: UTC+5 all year
NIGHT = (2 * 3600, 4 * 3600)  # the night window, seconds after local midnight
NIGHT_COVER = 3600          # seconds of the window a night's readings must span to count
SAMPLE_EVERY = 10           # seconds between the samples kept for the hour
GAP = 60                    # seconds: two samples further apart do not make a stretch of the hour
HOUR = 3600
MIN_VOLUME = 1.0            # m3 the hour must hold before kwh_per_m3 means anything
NIGHTS_KEPT, BASELINE_NIGHTS, BASELINE_MIN = 10, 7, 3
SAVE_EVERY = 60


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def local_day(t: float) -> str:
    return datetime.datetime.fromtimestamp(t + LOCAL, datetime.timezone.utc).date().isoformat()


def _seconds_into_day(t: float) -> float:
    return (t + LOCAL) % 86400


class Observations:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self.samples: list[list[float]] = []  # [t, energy_kwh, flow] for the last hour, at most every SAMPLE_EVERY s
        self.nights: dict[str, dict] = {}     # local date -> {"min", "first", "last"} of its 02:00-04:00 window
        self._saved_at: float | None = None
        self._dirty = False
        self.load()

    # ---- the file
    def load(self):
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # none yet, or damaged: start afresh (the values come back within an hour and a few nights)
        if not isinstance(data, dict):
            return
        samples = data.get("samples")
        if isinstance(samples, list):
            self.samples = [[float(s[0]), float(s[1]), float(s[2])] for s in samples
                            if isinstance(s, list) and len(s) == 3 and all(_num(x) for x in s)][-HOUR:]
            self.samples.sort(key=lambda s: s[0])
        nights = data.get("nights")
        if isinstance(nights, dict):
            for day, n in nights.items():
                try:
                    datetime.date.fromisoformat(day)
                except (TypeError, ValueError):
                    continue
                if isinstance(n, dict) and all(_num(n.get(k)) for k in ("min", "first", "last")) and n["min"] >= 0:
                    self.nights[day] = {k: float(n[k]) for k in ("min", "first", "last")}
            self._trim_nights()

    def save(self, now: float | None = None):
        if self.path is None:
            return
        data = {"v": 1, "samples": self.samples, "nights": self.nights}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
            self._saved_at = now
        except OSError as err:
            print(f"water: cannot write {self.path}: {err}", flush=True)  # the readings go on; the journal says why

    def close(self):
        if self._dirty:
            self.save()

    # ---- readings
    def observe(self, now: float, flow, energy) -> None:
        """One reading of the station (flow m3/h, energy counter kWh; either may be None)."""
        if not _num(now):
            return
        if self.samples and now < self.samples[-1][0]:
            self.samples = []  # the clock went back: an hour that cannot be trusted
        if _num(flow) and flow >= 0:
            into = _seconds_into_day(now)
            if NIGHT[0] <= into < NIGHT[1]:
                day = local_day(now)
                n = self.nights.get(day)
                if n is None:
                    self.nights[day] = {"min": float(flow), "first": now, "last": now}
                    self._trim_nights()
                else:
                    n["min"], n["last"] = min(n["min"], float(flow)), max(n["last"], now)
                self._dirty = True
            if _num(energy) and energy >= 0 and (not self.samples or now - self.samples[-1][0] >= SAMPLE_EVERY):
                self.samples.append([float(now), float(energy), float(flow)])
                self._dirty = True
        self.samples = [s for s in self.samples if now - s[0] <= HOUR]
        if self._dirty and (self._saved_at is None or now - self._saved_at >= SAVE_EVERY):
            self.save(now)

    def _trim_nights(self):
        for day in sorted(self.nights)[:-NIGHTS_KEPT]:
            del self.nights[day]

    # ---- what goes up
    def kwh_per_m3(self) -> float | None:
        energy = volume = 0.0
        for (t0, e0, f0), (t1, e1, f1) in zip(self.samples, self.samples[1:]):
            if 0 < t1 - t0 <= GAP and e1 >= e0:  # a counter that went back (replaced, reset) is not a stretch
                energy += e1 - e0
                volume += (f0 + f1) / 2.0 * (t1 - t0) / 3600.0
        if volume < MIN_VOLUME:
            return None
        value = energy / volume
        return round(value, 3) if 0 <= value <= 100 else None

    def _complete_nights(self, now: float) -> list[str]:
        """Dates whose night window is over and that the readings covered, oldest first."""
        today, into = local_day(now), _seconds_into_day(now)
        return [day for day, n in sorted(self.nights.items())
                if (day < today or (day == today and into >= NIGHT[1])) and n["last"] - n["first"] >= NIGHT_COVER]

    def night(self, now: float) -> tuple[float | None, float | None]:
        """(night_min_flow, night_baseline) as of `now`."""
        nights = self._complete_nights(now)
        # last night: this morning's window once it is over (after 04:00), else yesterday's
        last_night = local_day(now) if _seconds_into_day(now) >= NIGHT[1] else local_day(now - 86400)
        minimum = self.nights[last_night]["min"] if last_night in nights else None
        earlier = [self.nights[d]["min"] for d in nights if d < last_night][-BASELINE_NIGHTS:]
        baseline = round(float(median(earlier)), 2) if len(earlier) >= BASELINE_MIN else None
        return (round(minimum, 2) if minimum is not None else None), baseline
