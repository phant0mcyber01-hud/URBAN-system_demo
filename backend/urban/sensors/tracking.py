"""Speed from position: short-lived tracks along one lane, nothing that identifies a vehicle.

A track is a position on the lane, a smoothed speed and the time it was last seen. Detections
are matched to the nearest track within a gate; a track nobody matched for a while is dropped.
Track ids are per lane counters that never leave the unit.
"""
from dataclasses import dataclass

GATE = 6.0        # metres a vehicle may move between frames beyond its predicted position
FORGET = 3.0      # seconds without a match before a track is dropped
SMOOTH = 0.5      # weight of the newest speed estimate
UNKNOWN = 2.5     # m/s assumed for a vehicle seen once: not standing until proven standing


@dataclass
class Track:
    x: float
    t: float
    speed: float | None = None
    length: float = 4.5

    def predicted(self, t: float) -> float:
        return self.x - (self.speed or 0.0) * (t - self.t)  # vehicles approach the stop line: x decreases


class LaneTracker:
    def __init__(self):
        self.tracks: dict[int, Track] = {}
        self._next = 0

    def update(self, t: float, detections: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
        """detections: (distance from the stop line, length). Returns (distance, speed, length) per vehicle."""
        free = dict(self.tracks)
        out = []
        for x, length in sorted(detections):
            best, best_d = None, GATE
            for tid, tr in free.items():
                d = abs(tr.predicted(t) - x)
                if d < best_d:
                    best, best_d = tid, d
            if best is None:
                self._next += 1
                self.tracks[self._next] = Track(x, t, None, length)
                out.append((x, UNKNOWN, length))
                continue
            tr = free.pop(best)
            dt = t - tr.t
            if dt > 1e-3:
                v = (tr.x - x) / dt
                tr.speed = v if tr.speed is None else (1 - SMOOTH) * tr.speed + SMOOTH * v
            tr.x, tr.t, tr.length = x, t, length
            out.append((x, abs(tr.speed) if tr.speed is not None else UNKNOWN, length))
        for tid, tr in list(self.tracks.items()):
            if t - tr.t > FORGET:
                del self.tracks[tid]
        return out
