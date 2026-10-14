"""The platform without a simulation: a clock as the source, queues from device reports."""
import threading

from urban.edge.device import EdgeDevice
from urban.equipment.adapter import AdapterError, NoController
from urban.equipment.edge import EdgeController
from urban.platform import Platform
from urban.sources import clock


def test_clock_source_emits_one_frame_per_second():
    frames, stop = [], threading.Event()
    ticks = iter([100.2, 100.7, 101.1, 102.9, 103.0])

    def publish(f):
        frames.append(f)
        if len(frames) == 3:
            stop.set()
    clock.run(publish, stop, clock=lambda: next(ticks), sleep=lambda s: None)
    assert [f["t"] for f in frames] == [100, 101, 102]
    assert frames[0]["vehicles"] == [] and frames[0]["events"] == []


def test_platform_runs_on_device_reports_alone():
    cam = {"id": "J-1", "tls": "tls1", "camera": False, "lon": 71.78, "lat": 40.38, "name": "x",
           "approaches": [{"id": "a", "name": "A", "zone": 100.0}, {"id": "b", "name": "B", "zone": 100.0}],
           "phases": [{"index": 0, "duration": 40, "min_green": 10, "edges": ["a"], "streets": ["A"]},
                      {"index": 1, "duration": 40, "min_green": 10, "edges": ["b"], "streets": ["B"]}],
           "plans": [{"id": 0, "splits": {"0": 40, "1": 40}}, {"id": 2, "splits": {"0": 50, "1": 30}}]}
    p = Platform([71.78, 40.38], 1000, [cam])
    t0 = 1_800_000_000
    f = p.handle(clock.frame(t0))
    assert f["stats"]["cars"] == 0 and f["cams"]["J-1"][2] is None  # no report yet: no data
    p.on_report("tls1", {"t": t0 + 1, "queues": {"a": 55.0, "b": 4.0}, "events": []})
    f = p.handle(clock.frame(t0 + 1))
    assert f["cams"]["J-1"][2] == 55
    assert p.t == t0 + 1 and "reset" not in f


def test_unit_learns_its_plans_from_the_platform():
    adapter = NoController()
    dev = EdgeDevice.__new__(EdgeDevice)
    dev.adapter, dev.tls = adapter, "tls1"
    dev.on_welcome({"type": "welcome", "tls": "tls1", "plans": [0, 2, "3", "x"]})
    assert adapter.plans("tls1") == {0, 2, 3}
    dev.on_welcome({"type": "welcome", "tls": "other", "plans": [9]})  # not my junction: ignored
    assert adapter.plans("tls1") == {0, 2, 3}
    try:
        adapter.select("tls1", 2)
        assert False
    except AdapterError:
        pass
    ctl = EdgeController(adapter)
    ev = ctl.handle({"op": "lease", "id": "cmd-1", "tls": "tls1", "plan": 2, "ttl": 60}, 0)
    assert ev[0]["status"] == "rejected" and "no controller" in ev[0]["reason"]
