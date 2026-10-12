"""Traffic analytics over raw vehicle observations."""
import math
from collections import defaultdict

JAM_MIN_VEHICLES = 4
JAM_MAX_SPEED = 2.5   # m/s
CAMERA_RANGE = 70.0   # metres around the junction a camera watches


def jams(vehicles):
    """Road segments with a standing queue: enough vehicles and a low mean speed."""
    by_edge = defaultdict(list)
    for v in vehicles:
        if not v.edge.startswith(":"):
            by_edge[v.edge].append(v.speed)
    return [e for e, sp in by_edge.items() if len(sp) >= JAM_MIN_VEHICLES and sum(sp) / len(sp) < JAM_MAX_SPEED]


def stats(vehicles, bus_loss):
    n = len(vehicles)
    return {
        "cars": sum(1 for v in vehicles if v.kind == "car"),
        "buses": sum(1 for v in vehicles if v.kind == "bus"),
        "bus_avg_loss": round(sum(bus_loss) / len(bus_loss), 1) if bus_loss else 0,
        "mean_speed_kmh": round(3.6 * sum(v.speed for v in vehicles) / n, 1) if n else 0,
    }


def camera_counts(points, cameras):
    """Anonymous flow counters per camera: {camera id: [vehicles in view, mean speed km/h]}.

    points: [(x m, y m, speed m/s)], cameras: [(id, x m, y m)] in the same local frame.
    Only aggregates leave this function; no vehicle identity is kept.
    """
    out = {}
    for cam_id, cx, cy in cameras:
        speeds = [s for x, y, s in points if math.hypot(x - cx, y - cy) < CAMERA_RANGE]
        out[cam_id] = [len(speeds), round(3.6 * sum(speeds) / len(speeds)) if speeds else 0]
    return out
