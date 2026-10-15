"""The real city from OpenStreetMap: junctions found from street geometry, passports, drafts never commanded."""
import json
import math

import pytest

from urban.analytics.queues import WINDOW
from urban.city.build import build
from urban.city.passport import PassportError, Passports, content_hash
from urban.junctions import APPROVED, DRAFT, _junction, approved, approved_plans
from urban.platform import Platform
from urban.sources import clock

LON0, LAT0 = 71.785, 40.385
M_LON = 111320.0 * math.cos(math.radians(LAT0))
BBOX = f"{LON0 - 0.006},{LAT0 - 0.006},{LON0 + 0.006},{LAT0 + 0.006}"


def ll(x, y):
    return LON0 + x / M_LON, LAT0 + y / 111320.0


def district(tmp_path, extra_signal=False):
    """A crossroads with a signal on its centre node, a pedestrian signal on the main road 200 m east,
    and a side street joining 30 m west of the junction."""
    nodes = {1: (0, 0, True), 2: (-300, 0, False), 3: (-30, 0, False), 4: (200, 0, True), 5: (300, 0, False),
             6: (0, 250, False), 7: (0, -250, False), 8: (-30, 200, False)}
    if extra_signal:
        nodes[9] = (-150, 0, False)
    xml = ['<?xml version="1.0"?><osm version="0.6">']
    for nid, (x, y, signal) in nodes.items():
        lon, lat = ll(x, y)
        tag = '<tag k="highway" v="traffic_signals"/>' if signal else ""
        xml.append(f'<node id="{nid}" lon="{lon:.7f}" lat="{lat:.7f}">{tag}</node>')

    def way(wid, refs, **tags):
        nds = "".join(f'<nd ref="{r}"/>' for r in refs)
        tg = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        xml.append(f'<way id="{wid}">{nds}{tg}</way>')
    way(10, [2, 3, 1], highway="primary", name="Alpha", lanes="4")
    way(11, [1, 4, 5], highway="primary", name="Alpha", lanes="4")
    way(12, [6, 1], highway="residential", name="Beta")
    way(13, [1, 7], highway="residential", name="Beta")
    way(14, [3, 8], highway="residential", name="Gamma")
    xml.append("</osm>")
    path = tmp_path / "city.osm"
    path.write_text("".join(xml), encoding="utf-8")
    return path


def test_junctions_are_found_from_street_geometry(tmp_path):
    out = tmp_path / "city"
    r = build(district(tmp_path), out, "Тестоград", BBOX)
    assert r["junctions"] == 1  # the pedestrian signal on a straight road is not a junction
    j = json.loads((out / "signals.json").read_text(encoding="utf-8"))[0]
    assert j["id"] == "J-01" and j["tls"] == "J-01" and j["name"] == "Alpha / Beta"
    sides = {a["id"]: a for a in j["approaches"]}
    assert set(sides) == {"J-01-N", "J-01-S", "J-01-E", "J-01-W"}  # Gamma joins too far out to be an approach
    assert sides["J-01-E"]["lanes"] == 2 and sides["J-01-N"]["lanes"] == 1  # lanes=4 is both directions
    assert sides["J-01-E"]["from"] == "с востока"
    phases = {tuple(sorted(p["edges"])): p for p in j["phases"]}
    assert set(phases) == {("J-01-E", "J-01-W"), ("J-01-N", "J-01-S")}  # one phase per crossing axis
    assert all(p["duration"] >= p["min_green"] for p in j["phases"])
    assert sum(p["duration"] for p in j["phases"]) + 8 == j["cycle"]
    assert len(j["plans"]) > 1 and j["plans"][0]["id"] == 0
    assert j["passport"]["status"] == DRAFT and j["passport"]["source"] == "osm"
    for name in ("roads.geojson", "buildings.geojson", "stops.geojson", "cameras.geojson", "meta.json"):
        assert (out / name).exists()
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Тестоград" and meta["radius"] > 400


def test_a_draft_gives_nothing_to_command():
    j = _junction({"id": "J-01", "name": "x", "target": [LON0, LAT0], "plans": [{"id": 0, "splits": {"1": 30}}],
                   "passport": {"status": DRAFT}})
    assert not approved(j) and approved_plans([j]) == {}
    assert _junction({"id": "J-02", "name": "x", "target": [LON0, LAT0]}, DRAFT)["passport"]["status"] == DRAFT
    assert approved_plans([{**j, "passport": {"status": APPROVED}}]) == {"J-01": {0}}


