"""From image pixels to metres on the road: a homography per camera view.

Calibration is four reference points measured once at installation: the two ends of the stop
line and two marks a known distance up the approach (lane paint, a pole, a tape measure).
Each has pixel coordinates (u, v) and road coordinates (x, y): x metres from the stop line
along the approach, y metres across it. Pure Python, no numpy on the unit.
"""


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise ValueError("degenerate calibration: reference points are collinear")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for k in range(col, n + 1):
                m[r][k] -= f * m[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


class Homography:
    """Maps pixel (u, v) to road (x, y) given four point pairs."""

    def __init__(self, pairs: list[tuple[tuple[float, float], tuple[float, float]]]):
        if len(pairs) != 4:
            raise ValueError("exactly four reference points are needed")
        rows, rhs = [], []
        for (u, v), (x, y) in pairs:
            rows.append([u, v, 1, 0, 0, 0, -u * x, -v * x])
            rhs.append(x)
            rows.append([0, 0, 0, u, v, 1, -u * y, -v * y])
            rhs.append(y)
        self.h = solve(rows, rhs) + [1.0]

    def road(self, u: float, v: float) -> tuple[float, float]:
        h = self.h
        w = h[6] * u + h[7] * v + h[8]
        if abs(w) < 1e-9:
            raise ValueError("point at the horizon")
        return (h[0] * u + h[1] * v + h[2]) / w, (h[3] * u + h[4] * v + h[5]) / w


class View:
    """One camera looking at one approach: calibration, lane layout and the covered zone."""

    def __init__(self, approach: str, points: list, lanes: int = 2, lane_width: float = 3.5, zone: float = 100.0):
        """points: four {"px": [u, v], "m": [x, y]} dicts; x from the stop line up the approach, y across it,
        lane 0 at y in [0, lane_width)."""
        self.approach, self.lanes, self.lane_width, self.zone = approach, lanes, lane_width, zone
        self.map = Homography([((p["px"][0], p["px"][1]), (p["m"][0], p["m"][1])) for p in points])

    def place(self, box: tuple[float, float, float, float]) -> tuple[int, float, float] | None:
        """A detection box (u, v, w, h) -> (lane, distance from the stop line, length) or None if off the road."""
        u, v, w, h = box
        near_x, y = self.map.road(u + w / 2, v + h)       # bottom centre: where the vehicle touches the road
        far_x, _ = self.map.road(u + w / 2, v + h * 0.4)  # roughly where its roof ends on the road plane
        lane = int(y // self.lane_width)
        if not 0 <= lane < self.lanes or near_x < -5 or near_x > self.zone + 30:
            return None
        length = min(15.0, max(4.0, abs(far_x - near_x)))
        return lane, max(0.0, min(near_x, far_x)), length
