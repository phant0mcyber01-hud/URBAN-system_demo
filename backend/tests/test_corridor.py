"""Green corridor: route -> junctions and phases, one decision, precedence over rebalancing, release behind the unit."""
from types import SimpleNamespace

from urban.corridor import COMMAND, Corridors
from urban.gateway import CommandGateway


def junction(tls, approaches):
    """approaches: [(edge, phase)]; plans give each phase +5/+10/+15 as the pipeline does."""
    phases = sorted({ph for _, ph in approaches})
    other = {phases[0]: phases[-1], phases[-1]: phases[0]}
    plans = [{"id": 0, "splits": {}}]
    pid = 1
    for ph in phases:
        for shift in (5, 10, 15):
            plans.append({"id": pid, "give": ph, "take": other[ph], "shift": shift, "splits": {}})
            pid += 1
    return {"id": f"J-{tls}", "tls": tls, "name": tls, "plans": plans,
            "phases": [{"index": ph, "edges": [e for e, p in approaches if p == ph], "streets": [f"{e} street" for e, p in approaches if p == ph]}
                       for ph in phases]}


J = [junction("A", [("a_in", 0), ("a_side", 2)]), junction("B", [("b_in", 2), ("b_side", 0)]),
     junction("C", [("c_in", 0), ("c_side", 2)])]
ROUTE = ["start", "a_in", "ab", "b_in", "bc", "elsewhere"]  # crosses A then B, not C


def gateway():
    return CommandGateway(plans={j["tls"]: {p["id"] for p in j["plans"]} for j in J})


def dispatch(rid="emg-1"):
    return {"id": rid, "type": "dispatch", "status": "en_route", "t": 10, "unit": "Скорая", "route": ROUTE}


def test_route_to_junctions_with_the_most_green_for_its_approach():
    stops = Corridors(J).stops(ROUTE)
    assert [(i, j["tls"], ph, plan["shift"]) for i, j, ph, _, plan in stops] == [(1, "A", 0, 15), (3, "B", 2, 15)]
    assert stops[1][4]["give"] == 2 and Corridors(J).stops(["nowhere"]) == []


def test_corridor_is_proposed_as_one_group_and_applied_only_on_decision():
    gw, cor = gateway(), Corridors(J)
    made = cor.open(gw, dispatch(), 10, "Mustaqillik")
    assert [p["tls"] for p in made] == ["A", "B"] and all(p["command"] == COMMAND for p in made)
    assert "Mustaqillik" in made[0]["reason"]
    pub = {p["id"]: p for p in gw.public()}
    assert {pub[p["id"]]["group"] for p in made} == {"emg-1"} and pub[made[0]["id"]]["group_size"] == 2
    assert all(p["status"] == "pending" for p in made)  # nothing happens without the operator


def test_corridor_overrides_rebalancing_and_releases_behind_the_unit():
    sent = []
    gw = CommandGateway(send=sent.append, plans={j["tls"]: {p["id"] for p in j["plans"]} for j in J})
    rebal = gw.propose(0, "tls.select_plan", {"tls": "A", "plan": 4, "give": 2, "take": 0, "street": "x"}, "queue")
    gw.decide(rebal["id"], True, "ops", 1)
    cor = Corridors(J)
    made = cor.open(gw, dispatch(), 10)
    assert len(made) == 2                                   # A gets a corridor proposal despite the rebalancing
    gw.decide_many([p["id"] for p in made], True, "ops", 12)
    assert rebal["status"] == "revoking" and rebal["end"] == "corridor"
    ops = [(m["op"], m["id"]) for m in sent]
    assert ops.index(("release", rebal["id"])) < ops.index(("lease", made[0]["id"]))  # released before the lease

    cor.follow(gw, {"emg-1": "a_in"}, 20)                   # on A's approach: nothing yet
    assert made[0]["status"] == "approved"
    cor.follow(gw, {"emg-1": ":A_0_0"}, 21)                 # inside the junction: still nothing
    cor.follow(gw, {"emg-1": "ab"}, 22)                     # past A
    assert made[0]["status"] == "revoking" and made[0]["end"] == "passed" and made[1]["status"] == "approved"
    cor.close(gw, "emg-1", "arrived", 30)
    assert made[1]["status"] == "revoking" and made[1]["end"] == "arrived" and not cor.active


