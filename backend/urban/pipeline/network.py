"""OSM -> SUMO network, passenger demand and public transport."""
import sys

from .. import config as c
from .paths import CARS, CFG, NET, OSM, PT, PTLINES, STOPS, run


def build_network():
    run([c.SUMO_BIN / "netconvert", "--osm-files", OSM, "-o", NET,
         "--type-files", c.SUMO_HOME / "data" / "typemap" / "osmNetconvert.typ.xml",
         "--keep-edges.by-vclass", "passenger,bus", "--remove-edges.isolated",
         "--geometry.remove", "--roundabouts.guess", "--junctions.join",
         "--tls.guess-signals", "--tls.default-type", "static", "--tls.discard-simple", "--tls.join",
         "--ptstop-output", STOPS, "--ptline-output", PTLINES,
         "--output.street-names", "--no-warnings"])


def build_demand():
    run([sys.executable, c.SUMO_TOOLS / "randomTrips.py", "-n", NET, "-e", c.SIM_END,
         "-p", c.CAR_PERIOD, "--fringe-factor", "10", "--vehicle-class", "passenger",
         "--prefix", "car", "--validate", "-o", c.DATA / "trips.xml", "-r", CARS, "--seed", "42"])

    PT.unlink(missing_ok=True)
    if PTLINES.exists() and "<ptLine " in PTLINES.read_text(encoding="utf-8"):
        run([sys.executable, c.SUMO_TOOLS / "ptlines2flows.py", "-n", NET, "-e", c.SIM_END,
             "-p", c.BUS_PERIOD, "--random-begin", "--seed", "42",
             "--ptstops", STOPS, "--ptlines", PTLINES, "-o", PT, "--ignore-errors"], check=False)
    if not PT.exists() or "<flow" not in PT.read_text(encoding="utf-8"):
        print("!! no usable OSM public transport routes in bbox -> synthetic demo bus lines")
        synth_pt()

    CFG.write_text(f"""<configuration>
  <input>
    <net-file value="{NET.name}"/>
    <route-files value="{CARS.name},{PT.name}"/>
    <additional-files value="{STOPS.name}"/>
  </input>
  <time><begin value="0"/><step-length value="1"/></time>
  <processing><time-to-teleport value="120"/><ignore-route-errors value="true"/></processing>
  <report><no-warnings value="true"/><no-step-log value="true"/></report>
</configuration>
""")


def synth_pt(n_lines=6, stop_every=400):
    """Demo bus lines along major streets (OSM has no route relations for this area)."""
    import math
    import sumolib

    net = sumolib.net.readNet(str(NET))
    major = [e for e in net.getEdges()
             if e.allows("bus") and e.getType().split(".")[-1] in ("trunk", "primary", "secondary", "tertiary")]
    srcs = [e for e in major if e.is_fringe() and not e.getIncoming()]
    dsts = [e for e in major if e.is_fringe() and not e.getOutgoing()]

    def dist(a, b):
        (x0, y0), (x1, y1) = a.getFromNode().getCoord(), b.getToNode().getCoord()
        return math.hypot(x1 - x0, y1 - y0)

    pairs = sorted(((dist(a, b), a, b) for a in srcs for b in dsts), key=lambda p: -p[0])
    lines, used_src, used_dst = [], set(), set()
    for _, a, b in pairs:
        if len(lines) >= n_lines:
            break
        if a in used_src or b in used_dst:
            continue
        path, _cost = net.getShortestPath(a, b, vClass="bus")
        if path:
            lines.append(path)
            used_src.add(a)
            used_dst.add(b)

    stops, routes = {}, []
    for i, path in enumerate(lines, 1):
        acc, on_route = stop_every / 2, []
        for e in path:
            lane = e.getLanes()[0]
            acc += e.getLength()
            if acc >= stop_every and e.getLength() >= 40 and lane.allows("bus"):
                sid = stops.setdefault(lane.getID(), f"bs_{len(stops) + 1}")
                on_route.append(sid)
                acc = 0
        st = "".join(f'<stop busStop="{s}" duration="20"/>' for s in on_route)
        edges = " ".join(e.getID() for e in path)
        routes.append(f'  <route id="line{i}" edges="{edges}">{st}</route>')
        routes.append(f'  <flow id="bus_L{i}" type="bus" route="line{i}" line="{i}" begin="{i * 10}" '
                      f'end="{c.SIM_END}" period="{c.BUS_PERIOD}"/>')

    with open(STOPS, "w", encoding="utf-8") as f:
        print("<additional>", file=f)
        for lane_id, sid in stops.items():
            mid = net.getLane(lane_id).getLength() / 2
            print(f'  <busStop id="{sid}" lane="{lane_id}" startPos="{mid - 7.5:.1f}" endPos="{mid + 7.5:.1f}"/>',
                  file=f)
        print("</additional>", file=f)
    with open(PT, "w", encoding="utf-8") as f:
        for line in ("<routes>", '  <vType id="bus" vClass="bus"/>', *routes, "</routes>"):
            print(line, file=f)
    print(f"   synthetic lines: {len(lines)}, stops: {len(stops)}")
