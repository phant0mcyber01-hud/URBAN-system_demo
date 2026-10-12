import random

from urban.analytics import traffic
from urban.analytics.infra import Detector
from urban.core.events import Vehicle


def sample(value, limit=4.5):
    return [{"id": "НС-1", "kind": "water", "metric": "вибрация", "unit": "мм/с", "limit": limit,
             "lon": 0, "lat": 0, "value": value}]


def test_stable_asset_raises_no_alerts():
    rnd, det, alerts = random.Random(1), Detector(), []
    for t in range(0, 600, 5):
        alerts += det.update(t, sample(1.8 + rnd.gauss(0, 0.05)))[1]
    assert alerts == []


def test_drifting_asset_warns_with_forecast_then_alerts():
    rnd, det, alerts = random.Random(1), Detector(), []
    for t in range(0, 600, 5):
        alerts += det.update(t, sample(1.8 + rnd.gauss(0, 0.05) + max(0, t - 90) * 0.011))[1]
    assert [a["level"] for a in alerts] == ["warn", "alert"]
    assert alerts[0]["eta"] and alerts[0]["eta"] > 0


def test_jam_needs_a_slow_queue():
    def veh(edge, speed, n):
        return [Vehicle(f"{edge}{i}", 0, 0, 0, speed, "car", edge) for i in range(n)]
    found = traffic.jams(veh("slow", 0.5, 5) + veh("fast", 12, 5) + veh("few", 0.5, 2) + veh(":junction", 0.1, 9))
    assert found == ["slow"]


def test_camera_counts_are_aggregates_only():
    counts = traffic.camera_counts([(0, 0, 10), (30, 0, 20), (500, 0, 5)], [("CAM-01", 0, 0)])
    assert counts == {"CAM-01": [2, 54]}
