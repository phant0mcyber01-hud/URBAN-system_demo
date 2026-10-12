"""Synthetic utility telemetry (stands in for SCADA / metering feeds).

Asset positions and readings are generated; two assets degrade on a script.
"""
import math
import random

# kind -> (name prefix, metric, unit, base value, noise sigma, alarm limit)
KINDS = {
    "power": ("ТП", "нагрузка", "%", 58.0, 1.6, 95.0),
    "water": ("НС", "вибрация насоса", "мм/с", 1.8, 0.07, 4.5),
    "heat": ("ЦТП", "температура подачи", "°C", 72.0, 0.6, 95.0),
}
LAYOUT = ["power"] * 5 + ["water"] * 4 + ["heat"] * 3
# asset id -> (start second, drift per second): the scripted degradations
DRIFT = {"НС-2": (90, 0.011), "ТП-3": (300, 0.06)}


class Telemetry:
    def __init__(self, center, radius):
        rnd = random.Random(11)
        self.rnd = random.Random(12)
        m_lat = 111320
        m_lon = m_lat * math.cos(math.radians(center[1]))
        counters, self.assets = {}, []
        for i, kind in enumerate(LAYOUT):
            counters[kind] = counters.get(kind, 0) + 1
            ang, r = math.radians(i * 137.5), radius * (0.2 + 0.42 * rnd.random())
            prefix, metric, unit, base, sigma, limit = KINDS[kind]
            self.assets.append({
                "id": f"{prefix}-{counters[kind]}", "kind": kind, "metric": metric, "unit": unit, "limit": limit,
                "lon": round(center[0] + math.cos(ang) * r / m_lon, 6),
                "lat": round(center[1] + math.sin(ang) * r / m_lat, 6),
                "base": base * (0.9 + 0.2 * rnd.random()), "sigma": sigma,
            })

    def sample(self, t):
        out = []
        for a in self.assets:
            start, rate = DRIFT.get(a["id"], (0, 0.0))
            v = a["base"] + self.rnd.gauss(0, a["sigma"]) + max(0, t - start) * rate
            out.append({k: a[k] for k in ("id", "kind", "metric", "unit", "limit", "lon", "lat")} | {"value": round(v, 2)})
        return out
