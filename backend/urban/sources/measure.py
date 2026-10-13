"""Emulated queue sensor: what a camera with on-site analysis would report for an approach.

A real sensor cannot count overlapping vehicles far from the camera, but it does see where
the standing queue ends (see docs/hardware.md, section 5.1). So the measurement is the
length of the queue in metres, per approach, from the stop line back to the tail, capped by
the zone the sensor covers. Here it is derived from simulation ground truth; no vehicle
identity leaves this function.
"""
from collections import defaultdict

ZONE = 100.0       # metres of the approach a sensor covers, from the stop line back
STANDING = 2.0     # m/s: slower than this counts as standing
MAX_GAP = 15.0     # metres between standing vehicles that still belong to one queue
START_MAX = 25.0   # a lone stopped vehicle farther than this from the stop line is not a queue


def lane_queue(vehicles: list[tuple[float, float, float]], zone: float) -> float:
    """vehicles: (distance from the stop line to the front bumper, speed, length) on one lane."""
    # look a little past the zone, so a queue running out of view reads as a full zone
    seen = sorted(v for v in vehicles if v[0] <= zone + MAX_GAP)
    first = next((i for i, v in enumerate(seen) if v[1] < STANDING), None)
    if first is None:
        return 0.0
    # nothing between it and the stop line, yet it stands far back: a bus stop or a parked car, not a queue
    if first == 0 and seen[0][0] > START_MAX:
        return 0.0
    tail = seen[first]
    for v in seen[first + 1:]:
        if v[1] >= STANDING:
            continue
        if v[0] - (tail[0] + tail[2]) > MAX_GAP:
            break
        tail = v
    return min(zone, tail[0] + tail[2])


def queue_lengths(samples, zones: dict[str, float] | None = None) -> dict[str, float]:
    """samples: (lane id, distance to stop line, speed, length). Returns {edge id: queue metres}.

    The longest lane queue stands for the approach. zones: edge id -> covered metres (default ZONE).
    """
    lanes = defaultdict(list)
    for lane, dist, speed, length in samples:
        if not lane.startswith(":"):
            lanes[lane].append((dist, speed, length))
    out: dict[str, float] = {}
    for lane, vehicles in lanes.items():
        edge = lane.rsplit("_", 1)[0]
        q = lane_queue(vehicles, (zones or {}).get(edge, ZONE))
        if q > out.get(edge, 0.0):
            out[edge] = round(q, 1)
    return out
