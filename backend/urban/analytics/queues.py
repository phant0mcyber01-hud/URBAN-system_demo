"""Queue detection on camera approaches.

A virtual camera watches the approaches of one signalised junction. It reports only
aggregates: how many vehicles are standing on each approach. A problem is raised when a
queue of at least QUEUE_MIN vehicles has been standing for HOLD seconds. No vehicle identity
leaves this module.
"""
from collections import defaultdict

STANDING_SPEED = 2.0  # m/s: slower than this counts as standing
QUEUE_MIN = 6
HOLD = 15             # seconds the queue must persist
RESET_BELOW = 3       # queue must drop below this before the same approach can raise a new problem


class QueueMonitor:
    def __init__(self, cameras: list[dict]):
        """cameras: [{id, tls, name, approaches: [{id, name}]}]"""
        self.cameras = cameras
        self._since: dict[tuple, int] = {}
        self._raised: set[tuple] = set()

    def update(self, t: int, vehicles) -> tuple[dict, list[dict]]:
        standing = defaultdict(int)
        for v in vehicles:
            if v.speed < STANDING_SPEED and not v.edge.startswith(":"):
                standing[v.edge] += 1
        queues, problems = {}, []
        for cam in self.cameras:
            per = {a["id"]: standing.get(a["id"], 0) for a in cam["approaches"]}
            queues[cam["id"]] = per
            for a in cam["approaches"]:
                key, q = (cam["id"], a["id"]), per[a["id"]]
                if q >= QUEUE_MIN:
                    first = self._since.setdefault(key, t)
                    if t - first >= HOLD and key not in self._raised:
                        self._raised.add(key)
                        problems.append({"cam": cam["id"], "tls": cam["tls"], "approach": a["id"],
                                         "street": a["name"] or cam["name"], "queue": q})
                else:
                    self._since.pop(key, None)
                    if q < RESET_BELOW:
                        self._raised.discard(key)
        return queues, problems