def test_rebuilding_keeps_ids_and_never_replaces_an_approved_passport(tmp_path):
    out = tmp_path / "city"
    osm = district(tmp_path)
    build(osm, out, "Тестоград", BBOX)
    book = Passports(out / "signals.json")
    book.edit("J-01", {"phases": [{"index": 1, "duration": 30}]}, "engineer")
    book.approve("J-01", "engineer")
    before = book.get("J-01")
    r = build(osm, out, "Тестоград", BBOX)
    assert r["kept"] == 1 and r["new"] == 0
    assert Passports(out / "signals.json").get("J-01") == before

    book = Passports(out / "signals.json")
    book.withdraw("J-01", "engineer", "сверяем заново")
    draft = book.edit("J-01", {"approaches": [{"id": "J-01-E", "zone": 60}]}, "engineer")
    build(osm, out, "Тестоград", BBOX)  # an edited draft is work in progress: kept too
    assert Passports(out / "signals.json").get("J-01") == draft


def test_passport_edit_approve_withdraw(tmp_path):
    out = tmp_path / "city"
    build(district(tmp_path), out, "Тестоград", BBOX)
    book = Passports(out / "signals.json")
    j = book.get("J-01")
    first = next(p for p in j["phases"] if "J-01-E" in p["edges"])
    with pytest.raises(PassportError):
        book.edit("J-01", {"phases": [{"index": first["index"], "duration": 500}]}, "engineer")
    with pytest.raises(PassportError):
        book.edit("J-01", {"approaches": [{"id": "J-01-X", "zone": 50}]}, "engineer")
    with pytest.raises(PassportError):
        book.edit("J-01", {"phases": [{"index": first["index"], "edges": ["nowhere"]}]}, "engineer")

    j = book.edit("J-01", {"phases": [{"index": first["index"], "duration": 40}],
                           "approaches": [{"id": "J-01-E", "zone": 80}]}, "engineer")
    assert j["passport"]["status"] == DRAFT and j["passport"]["version"] == 2
    assert next(p for p in j["phases"] if p["index"] == first["index"])["duration"] == 40
    assert j["cycle"] == sum(p["duration"] for p in j["phases"]) + 8
    assert all(sum(pl["splits"].values()) == sum(p["duration"] for p in j["phases"]) for pl in j["plans"])

    # a phase below its minimum green cannot be approved
    book.edit("J-01", {"phases": [{"index": first["index"], "duration": 5, "min_green": 20}]}, "engineer")
    with pytest.raises(PassportError, match="меньше минимума"):
        book.approve("J-01", "engineer")
    book.edit("J-01", {"phases": [{"index": first["index"], "duration": 40}]}, "engineer")
    j = book.approve("J-01", "chief")
    assert j["passport"]["status"] == APPROVED and j["passport"]["approved_by"] == "chief"
    assert j["passport"]["hash"] == content_hash(j)
    with pytest.raises(PassportError):
        book.approve("J-01", "chief")
    assert Passports(out / "signals.json").get("J-01")["passport"]["status"] == APPROVED  # saved

    j = book.withdraw("J-01", "chief", "контроллер заменили")
    assert j["passport"]["status"] == DRAFT and "hash" not in j["passport"]
    edited = book.edit("J-01", {"name": "Alpha / Beta (центр)"}, "engineer")
    assert edited["passport"]["version"] == 5 and edited["name"] == "Alpha / Beta (центр)"


def test_platform_watches_a_draft_and_commands_only_after_approval(tmp_path):
    out = tmp_path / "city"
    build(district(tmp_path), out, "Тестоград", BBOX)
    book = Passports(out / "signals.json")
    raw = book.get("J-01")
    j = _junction(raw, DRAFT)
    platform = Platform([LON0, LAT0], 500, [j])
    main = next(p for p in j["phases"] if "J-01-N" in p["edges"])  # the side street: the main road has green to spare
    loaded = {a: (90.0 if a in main["edges"] else 3.0) for a in (x["id"] for x in j["approaches"])}
    t = 1_800_000_000

    def run(seconds):
        nonlocal t
        for _ in range(seconds):
            t += 1
            platform.on_report("J-01", {"t": t, "queues": loaded, "events": []})
            platform.handle(clock.frame(t))

    run(WINDOW * 3)
    assert platform.queues["J-01"]["J-01-N"] == 90.0  # watched: the queue is seen
    assert not platform.gateway.proposals              # but nothing is proposed for a draft
    assert not [r for r in platform.gateway.records if r["event"] == "denied"]  # and the log is not flooded

    approved_raw = book.approve("J-01", "chief")
    platform.update_junction(_junction(approved_raw, DRAFT), "passport-approved")
    assert platform.gateway.plans["J-01"] == {pl["id"] for pl in approved_raw["plans"]}
    run(WINDOW + 5)  # one averaging window with the new passport, well before an undecided proposal expires
    pending = [p for p in platform.gateway.proposals.values() if p["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["give"] == main["index"]

    withdrawn = book.withdraw("J-01", "chief")
    platform.update_junction(_junction(withdrawn, DRAFT), "passport-withdrawn")
    assert "J-01" not in platform.gateway.plans
    assert pending[0]["status"] == "expired"  # a proposal made against the old table is taken back
