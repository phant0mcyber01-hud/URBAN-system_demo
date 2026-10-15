"""Proposals on one street are grouped and can be decided together, each still on its own terms."""
from urban.gateway import CommandGateway
from urban.gateway.gateway import street_groups, street_names


def prop(gw, tls, street, t=0):
    return gw.propose(t, "tls.select_plan", {"tls": tls, "plan": 2, "give": 0, "take": 1, "street": street}, "why")


def test_street_names_ignore_apostrophe_spelling_and_split_pairs():
    assert street_names("Tadbirkorlar koʻchasi, Universitet ko'chasi") == {"tadbirkorlar ko'chasi", "universitet ko'chasi"}
    assert street_names("Universitet koʻchasi") == street_names("Universitet ko‘chasi") == {"universitet ko'chasi"}
    assert street_names("") == set()


def test_groups_join_through_shared_streets_only():
    pending = [
        {"id": "cmd-1", "street": "Universitet koʻchasi"},
        {"id": "cmd-2", "street": "Tadbirkorlar koʻchasi, Universitet ko'chasi"},
        {"id": "cmd-3", "street": "Tadbirkorlar koʻchasi"},     # joins through cmd-2
        {"id": "cmd-4", "street": "Sayilgoh koʻchasi"},         # alone: no group
        {"id": "cmd-5", "street": ""},
    ]
    g = street_groups(pending)
    assert set(g) == {"cmd-1", "cmd-2", "cmd-3"}
    assert {v["group"] for v in g.values()} == {"cmd-1"} and g["cmd-3"]["group_size"] == 3
    assert g["cmd-1"]["group_street"] in ("Universitet koʻchasi", "Tadbirkorlar koʻchasi", "Universitet ko'chasi")


def test_public_marks_pending_groups_and_decide_many_records_each():
    gw = CommandGateway(plans={"A": {2}, "B": {2}, "C": {2}}, reachable=lambda tls: tls != "C")
    a, b, c = prop(gw, "A", "Universitet koʻchasi"), prop(gw, "B", "Universitet ko'chasi"), prop(gw, "C", "Universitet koʻchasi")
    marked = {p["id"]: p for p in gw.public()}
    assert marked[a["id"]]["group_size"] == 3 and marked[a["id"]]["group"] == marked[c["id"]]["group"]
    assert "group" not in gw.proposals[a["id"]]  # the stored proposal is not touched

    results = gw.decide_many([a["id"], b["id"], c["id"], a["id"], "nope"], True, "ops", 5)
    assert results == {a["id"]: "approved", b["id"]: "approved", c["id"]: None, "nope": None}  # C is offline
    events = [r["event"] for r in gw.records]
    assert events.count("approved") == 2 and events[-1] == "group-approved"
    assert gw.records[-1]["ref"] == f"{a['id']},{b['id']}" and gw.records[-1]["actor"] == "ops"
    assert "group" not in {k for p in gw.public() if p["status"] != "pending" for k in p}


def test_rejecting_a_group_mutes_every_junction_in_it():
    gw = CommandGateway(plans={"A": {2}, "B": {2}})
    a, b = prop(gw, "A", "X ko'chasi"), prop(gw, "B", "X koʻchasi")
    gw.decide_many([a["id"], b["id"]], False, "ops", 3)
    assert gw.muted == {"A", "B"} and gw.records[-1]["event"] == "group-rejected"
