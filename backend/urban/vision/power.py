"""Vision under the machine's power plan (urban.node.power writes it to /run/urban/power.json).

The plan gives a frame rate per camera and a thread count. Vision never analyses more frames than that: a hot
unit gets fewer, a paused one none (its approaches then go stale and the platform shows "no data", which is the
truth). Without a plan - no power manager, or it has not run yet - vision works at its own settings.
Vision reports back how many cameras it serves and how long a frame takes, so the plan can fit the work.
"""
import json
import statistics
import time
from collections import deque
from pathlib import Path


class PowerPlan:
    """The vision part of the plan, re-read when the file changes."""

    def __init__(self, path: Path | None, fps: float):
        self.path, self.default_fps = path, fps
        self.mtime, self.vision = None, {}

    def refresh(self):
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self.mtime:
                self.vision = json.loads(self.path.read_text()).get("vision", {}) or {}
                self.mtime = mtime
        except (OSError, ValueError, AttributeError):
            self.vision, self.mtime = {}, None

    @property
    def fps(self) -> float:
        """Frames per second per camera; never above what vision itself was started with."""
        fps = self.vision.get("fps")
        return self.default_fps if not isinstance(fps, (int, float)) else max(0.0, min(float(fps), self.default_fps))

    def threads(self, given: int | None, fallback: int = 2) -> int:
        if given:
            return given
        t = self.vision.get("threads")
        return t if isinstance(t, int) and t > 0 else fallback


class Throttle:
    """At most `fps` analysed frames per camera; none while the rate is 0."""

    def __init__(self):
        self.last: dict[str, float] = {}

    def due(self, camera: str, now: float, fps: float) -> bool:
        """May this camera's next frame be analysed now?"""
        return fps > 0 and now - self.last.get(camera, float("-inf")) >= 1.0 / fps - 1e-6

    def mark(self, camera: str, now: float):
        """A frame of this camera was analysed."""
        self.last[camera] = now


class Stats:
    """How long the detector takes per frame, for the power manager."""

    def __init__(self, path: Path | None):
        self.path, self.ms, self.written = path, deque(maxlen=50), float("-inf")

    def add(self, ms: float):
        self.ms.append(ms)

    def write(self, cameras: int, fps: float, now: float | None = None):
        """At most every 5 s; `now` is a monotonic time (a step of the wall clock must not stop it for hours)."""
        now = time.monotonic() if now is None else now
        if self.path is None or now - self.written < 5.0:
            return
        self.written = now
        body = {"cameras": cameras, "fps": fps, "ms_per_frame": round(statistics.median(self.ms), 1) if self.ms else None}
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body))
            tmp.replace(self.path)
        except OSError:
            pass
