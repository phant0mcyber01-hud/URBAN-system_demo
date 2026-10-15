"""Downstream links between junctions and the (measured, switched off) spillback rule."""
from types import SimpleNamespace

from urban.pipeline.links import downstream
from urban.platform import SPILL, Platform


class Edge:
    def __init__(self, ident, length=100.0, passenger=True):
        self.ident, self.length, self.passenger, self.out = ident, length, passenger, []

    def getID(self):
        return self.ident

    def getLength(self):
        return self.length

    def allows(self, cls):
        return self.passenger

    def getOutgoing(self):
        return {e: [] for e in self.out}


def test_links_follow_the_road_to_the_next_signalised_approach():
    a_in, link, b_in, far = Edge("a_in"), Edge("ab"), Edge("b_in"), Edge("far", length=500)
    a_in.out, link.out, b_in.out = [link], [b_in, far], []
    net = SimpleNamespace(getEdge=lambda i: {e.getID(): e for e in (a_in, link, b_in, far)}[i])
    js = [{"id": "A", "phases": [{"index": 0, "edges": ["a_in"]}]},
          {"id": "B", "phases": [{"index": 2, "edges": ["b_in"]}]}]
    assert downstream(net, js) == 1
    assert js[0]["phases"][0]["downstream"] == [{"junction": "B", "approach": "b_in", "phase": 2}]
    assert js[1]["phases"][0]["downstream"] == []


def test_full_ahead_reads_the_downstream_queue():
    cams = [{"id": "A", "tls": "TA", "camera": False, "lon": 71.78, "lat": 40.38, "name": "A",
             "approaches": [{"id": "a_in", "zone": 100.0}],
             "phases": [{"index": 0, "edges": ["a_in"], "downstream": [{"junction": "B", "approach": "b_in", "phase": 2}]}],
             "plans": []},
            {"id": "B", "tls": "TB", "camera": False, "lon": 71.78, "lat": 40.38, "name": "B",
             "approaches": [{"id": "b_in", "zone": 100.0}], "phases": [{"index": 2, "edges": ["b_in"]}], "plans": []}]
    p = Platform([71.78, 40.38], 1000, cams, spillback=True)
    p.queues = {"B": {"b_in": SPILL * 100 - 1}}
    assert p.full_ahead("TA", 0) is None
    p.queues = {"B": {"b_in": SPILL * 100}}
    assert p.full_ahead("TA", 0) == "B"
    p.queues = {"B": None}                       # no data downstream is not a full block
    assert p.full_ahead("TA", 0) is None
    assert Platform([71.78, 40.38], 1000, cams).spillback is False  # off unless asked for
