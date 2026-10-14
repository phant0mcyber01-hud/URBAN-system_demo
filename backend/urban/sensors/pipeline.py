"""Detections per camera frame -> queue metres per approach -> queues.json for the Edge unit."""
import json
import os
import time
from pathlib import Path

from ..sources.measure import lane_queue
from .geometry import View
from .tracking import LaneTracker

STALE = 5.0  # seconds without a frame before an approach reports no data


class QueuePipeline:
    def __init__(self, views: list[View]):
        self.views = {v.approach: v for v in views}
        self.trackers: dict[tuple[str, int], LaneTracker] = {}
        self.latest: dict[str, tuple[float, float]] = {}  # approach -> (frame time, queue metres)

    def feed(self, approach: str, t: float, boxes: list) -> float | None:
        """One frame of one camera. Returns the queue length of that approach now."""
        view = self.views.get(approach)
        if view is None:
            return None
        per_lane: dict[int, list[tuple[float, float]]] = {}
        for box in boxes:
            placed = view.place(tuple(box))
            if placed is not None:
                lane, x, length = placed
                per_lane.setdefault(lane, []).append((x, length))
        longest = 0.0
        for lane in range(view.lanes):
            tracker = self.trackers.setdefault((approach, lane), LaneTracker())
            vehicles = tracker.update(t, per_lane.get(lane, []))
            longest = max(longest, lane_queue(vehicles, view.zone))
        longest = round(longest, 1)
        self.latest[approach] = (t, longest)
        return longest

    def queues(self, now: float) -> dict[str, float]:
        """What the unit reports: only approaches with a fresh frame."""
        return {a: q for a, (t, q) in self.latest.items() if now - t <= STALE}

    def write(self, path: Path, now: float | None = None):
        """Atomic rewrite, so the unit never reads a half-written file."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.queues(time.time() if now is None else now)), encoding="utf-8")
        os.replace(tmp, path)


def load_views(path: Path) -> list[View]:
    """Calibration file: [{"approach": id, "points": [...4...], "lanes": 2, "lane_width": 3.5, "zone": 100}, ...]"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [View(v["approach"], v["points"], v.get("lanes", 2), v.get("lane_width", 3.5), v.get("zone", 100.0)) for v in data]
