"""Queue analysis at camera junctions: who is short of green and who has green to spare.

A camera reports only aggregates: how many vehicles are standing on each approach.
Standing counts swing with the signal cycle (queues build on red and discharge on green),
so decisions use the mean over WINDOW seconds, about one cycle. For every green phase the
demand is the mean number of vehicles standing on the approaches it serves.

A problem is raised only when one phase is clearly overloaded AND another one has green to
spare. If every direction is queued there is nothing to move, and nothing is proposed.
No vehicle identity leaves this module.
"""
from collections import defaultdict, deque

STANDING_SPEED = 2.0  # m/s: slower than this counts as standing
WINDOW = 90           # seconds of history, about one signal cycle
LOADED_MIN = 5.0      # mean standing vehicles before a phase counts as overloaded
RATIO, MARGIN = 2.0, 2.0  # overloaded means: demand >= RATIO * spare demand + MARGIN
RELIEVED = 2.0        # demand below this: the queue is gone
SHIFT_MIN, SHIFT_MAX = 5, 15  # seconds of green that may be moved
DONOR_MIN_GREEN = 15  # seconds a controller keeps for any phase (sources/signals.MIN_GREEN): never ask for more


def shift_seconds(loaded: float, spare: float, green_loaded: float, green_spare: float) -> int:
    """Half of the way towards green shared in proportion to demand, within what may be moved.

    Returns 0 when the donor phase cannot give at least SHIFT_MIN without dropping below DONOR_MIN_GREEN.
    """
    available = min(SHIFT_MAX, green_spare - DONOR_MIN_GREEN)
    if available < SHIFT_MIN:
        return 0
    total = green_loaded + green_spare
    target = total * loaded / (loaded + spare) if loaded + spare else green_loaded
    return max(SHIFT_MIN, min(available, round(0.5 * (target - green_loaded))))


class QueueMonitor:
    def __init__(self, cameras: list[dict]):
        """cameras: [{id, tls, name, approaches: [{id, name}], phases: [{index, duration, edges, streets}]}]"""
        self.cameras = cameras
        self._by_id = {cam["id"]: cam for cam in cameras}
        self._hist: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        self._sum: dict[tuple, float] = defaultdict(float)
        self.demands: dict[str, dict[int, float]] = {}  # camera id -> demand per phase, refreshed by update()

    def demand(self, cam_id: str) -> dict[int, float]:
        """Mean standing vehicles per green phase over the window ({} until the window is full)."""
        cam = self._by_id.get(cam_id)
        if cam is None:
            return {}
        out = {}
        for ph in cam.get("phases", []):
            key = (cam_id, ph["index"])
            if len(self._hist[key]) < WINDOW:
                return {}
            out[ph["index"]] = self._sum[key] / WINDOW
        return out

    def update(self, t: int, vehicles) -> tuple[dict, list[dict]]:
        standing = defaultdict(int)
        for v in vehicles:
            if v.speed < STANDING_SPEED and not v.edge.startswith(":"):
                standing[v.edge] += 1

        queues, problems = {}, []
        for cam in self.cameras:
            queues[cam["id"]] = {a["id"]: standing.get(a["id"], 0) for a in cam["approaches"]}
            for ph in cam.get("phases", []):
                key = (cam["id"], ph["index"])
                hist = self._hist[key]
                if len(hist) == WINDOW:
                    self._sum[key] -= hist[0]
                now = sum(standing.get(e, 0) for e in ph["edges"])
                hist.append(now)
                self._sum[key] += now

            demand = self.demands[cam["id"]] = self.demand(cam["id"])
            if len(demand) < 2:
                continue
            phases = {ph["index"]: ph for ph in cam["phases"]}
            give = max(demand, key=demand.get)
            # the donor is the least loaded phase that still has green to give
            donors = [i for i in demand if i != give and phases[i]["duration"] >= DONOR_MIN_GREEN + SHIFT_MIN]
            if not donors:
                continue
            take = min(donors, key=demand.get)
            seconds = shift_seconds(demand[give], demand[take], phases[give]["duration"], phases[take]["duration"])
            if seconds and demand[give] >= LOADED_MIN and demand[give] >= RATIO * demand[take] + MARGIN:
                problems.append({
                    "cam": cam["id"], "camera": bool(cam.get("camera")), "tls": cam["tls"],
                    "give": give, "take": take, "seconds": seconds,
                    "street": ", ".join(phases[give]["streets"]) or cam["name"],
                    "from_street": ", ".join(phases[take]["streets"]) or cam["name"],
                    "queue": round(demand[give], 1), "other": round(demand[take], 1),
                })
        return queues, problems
