"""OpenStreetMap -> the map of a real city and a draft passport for every signalised junction.

No simulator is involved. Junctions are found where OSM has traffic signals; their approaches are the
roads that lead into them, read from the street geometry. Phases and plans are only a first guess
(two phases, one per crossing axis) and the passport stays a draft: the platform watches such a junction
but proposes nothing for it, and its unit obeys nothing, until an engineer checks and approves it.
"""
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from ..pipeline.export import build_plans, min_green
from ..pipeline.osm import export_osm_layers
from ..pipeline.paths import dump

M_LAT = 111320.0
DRIVABLE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street",
            "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
RANK = {"motorway": 6, "trunk": 5, "primary": 4, "secondary": 3, "tertiary": 2}  # the rest: 1
LANES = {"motorway": 4, "trunk": 4, "primary": 4, "secondary": 2, "tertiary": 2}  # both directions, when untagged
SPEED = {"motorway": 90, "trunk": 80, "primary": 60, "secondary": 60, "tertiary": 50}  # km/h, when untagged; else 40
CLUSTER = 45.0       # metres: OSM maps signal heads at the stop lines, one junction's heads are this close at most
MARGIN = 15.0        # metres beyond the outermost signal head where an approach begins
CORE = 10.0          # metres around the heads a road must reach to count as an approach (not a side street)
ZONE = 100.0         # metres of an approach a queue sensor covers, at most
SAME = 25.0          # degrees: inbound roads closer than this in heading, and close together, are one approach
LOST = 4             # seconds of amber and all-red per phase
MATCH = 30.0         # metres: a junction found again within this distance keeps its id and passport
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
COMPASS_RU = {"N": "с севера", "NE": "с северо-востока", "E": "с востока", "SE": "с юго-востока",
              "S": "с юга", "SW": "с юго-запада", "W": "с запада", "NW": "с северо-запада"}
DRAFT_NOTE = ("черновик из OpenStreetMap: подъезды найдены по геометрии улиц, фазы и планы предположены; "
              "инженер должен сверить с контроллером и утвердить")


class Frame:
    """Local metres around a centre point."""

    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = lon0, lat0
        self.m_lon = M_LAT * math.cos(math.radians(lat0))

    def xy(self, lon, lat):
        return (lon - self.lon0) * self.m_lon, (lat - self.lat0) * M_LAT

    def ll(self, x, y):
        return [round(self.lon0 + x / self.m_lon, 7), round(self.lat0 + y / M_LAT, 7)]


def read_osm(path: Path):
    """Nodes, signal heads, drivable ways and bus stops of an OSM extract."""
    nodes, signals, ways, stops = {}, [], [], []
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "node":
            nid, lon, lat = el.get("id"), float(el.get("lon")), float(el.get("lat"))
            nodes[nid] = (lon, lat)
            if len(el):
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                if tags.get("highway") == "traffic_signals":
                    signals.append(nid)
                if tags.get("highway") == "bus_stop":
                    stops.append({"type": "Feature", "properties": {"id": f"n{nid}", "name": tags.get("name", "")},
                                  "geometry": {"type": "Point", "coordinates": [lon, lat]}})
            el.clear()
        elif el.tag == "way":
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            if tags.get("highway") in DRIVABLE and tags.get("area") != "yes":
                refs = [nd.get("ref") for nd in el.findall("nd") if nd.get("ref") in nodes]
                if len(refs) >= 2:
                    ways.append({"id": el.get("id"), "nodes": refs, "tags": tags})
            el.clear()
        elif el.tag == "relation":
            el.clear()
    return nodes, signals, ways, stops


def _int(value, default):
    try:
        return max(1, int(str(value).split(";")[0].strip()))
    except (TypeError, ValueError):
        return default


