"""Camera vision under the power plan: no more frames than allowed, none while paused, threads from the plan."""
import json
import os
import time
from types import SimpleNamespace

import pytest

import urban.vision.__main__ as vision
from urban.vision.power import PowerPlan, Stats, Throttle


def put_plan(path, **vision_part):
    path.write_text(json.dumps({"vision": vision_part}))
    t = time.time() + len(vision_part) + vision_part.get("fps", 0)  # a new mtime every time
    os.utime(path, (t, t))


def test_without_a_plan_vision_keeps_its_own_settings(tmp_path):
    plan = PowerPlan(tmp_path / "missing.json", fps=4.0)
    plan.refresh()
    assert plan.fps == 4.0 and plan.threads(None) == 2 and plan.threads(3) == 3
    unused = PowerPlan(None, fps=4.0)
    unused.refresh()
    assert unused.fps == 4.0


def test_the_plan_is_followed_and_reread_when_it_changes(tmp_path):
    path = tmp_path / "power.json"
    put_plan(path, fps=1.0, threads=6)
    plan = PowerPlan(path, fps=4.0)
    plan.refresh()
    assert plan.fps == 1.0 and plan.threads(None) == 6 and plan.threads(2) == 2  # a given count wins
    put_plan(path, fps=0.0, threads=6)
    plan.refresh()
    assert plan.fps == 0.0
    put_plan(path, fps=9.0, threads=6)
    plan.refresh()
    assert plan.fps == 4.0  # never above what vision was started with
    path.write_text("{broken")
    os.utime(path, (time.time() + 99, time.time() + 99))
    plan.refresh()
    assert plan.fps == 4.0


def test_throttle_counts_per_camera():
    th = Throttle()
    analysed = {"a": 0, "b": 0}
    for i in range(200):  # frames every 50 ms for 10 s
        now = i * 0.05
        for cam in analysed:
            if th.due(cam, now, fps=2.0):
                th.mark(cam, now)
                analysed[cam] += 1
    assert analysed == {"a": 20, "b": 20}
    paused = Throttle()
    assert not any(paused.due("a", i * 0.05, fps=0.0) for i in range(200))


def test_stats_report_the_detector_time(tmp_path):
    stats = Stats(tmp_path / "vision.json")
    for ms in (10, 30, 20):
        stats.add(ms)
    stats.write(cameras=3, fps=4.0, now=100.0)
    assert json.loads((tmp_path / "vision.json").read_text()) == {"cameras": 3, "fps": 4.0, "ms_per_frame": 20}


class FakeFeed:
    """A camera that always has a new frame."""

    def __init__(self, cam, fps, stop):
        self.cam, self.source = cam, SimpleNamespace(error=None)

    def take(self):
        return time.time(), "frame"


@pytest.mark.parametrize("fps,most", [(2.0, 2 * 2.5 + 2), (0.0, 0)])
def test_run_analyses_no_more_than_the_plan_allows(tmp_path, monkeypatch, fps, most):
    plan_path = tmp_path / "power.json"
    put_plan(plan_path, fps=fps, threads=3)
    cams = [{"camera": "c1", "approach": "N", "source": "x"}, {"camera": "c2", "approach": "S", "source": "y"}]
    calls, made = {"c1": 0, "c2": 0}, {}
    end = time.time() + 2.5

    class Detector:
        def detect(self, frame):
            if time.time() > end:
                raise KeyboardInterrupt
            return []

    def detector(args, threads=None):
        made["threads"] = threads
        return Detector()

    class Pipeline:
        def __init__(self, views):
            pass

        def feed(self, approach, t, boxes):
            calls["c1" if approach == "N" else "c2"] += 1

        def write(self, out, now):
            if time.time() > end:
                raise KeyboardInterrupt

    monkeypatch.setattr(vision.cams, "load", lambda path: cams)
    monkeypatch.setattr(vision.cams, "calibrated", lambda c: True)
    monkeypatch.setattr(vision.cams, "view", lambda c: c)
    monkeypatch.setattr(vision, "QueuePipeline", Pipeline)
    monkeypatch.setattr(vision, "Latest", FakeFeed)
    monkeypatch.setattr(vision, "make_detector", detector)
    args = SimpleNamespace(cameras="cams.json", out=str(tmp_path / "queues.json"), power=str(plan_path),
                           fps=4.0, threads=None, model="m.onnx", score=0.35)
    vision.run(args)
    assert made["threads"] == 3
    assert all(n <= most for n in calls.values()), calls
    if fps:
        assert all(n >= 2 for n in calls.values()), calls  # it does work when allowed
