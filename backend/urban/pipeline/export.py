"""SUMO network -> roads, stops, virtual cameras and scene metadata for the web layer."""
import html
import json
import math
import xml.etree.ElementTree as ET

from .. import config as c
from .paths import NET, STOPS, dump


def export_network():
    import sumolib
    from sumolib.geomhelper import positionAtShapeOffset

    net = sumolib.net.readNet(str(NET))

    def ll(x, y):
        lon, lat = net.convertXY2LonLat(x, y)
        return [round(lon, 6), round(lat, 6)]

    roads = [{
        "type": "Feature",
        "properties": {"id": e.getID(), "name": html.unescape(e.getName() or ""), "speed": round(e.getSpeed() * 3.6),
                       "lanes": e.getLaneNumber(), "bus": e.allows("bus"),
                       "cls": e.getType().split(".")[-1]},
        "geometry": {"type": "LineString", "coordinates": [ll(x, y) for x, y in e.getShape()]},
    } for e in net.getEdges()]

    stops = []
    for s in ET.parse(STOPS).getroot().iter("busStop"):
        lane = net.getLane(s.get("lane"))
        pos = (float(s.get("startPos")) + float(s.get("endPos"))) / 2
        x, y = positionAtShapeOffset(lane.getShape(), pos)
        stops.append({"type": "Feature", "properties": {"id": s.get("id"), "name": s.get("name", "")},
                      "geometry": {"type": "Point", "coordinates": ll(x, y)}})
    dump("roads.geojson", roads)
    dump("stops.geojson", stops)

    # the scene is a disc inscribed in the bbox: the web layer fades everything out towards its rim
    w, s_, e, n = (float(v) for v in c.BBOX.split(","))
    lat = (s_ + n) / 2
    radius = min((e - w) * 111320 * math.cos(math.radians(lat)), (n - s_) * 111320) / 2
    (c.OUT / "meta.json").write_text(json.dumps({"center": [(w + e) / 2, lat], "radius": round(radius)}))

    # virtual cameras: the busiest signalised junctions, spaced apart, looking along the main approach
    cx, cy = net.convertLonLat2XY((w + e) / 2, lat)
    cand = []
    for nd in net.getNodes():
        x, y = nd.getCoord()
        inc = [ed for ed in nd.getIncoming() if ed.getLength() > 40]
        if nd.getType() == "traffic_light" and inc and math.hypot(x - cx, y - cy) < radius * 0.72:
            cand.append((sum(ed.getLaneNumber() for ed in inc), nd, max(inc, key=lambda ed: ed.getLaneNumber())))
    cams = []
    for _, nd, ed in sorted(cand, key=lambda t: -t[0]):
        x, y = nd.getCoord()
        if len(cams) >= 8 or any(math.hypot(x - q[0], y - q[1]) < 380 for q in cams):
            continue
        (ax, ay), (bx, by) = ed.getShape()[-2:]
        d = math.hypot(bx - ax, by - ay) or 1
        ux, uy = (bx - ax) / d, (by - ay) / d
        names = [nm for nm in dict.fromkeys(html.unescape(i.getName() or "") for i in nd.getIncoming()) if nm]
        approaches = [{"id": i.getID(), "name": html.unescape(i.getName() or "")}
                      for i in nd.getIncoming() if i.getLength() > 25 and i.allows("passenger")]
        cams.append((x, y, bx - ux * 35, by - uy * 35, math.degrees(math.atan2(ux, uy)) % 360,
                     " / ".join(names[:2]), nd.getID(), approaches))
    dump("cameras.geojson", [{
        "type": "Feature",
        "properties": {"id": f"CAM-{i:02d}", "name": nm or "перекрёсток", "bearing": round(b), "target": ll(x, y),
                       "tls": tls, "approaches": appr},
        "geometry": {"type": "Point", "coordinates": ll(px, py)},
    } for i, (x, y, px, py, b, nm, tls, appr) in enumerate(cams, 1)])
