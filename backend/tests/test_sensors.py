"""The camera pipeline on a synthetic approach: pixels -> metres -> speed -> queue."""
import json

import pytest

from urban.sensors.geometry import Homography, View
from urban.sensors.pipeline import STALE, QueuePipeline, load_views
from urban.sensors.tracking import UNKNOWN, LaneTracker

def pixel(x: float, y: float) -> tuple[float, float]:
    """A synthetic camera looking up a two-lane, 7 m wide approach: a true perspective (projective) map.
    The stop line (x = 0) spans u 300..900 at v = 700; 100 m away the road is 200 px wide at v = 200."""
    w = 0.02 * x + 1
    return (12 * x + 600 / 7 * y + 300) / w, (700 - x) / w


POINTS = [{"px": list(pixel(x, y)), "m": [x, y]} for x, y in ((0, 0), (0, 7), (100, 0), (100, 7))]


def test_homography_reproduces_the_reference_points_and_interpolates():
    h = Homography([((p["px"][0], p["px"][1]), (p["m"][0], p["m"][1])) for p in POINTS])
    for p in POINTS:
        x, y = h.road(*p["px"])
        assert abs(x - p["m"][0]) < 1e-6 and abs(y - p["m"][1]) < 1e-6
    x, y = h.road(*pixel(50, 3.5))
    assert abs(x - 50) < 1e-6 and abs(y - 3.5) < 1e-6
    with pytest.raises(ValueError):
        Homography([((0, 0), (0, 0)), ((1, 0), (1, 0)), ((2, 0), (2, 0)), ((3, 0), (3, 0))])


def box_at(x: float, lane: int, length: float = 4.5):
    """A detection whose bottom centre sits at x metres in the middle of a lane."""
    u, v = pixel(x, lane * 3.5 + 1.75)
    _, v_far = pixel(x + length, lane * 3.5 + 1.75)
    h = (v - v_far) / 0.6
    return [u - 20, v - h, 40, h]


def test_view_places_boxes_on_lanes():
    view = View("A", POINTS, lanes=2)
    lane, x, length = view.place(tuple(box_at(30, 1)))
    assert lane == 1 and abs(x - 30) < 0.5 and 4 <= length <= 6
    assert view.place((0, 0, 10, 10)) is None  # sky


def test_tracker_measures_speed_and_forgets():
    tr = LaneTracker()
    assert tr.update(0.0, [(50.0, 4.5)]) == [(50.0, UNKNOWN, 4.5)]
    out = tr.update(0.5, [(45.0, 4.5)])
    assert abs(out[0][1] - 10.0) < 1e-6 and len(tr.tracks) == 1
    tr.update(1.0, [(40.0, 4.5), (80.0, 12.0)])  # a bus appears behind
    assert len(tr.tracks) == 2
    assert tr.update(5.0, []) == [] and tr.tracks == {}


def test_pipeline_reports_a_standing_queue_and_no_data_when_frames_stop(tmp_path):
    p = QueuePipeline([View("A", POINTS, lanes=2, zone=100.0), View("B", POINTS, lanes=2)])
    # lane 0: four cars standing from the stop line back; lane 1: one car driving through
    standing = [box_at(2, 0), box_at(10, 0), box_at(18, 0), box_at(26, 0)]
    for i in range(6):
        t = i * 0.5
        moving = box_at(60 - 8 * t, 1)
        q = p.feed("A", t, standing + [moving])
    assert 28 <= q <= 32  # the tail of the fourth car: 26 m + its length
    p.feed("B", 2.5, [])
    assert p.queues(2.5) == {"A": q, "B": 0.0}
    assert p.queues(2.5 + STALE + 1) == {}
    assert p.feed("nowhere", 0, []) is None
    p.write(tmp_path / "queues.json", now=2.5)
    assert json.loads((tmp_path / "queues.json").read_text()) == {"A": q, "B": 0.0}


def test_calibration_file(tmp_path):
    f = tmp_path / "cams.json"
    f.write_text(json.dumps([{"approach": "A", "points": POINTS, "lanes": 3, "zone": 80}]))
    views = load_views(f)
    assert views[0].approach == "A" and views[0].lanes == 3 and views[0].zone == 80
