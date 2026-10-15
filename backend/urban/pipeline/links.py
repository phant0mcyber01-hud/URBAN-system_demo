"""Which junction feeds which: for every green phase, the approaches of other junctions its traffic reaches next.

Traffic released by phase p of junction A drives off along A's outgoing edges; the first approach of
another signalised junction it meets (within REACH metres) is where it will queue next. That is the
downstream of (A, p). The platform uses it to avoid pushing cars into a block that is already full.

Written into signals.json as phases[*].downstream = [{"junction": id, "approach": edge id, "phase": index}].
    python -m urban.pipeline --links    adds it to an existing signals.json without rebuilding anything else
"""
REACH = 400.0  # metres of road searched beyond the junction


def downstream(net, junctions: list[dict]) -> int:
    """Adds `downstream` to every phase. Returns the number of links found."""
    approach_of = {}  # edge id -> (junction id, phase index)
    for j in junctions:
        for ph in j["phases"]:
            for e in ph["edges"]:
                approach_of[e] = (j["id"], ph["index"])
    found = 0
    for j in junctions:
        for ph in j["phases"]:
            seen, out = set(), {}
            for start in ph["edges"]:
                try:
                    edge = net.getEdge(start)
                except KeyError:
                    continue
                frontier = [(nxt, 0.0) for nxt in edge.getOutgoing()]  # the movements this approach can make
                while frontier:
                    e, dist = frontier.pop()
                    if e.getID() in seen or dist > REACH:
                        continue
                    seen.add(e.getID())
                    hit = approach_of.get(e.getID())
                    if hit is not None and hit[0] != j["id"]:
                        out[e.getID()] = {"junction": hit[0], "approach": e.getID(), "phase": hit[1]}
                        continue  # stop at the first signalised junction on this way
                    if not e.allows("passenger"):
                        continue
                    frontier += [(n, dist + e.getLength()) for n in e.getOutgoing()]
            ph["downstream"] = sorted(out.values(), key=lambda d: (d["junction"], d["approach"]))
            found += len(ph["downstream"])
    return found
