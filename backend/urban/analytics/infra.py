"""Asset health analytics: deviation from the asset's own baseline and a trend forecast.

Deliberately simple and explainable: z-score against the first samples, linear
regression over the recent window to estimate when a reading reaches its limit.
"""


class Detector:
    """Per-asset baseline (first samples) -> z-score; linear trend -> time until the limit."""
    BASELINE, WINDOW = 12, 12

    def __init__(self):
        self.hist, self.status = {}, {}

    def update(self, t, samples):
        assets, alerts = [], []
        for s in samples:
            h = self.hist.setdefault(s["id"], [])
            h.append((t, s["value"]))
            status, z, eta = "ok", 0.0, None
            if len(h) > self.BASELINE:
                base = [v for _, v in h[:self.BASELINE]]
                mean = sum(base) / len(base)
                std = max(1e-6, (sum((v - mean) ** 2 for v in base) / len(base)) ** 0.5)
                recent = h[-self.WINDOW:]
                z = (sum(v for _, v in recent[-3:]) / 3 - mean) / std
                tm = sum(x for x, _ in recent) / len(recent)
                vm = sum(v for _, v in recent) / len(recent)
                den = sum((x - tm) ** 2 for x, _ in recent)
                slope = sum((x - tm) * (v - vm) for x, v in recent) / den if den else 0.0
                if s["value"] >= s["limit"]:
                    status = "alert"
                elif z > 5:
                    status = "warn"
                    if slope > 0:
                        eta = round((s["limit"] - s["value"]) / slope)
            if status != "ok" and self.status.get(s["id"], "ok") != status:
                text = (f"{s['id']}: {s['metric']} {s['value']} {s['unit']}, порог {s['limit']} {s['unit']} превышен"
                        if status == "alert" else
                        f"{s['id']}: {s['metric']} {s['value']} {s['unit']} (+{z:.1f}σ к базовому уровню)"
                        + (f", прогноз порога {s['limit']} {s['unit']} через ~{max(1, eta // 60)} мин" if eta else ""))
                alerts.append({"id": f"{s['id']}-{status}", "t": t, "asset": s["id"], "kind": s["kind"],
                               "level": status, "text": text, "eta": eta})
            self.status[s["id"]] = status
            assets.append({**s, "status": status, "z": round(z, 1), "eta": eta})
        return assets, alerts