def test_rejecting_a_corridor_does_not_mute_the_junction_and_pending_ones_are_withdrawn():
    gw, cor = gateway(), Corridors(J)
    made = cor.open(gw, dispatch(), 10)
    gw.decide(made[0]["id"], False, "ops", 11)
    assert gw.muted == set()
    cor.follow(gw, {"emg-1": "bc"}, 15)                    # passed both before anyone decided on B
    assert made[1]["status"] == "expired" and gw.records[-1]["event"] == "withdrawn"
    assert gw.propose(16, "tls.select_plan", {"tls": "A", "plan": 1, "give": 0, "take": 2, "street": "x"}, "q") is not None


def test_none_through_a_locked_unit():
    gw, cor = gateway(), Corridors(J)
    gw.locked.add("B")
    assert [p["tls"] for p in cor.open(gw, dispatch("emg-1"), 10)] == ["A"]


def test_units_on_the_same_approach_share_the_green_until_the_last_has_passed():
    gw, cor = gateway(), Corridors(J)
    first = cor.open(gw, dispatch("emg-1"), 10)
    assert cor.open(gw, dispatch("emg-2"), 11) == []           # same route: rides along, no new proposals
    gw.decide_many([p["id"] for p in first], True, "ops", 12)
    cor.follow(gw, {"emg-1": "ab"}, 20)                        # the first is past A, the second is not
    assert first[0]["status"] == "approved"
    cor.follow(gw, {"emg-2": "ab"}, 25)
    assert first[0]["status"] == "revoking" and first[0]["end"] == "passed"


def test_crossing_units_take_turns_at_a_shared_junction():
    gw, cor = gateway(), Corridors(J)
    one = {**dispatch("emg-1"), "route": ["start", "a_in", "ab"]}            # through A on phase 0
    two = {**dispatch("emg-2"), "route": ["x", "a_side", "ay"], "unit": "ДПС"}  # through A on phase 2
    first = cor.open(gw, one, 10)
    assert cor.open(gw, two, 11) == []                          # A is taken for the other approach: waits
    gw.decide(first[0]["id"], True, "ops", 12)
    cor.follow(gw, {"emg-1": "ab"}, 20)                         # the first has crossed A
    assert first[0]["status"] == "revoking"
    offered = [p for p in gw.proposals.values() if p.get("corridor") == "emg-2"]
    assert len(offered) == 1 and offered[0]["tls"] == "A" and offered[0]["give"] == 2 and offered[0]["status"] == "pending"
    cor.close(gw, "emg-2", "arrived", 25)
    assert offered[0]["status"] == "expired" and not cor.held


def test_platform_opens_and_follows_a_corridor_from_simulation_events():
    from urban.platform import Platform
    cams = [dict(j, camera=False, lon=71.78, lat=40.38, approaches=[]) for j in J]
    p = Platform([71.78, 40.38], 1000, cams)
    veh = lambda edge: SimpleNamespace(id="emg-1", lon=71.78, lat=40.38, angle=0, speed=10, kind="emg", edge=edge)
    frame = p.handle({"t": 10, "vehicles": [], "events": [dispatch()], "bus_loss": []})
    assert "route" not in frame["events"][0]                 # the route stays on the server
    ids = [c["id"] for c in p.gateway.proposals.values() if c["command"] == COMMAND]
    p.gateway.decide_many(ids, True, "ops", 11)
    p.handle({"t": 12, "vehicles": [veh("ab")], "events": [], "bus_loss": []})
    assert p.gateway.proposals[ids[0]]["end"] == "passed"
    p.handle({"t": 14, "vehicles": [veh("bc")], "events": [{**dispatch(), "status": "arrived", "route": None}],
              "bus_loss": []})
    assert all(p.gateway.proposals[i]["status"] == "revoking" for i in ids)
