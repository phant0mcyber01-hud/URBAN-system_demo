"""Queue analysis at junctions: who is short of green and who has green to spare.

Input is what the sensors report: the length of the standing queue on each approach, in
metres. Queue length swings with the signal cycle (it builds on red and discharges on
green), so decisions use the mean over WINDOW seconds, about one cycle. The demand of a
green phase is the longest queue among the approaches it serves: green time has to clear
the critical approach.

A problem is raised only when one phase is clearly overloaded AND another one has green to
spare, and only if the junction has an approved plan for exactly that shift. The answer is
the number of that plan, never a free-form timing.
"""
from collections import defaultdict, deque

WINDOW = 90            # seconds of history, about one signal cycle
LOADED_MIN = 40.0      # metres: mean queue before a phase counts as overloaded (about five cars)
LOADED_SHARE = 0.7     # ... or this share of the covered zone on short approaches
RATIO, MARGIN = 2.0, 15.0  # overloaded means: demand >= RATIO * spare demand + MARGIN metres
RELIEVED = 15.0        # metres: below this the queue is gone
SHIFT_MIN, SHIFT_MAX = 5, 15  # seconds of green worth moving


def shift_seconds(loaded: float, spare: float, green_loaded: float, green_spare: float) -> int:
    """Half of the way towards green shared in proportion to demand, within the sensible range."""
    total = green_loaded + green_spare
    target = total * loaded / (loaded + spare) if loaded + spare else green_loaded
    return max(SHIFT_MIN, min(SHIFT_MAX, round(0.5 * (target - green_loaded))))


def pick_plan(plans: list[dict], give: int, take: int, wanted: int) -> dict | None:
    """The approved plan moving green from `take` to `give`: the largest shift not above `wanted`,
    or the smallest one available."""
    fitting = sorted((pl for pl in plans if pl.get("give") == give and pl.get("take") == take),
                     key=lambda pl: pl["shift"])
    if not fitting:
        return None
    not_above = [pl for pl in fitting if pl["shift"] <= wanted]
    return not_above[-1] if not_above else fitting[0]


class QueueMonitor:
    def __init__(self, junctions: list[dict]):
        """junctions: [{id, camera, tls, name, approaches: [{id, name, zone}],
        phases: [{index, duration, edges, streets}], plans: [{id, give, take, shift}]}]"""
        self.junctions = junctions
        self._by_id = {j["id"]: j for j in junctions}
        self._hist: dict[tuple, deque] = defaultdict(lambda: deque(maxlen=WINDOW))
        self._sum: dict[tuple, float] = defaultdict(float)
        self.demands: dict[str, dict[int, float]] = {}  # junction id -> mean queue metres per phase
        self._loaded_min = {}
        for j in junctions:
            zone = {a["id"]: a.get("zone", 100.0) for a in j["approaches"]}
            for ph in j.get("phases", []):
                covered = max((zone.get(e, 100.0) for e in ph["edges"]), default=100.0)
                self._loaded_min[(j["id"], ph["index"])] = min(LOADED_MIN, LOADED_SHARE * covered)

    def demand(self, junction_id: str) -> dict[int, float]:
        """Mean queue metres per green phase over the window ({} until the window is full)."""
        j = self._by_id.get(junction_id)
        if j is None:
            return {}
        out = {}
        for ph in j.get("phases", []):
            key = (junction_id, ph["index"])
            if len(self._hist[key]) < WINDOW:
                return {}
            out[ph["index"]] = self._sum[key] / WINDOW
        return out

    def update(self, t: int, queues_m: dict[str, float]) -> tuple[dict, list[dict]]:
        """queues_m: {approach edge id: queue metres} as reported by the sensors this second."""
        queues, problems = {}, []
        for j in self.junctions:
            queues[j["id"]] = {a["id"]: queues_m.get(a["id"], 0.0) for a in j["approaches"]}
            for ph in j.get("phases", []):
                key = (j["id"], ph["index"])
                hist = self._hist[key]
                if len(hist) == WINDOW:
                    self._sum[key] -= hist[0]
                now = max((queues_m.get(e, 0.0) for e in ph["edges"]), default=0.0)
                hist.append(now)
                self._sum[key] += now

            demand = self.demands[j["id"]] = self.demand(j["id"])
            if len(demand) < 2:
                continue
            phases = {ph["index"]: ph for ph in j["phases"]}
            give = max(demand, key=demand.get)
            # the donor is the least loaded phase the engineer allowed to give green to this one
            donors = {pl["take"] for pl in j.get("plans", []) if pl.get("give") == give}
            if not donors or demand[give] < self._loaded_min[(j["id"], give)]:
                continue
            take = min(donors, key=demand.get)
            if demand[give] < RATIO * demand[take] + MARGIN:
                continue
            wanted = shift_seconds(demand[give], demand[take], phases[give]["duration"], phases[take]["duration"])
            plan = pick_plan(j["plans"], give, take, wanted)
            problems.append({
                "cam": j["id"], "camera": bool(j.get("camera")), "tls": j["tls"],
                "plan": plan["id"], "shift": plan["shift"], "give": give, "take": take,
                "street": ", ".join(phases[give]["streets"]) or j["name"],
                "from_street": ", ".join(phases[take]["streets"]) or j["name"],
                "queue": round(demand[give]), "other": round(demand[take]),
            })
        return queues, problems
