"""OSM download and the map layers taken straight from OSM (buildings, green, water, POIs)."""
import math
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .. import config as c
from .paths import OSM, dump


def download():
    if OSM.exists() and OSM.stat().st_size > 0:
        return
    w, s, e, n = c.BBOX.split(",")
    q = (f"[out:xml][timeout:180];(nwr({s},{w},{n},{e});"
         f"rel[route~'^(bus|trolleybus|share_taxi)$']({s},{w},{n},{e}););(._;>;);out meta;")
    overpass = urllib.parse.urlencode({"data": q}).encode()
    # OSM API first (limit: 50k nodes per bbox), Overpass mirrors as fallback for bigger areas
    sources = [(f"https://api.openstreetmap.org/api/0.6/map?bbox={w},{s},{e},{n}", None),
               ("https://overpass-api.de/api/interpreter", overpass),
               ("https://overpass.kumi.systems/api/interpreter", overpass)]
    for url, data in sources:
        print(">> downloading OSM for bbox", c.BBOX, "from", url.split("/")[2])
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "urban-demo/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                OSM.write_bytes(r.read())
            return
        except OSError as err:
            print("   failed:", err)
    raise SystemExit("could not download OSM data")


GREEN = {"leisure": {"park", "garden", "playground", "stadium", "recreation_ground"},
         "landuse": {"grass", "forest", "meadow", "recreation_ground", "cemetery", "village_green", "orchard"},
         "natural": {"wood", "scrub", "grassland"}}
LOW = {"house", "detached", "garage", "garages", "shed", "hut", "roof", "kiosk", "service", "bungalow"}


def area_kind(tags):
    if tags.get("natural") == "water" or tags.get("landuse") in ("reservoir", "basin"):
        return "water"
    if tags.get("leisure") == "pitch":
        return "pitch"
    if tags.get("amenity") == "parking":
        return "parking"
    if any(tags.get(k) in v for k, v in GREEN.items()):
        return "green"
    return None


def line_kind(tags):
    if tags.get("waterway") in ("river", "canal", "stream", "ditch", "drain"):
        return "water"
    if tags.get("highway") in ("service", "track"):
        return "service"
    if tags.get("highway") in ("footway", "path", "pedestrian", "steps", "cycleway"):
        return "path"
    if tags.get("railway") in ("rail", "light_rail"):
        return "rail"
    return None


def ring_area_m2(ring):
    k = 111320.0
    kx = k * math.cos(math.radians(ring[0][1]))
    return abs(sum((x0 * kx) * (y1 * k) - (x1 * kx) * (y0 * k)
                   for (x0, y0), (x1, y1) in zip(ring, ring[1:]))) / 2


def building_height(way_id, tags, ring):
    """Tagged height if present, otherwise an estimate from type and footprint (est=True)."""
    try:
        if "height" in tags:
            return float(tags["height"].split()[0].replace(",", ".")), False
        if "building:levels" in tags:
            return float(tags["building:levels"].replace(",", ".")) * 3.2 + 1, False
    except ValueError:
        pass
    r = (int(way_id) * 2654435761 % 1000) / 1000  # stable pseudo-random 0..1
    kind, area = tags["building"], ring_area_m2(ring)
    if kind in LOW or area < 120:
        lo, hi = 3.0, 5.5
    elif kind == "apartments":
        lo, hi = 13.0, 29.0
    elif area < 500:
        lo, hi = 5.0, 10.0
    elif area < 2000:
        lo, hi = 8.0, 17.0
    else:
        lo, hi = 9.0, 14.0
    return round(lo + (hi - lo) * r, 1), True


def export_osm_layers():
    """Buildings plus context layers (green, water, paths, trees, signals) straight from OSM."""
    nodes, buildings, areas, lines, points, pois = {}, [], [], [], [], []

    def poi(tags, xy):
        if tags.get("amenity") in ("hospital", "clinic", "fire_station", "police"):
            pois.append({"type": "Feature", "properties": {"kind": tags["amenity"], "name": tags.get("name", "")},
                         "geometry": {"type": "Point", "coordinates": xy}})

    for _, el in ET.iterparse(OSM, events=("end",)):
        if el.tag == "node":
            xy = (float(el.get("lon")), float(el.get("lat")))
            nodes[el.get("id")] = xy
            if len(el):
                tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
                poi(tags, xy)
                kind = ("tree" if tags.get("natural") == "tree" else
                        "signal" if tags.get("highway") == "traffic_signals" else
                        "crossing" if tags.get("highway") == "crossing" else None)
                if kind:
                    points.append({"type": "Feature", "properties": {"kind": kind},
                                   "geometry": {"type": "Point", "coordinates": xy}})
            el.clear()
        elif el.tag == "way":
            tags = {t.get("k"): t.get("v") for t in el.findall("tag")}
            pts = [nodes[nd.get("ref")] for nd in el.findall("nd") if nd.get("ref") in nodes]
            closed = len(pts) >= 4 and pts[0] == pts[-1]
            if pts:
                poi(tags, (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)))
            if "building" in tags and closed:
                h, est = building_height(el.get("id"), tags, pts)
                props = {"kind": tags["building"], "h": h, "est": est}
                if "name" in tags:
                    props["name"] = tags["name"]
                buildings.append({"type": "Feature", "properties": props,
                                  "geometry": {"type": "Polygon", "coordinates": [pts]}})
            elif closed and (k := area_kind(tags)):
                areas.append({"type": "Feature", "properties": {"kind": k, "name": tags.get("name", "")},
                              "geometry": {"type": "Polygon", "coordinates": [pts]}})
            elif len(pts) >= 2 and (k := line_kind(tags)):
                lines.append({"type": "Feature", "properties": {"kind": k},
                              "geometry": {"type": "LineString", "coordinates": pts}})
            el.clear()
    dump("buildings.geojson", buildings)
    dump("areas.geojson", areas)
    dump("lines.geojson", lines)
    dump("points.geojson", points)
    dump("pois.geojson", pois)