def lanes_total(tags) -> int:
    hw = tags["highway"].removesuffix("_link")
    oneway = oneway_of(tags) != 0
    return _int(tags.get("lanes"), (1 if oneway else 2) if hw not in LANES else (LANES[hw] // 2 if oneway else LANES[hw]))


def oneway_of(tags) -> int:
    """1: only along the way, -1: only against it, 0: both directions."""
    v = tags.get("oneway", "")
    if v in ("yes", "true", "1") or tags.get("junction") in ("roundabout", "circular") or tags["highway"] == "motorway":
        return 1 if v != "-1" else -1
    return -1 if v == "-1" else 0


def lanes_inbound(tags, direction: int) -> int:
    """Lanes in the direction of travel along the way (direction 1) or against it (-1)."""
    key = "lanes:forward" if direction == 1 else "lanes:backward"
    if key in tags:
        return _int(tags[key], 1)
    total = lanes_total(tags)
    return total if oneway_of(tags) != 0 else max(1, total // 2)


def roads_layer(ways, nodes) -> list[dict]:
    out = []
    for w in ways:
        t = w["tags"]
        hw = t["highway"]
        out.append({"type": "Feature",
                    "properties": {"id": f"w{w['id']}", "name": t.get("name", ""), "lanes": lanes_total(t),
                                   "speed": _int((t.get("maxspeed") or "").split()[0] if t.get("maxspeed") else None,
                                                 SPEED.get(hw.removesuffix("_link"), 40)),
                                   "bus": False, "cls": hw},
                    "geometry": {"type": "LineString", "coordinates": [list(nodes[n]) for n in w["nodes"]]}})
    return out


def clusters(points: dict[str, tuple[float, float]]) -> list[list[str]]:
    """Signal heads closer than CLUSTER, joined transitively."""
    ids = list(points)
    parent = {i: i for i in ids}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cells: dict[tuple[int, int], list[str]] = {}
    for i in ids:
        x, y = points[i]
        cells.setdefault((int(x // CLUSTER), int(y // CLUSTER)), []).append(i)
    for (cx, cy), members in cells.items():
        near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in cells.get((cx + dx, cy + dy), [])]
        for i in members:
            for j in near:
                if i < j and math.dist(points[i], points[j]) < CLUSTER:
                    parent[find(i)] = find(j)
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def heading(ax, ay, bx, by) -> float:
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360


def angle_diff(a, b, period=360.0) -> float:
    d = abs(a - b) % period
    return min(d, period - d)


class City:
    def __init__(self, osm: Path, frame: Frame):
        self.frame = frame
        self.nodes_ll, signals, self.ways, self.stops = read_osm(osm)
        self.xy = {n: frame.xy(*ll) for n, ll in self.nodes_ll.items()}
        on_roads = {n for w in self.ways for n in w["nodes"]}
        self.signals = [s for s in signals if s in on_roads]  # a signal on a footway is not a road junction
        self._heads = set(self.signals)
        self.at_node: dict[str, list[tuple[int, int]]] = {}  # node -> (way index, position in the way)
        for wi, w in enumerate(self.ways):
            for pos, n in enumerate(w["nodes"]):
                self.at_node.setdefault(n, []).append((wi, pos))

    # ---- one junction
    def approaches(self, cx, cy, heads: list[str]) -> list[dict]:
        reach = max((math.dist(self.xy[h], (cx, cy)) for h in heads), default=0.0)
        radius, core = max(25.0, reach) + MARGIN, reach + CORE
        found = []
        for wi, w in enumerate(self.ways):
            pts = [self.xy[n] for n in w["nodes"]]
            if not any(math.dist(p, (cx, cy)) <= core for p in pts) and not set(heads) & set(w["nodes"]):
                continue  # never reaches the junction itself: a side street that joins nearby
            one = oneway_of(w["tags"])
            for k in range(len(pts) - 1):
                da, db = math.dist(pts[k], (cx, cy)), math.dist(pts[k + 1], (cx, cy))
                if da > radius >= db and one != -1:      # entering along the way
                    found.append(self._approach(w, wi, k, k + 1, radius, (cx, cy), 1))
                elif db > radius >= da and one != 1:     # entering against the way
                    found.append(self._approach(w, wi, k + 1, k, radius, (cx, cy), -1))
        found.sort(key=lambda a: (-a["rank"], -a["lanes"]))
        merged = []
        for a in found:  # one approach per direction: drop parallel ways close to a stronger one
            if any(angle_diff(a["heading"], b["heading"]) < SAME and math.dist(a["xy"], b["xy"]) < 30 for b in merged):
                continue
            merged.append(a)
        return merged

    def _approach(self, w, wi, outer, inner, radius, centre, direction) -> dict:
        (ox, oy), (ix, iy) = self.xy[w["nodes"][outer]], self.xy[w["nodes"][inner]]
        do, di = math.dist((ox, oy), centre), math.dist((ix, iy), centre)
        t = (do - radius) / (do - di) if do != di else 0.0
        px, py = ox + (ix - ox) * t, oy + (iy - oy) * t
        return {"way": w["id"], "name": w["tags"].get("name", ""), "heading": heading(ox, oy, ix, iy),
                "xy": (px, py), "lanes": lanes_inbound(w["tags"], direction),
                "rank": RANK.get(w["tags"]["highway"].removesuffix("_link"), 1),
                "zone": round(min(ZONE, math.dist((px, py), (ox, oy)) + self._upstream(wi, outer, -direction)), 1)}

    def _upstream(self, wi, pos, step, limit=ZONE) -> float:
        """Road length behind a point, walking against the traffic along the way and its continuations."""
        total, seen = 0.0, {(wi, pos)}
        while total < limit:
            nodes = self.ways[wi]["nodes"]
            nxt = pos + step
            if 0 <= nxt < len(nodes):
                total += math.dist(self.xy[nodes[pos]], self.xy[nodes[nxt]])
                if nodes[nxt] in self._heads:
                    break  # the previous signalised junction
                pos = nxt
                continue
            here, name = nodes[pos], self.ways[wi]["tags"].get("name")
            cont = [(w2, p2) for w2, p2 in self.at_node.get(here, []) if w2 != wi and (w2, p2) not in seen
                    and self.ways[w2]["tags"].get("name") == name]
            if not cont:
                break
            wi, pos = cont[0]
            seen.add((wi, pos))
            nodes = self.ways[wi]["nodes"]
            step = 1 if pos == 0 else -1 if pos == len(nodes) - 1 else step
        return min(total, limit)

    # ---- every junction
    def junctions(self, radius_limit: float) -> list[dict]:
        out = []
        for heads in clusters({s: self.xy[s] for s in self.signals}):
            cx = sum(self.xy[h][0] for h in heads) / len(heads)
            cy = sum(self.xy[h][1] for h in heads) / len(heads)
            if math.hypot(cx, cy) > radius_limit:
                continue
            approaches = self.approaches(cx, cy, heads)
            j = draft(approaches)
            if j is None:
                continue  # a pedestrian crossing on a straight road: nothing to rebalance
            j.update({"target": self.frame.ll(cx, cy), "osm": sorted(heads, key=int), "xy": (cx, cy)})
            out.append(j)
        return out


def draft(found: list[dict]) -> dict | None:
    """Approaches, two draft phases (one per crossing axis) and the plan table of a junction, or None."""
    if len(found) < 2:
        return None
    main = found[0]  # strongest road first
    axis = main["heading"] % 180
    first = [a for a in found if angle_diff(a["heading"] % 180, axis, 180) < 45]
    second = [a for a in found if a not in first]
    if not second:
        return None
    approaches, used = [], set()
    for a in sorted(found, key=lambda a: a["heading"]):
        side = COMPASS[round(((a["heading"] + 180) % 360) / 45) % 8]  # where the cars come from
        ident = side if side not in used else f"{side}{sum(u.startswith(side) for u in used) + 1}"
        used.add(ident)
        a["side"], a["key"] = side, ident
        approaches.append(a)
    lanes = {a["key"]: a["lanes"] for a in approaches}
    groups = [first, second]
    total = sum(a["lanes"] for a in approaches)
    cycle = 90 if total >= 8 else 60
    green = cycle - LOST * len(groups)
    weight = [max(a["lanes"] for a in g) for g in groups]
    first_green = round(green * weight[0] / sum(weight))
    phases = []
    for i, g in enumerate(groups):
        phases.append({"index": i + 1, "duration": first_green if i == 0 else green - first_green,
                       "edges": [a["key"] for a in g],
                       "streets": [n for n in dict.fromkeys(a["name"] for a in g) if n], "downstream": []})
    for p in phases:
        p["min_green"] = min_green(p, phases, lanes)
    for p, other in ((phases[0], phases[1]), (phases[1], phases[0])):  # keep both above their minimum
        if p["duration"] < p["min_green"]:
            other["duration"] -= p["min_green"] - p["duration"]
            p["duration"] = p["min_green"]
    names = [n for n in dict.fromkeys(a["name"] for a in found) if n]
    return {"name": " / ".join(names[:2]) or "перекрёсток", "approaches": approaches, "phases": phases,
            "cycle": cycle}


def finish(j: dict, ident: str) -> dict:
    """Give the junction its id: approach ids become <junction>-<side>, the plan table is built."""
    keys = {a["key"]: f"{ident}-{a['key']}" for a in j["approaches"]}
    for p in j["phases"]:
        p["edges"] = [keys[k] for k in p["edges"]]
    return {
        "id": ident, "tls": ident, "camera": False, "name": j["name"], "target": j["target"], "osm": j["osm"],
        "approaches": [{"id": keys[a["key"]], "name": a["name"], "from": COMPASS_RU[a["side"]], "lanes": a["lanes"],
                        "zone": a["zone"], "heading": round(a["heading"])} for a in j["approaches"]],
        "phases": j["phases"], "cycle": j["cycle"], "plans": build_plans(j["phases"]),
        "passport": {"status": "draft", "source": "osm", "version": 1, "note": DRAFT_NOTE},
    }


def kept(passport: dict) -> bool:
    """A passport a person approved or worked on is never replaced by what OSM says now."""
    return passport.get("status") == "approved" or "edited_by" in passport


def build(osm: Path, out: Path, name: str, bbox: str) -> dict:
    """Write the map layers and the junctions of a city into `out`. Junctions found before keep their ids;
    approved passports and drafts an engineer has edited are kept as they are."""
    out.mkdir(parents=True, exist_ok=True)
    w, s, e, n = (float(v) for v in bbox.split(","))
    lat0 = (s + n) / 2
    frame = Frame((w + e) / 2, lat0)
    radius = min((e - w) * frame.m_lon, (n - s) * M_LAT) / 2
    city = City(osm, frame)

    export_osm_layers(osm, out)
    dump("roads.geojson", roads_layer(city.ways, city.nodes_ll), out)
    dump("stops.geojson", city.stops, out)
    dump("cameras.geojson", [], out)  # camera windows belong to the demo twin; real cameras live on the units

    old_path = out / "signals.json"
    old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else []
    found = sorted(city.junctions(radius * 0.98), key=lambda j: (-round(j["target"][1], 3), j["target"][0]))
    taken = {o["id"] for o in old}
    result, n_kept, fresh = [], 0, 0
    for j in found:
        x, y = j["xy"]
        match = next((o for o in old if math.dist(frame.xy(*o["target"]), (x, y)) < MATCH), None)
        if match is not None:
            old.remove(match)
            if kept(match.get("passport", {})):
                result.append(match)  # the engineer's passport wins over anything OSM says now
                n_kept += 1
                continue
            ident = match["id"]
        else:
            k = 1
            while f"J-{k:02d}" in taken:
                k += 1
            ident = f"J-{k:02d}"
            taken.add(ident)
            fresh += 1
        result.append(finish(j, ident))
    gone = [o for o in old if kept(o.get("passport", {}))]
    result += gone  # junctions OSM no longer shows stay, if people worked on them, until removed on purpose
    result.sort(key=lambda j: j["id"])
    old_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = {"name": name, "bbox": bbox, "center": [(w + e) / 2, lat0], "radius": round(radius)}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"junctions": len(result), "new": fresh, "kept": n_kept + len(gone), "roads": len(city.ways),
            "stops": len(city.stops)}
