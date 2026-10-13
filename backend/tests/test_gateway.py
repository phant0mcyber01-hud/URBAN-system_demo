from urban.gateway import POLICY, CommandGateway
from urban.gateway.audit import GENESIS, AuditLog

POL = POLICY["tls.select_plan"]
APPROVED = {"J1": {0, 1, 2, 3}, "J2": {0, 1, 2, 3}, "J9": {0, 1}}


def make():
    sent = []
    return CommandGateway(sent.append, APPROVED), sent


def propose(gw, t=0, tls="J1", plan=2):
    return gw.propose(t, "tls.select_plan", {"tls": tls, "cam": "CAM-01", "plan": plan, "shift": 10, "give": 0, "take": 2,
                                             "street": "Main", "from_street": "Side", "queue": 62, "other": 8}, "reason")


def applied(gw, p, t=1):
    gw.on_equipment({"id": p["id"], "status": "applied", "plan": p["plan"]}, t)


def test_audit_chain_links_and_detects_tampering():
    log = AuditLog()
    a = log.append(1, "proposed", "x", "c", "t")
    b = log.append(2, "approved", "x", "c", "t", actor="operator")
    assert a["prev"] == GENESIS and b["prev"] == a["hash"] and log.verify()
    log.records[0]["target"] = "other"
    assert not log.verify()


def test_nothing_is_sent_without_an_operator_decision():
    gw, sent = make()
    assert propose(gw)["status"] == "pending"
    gw.tick(30, {})
    assert sent == [] and gw.applied == 0


def test_approval_sends_the_plan_number_and_renews_only_while_link_is_up():
    gw, sent = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    assert sent == [{"op": "lease", "id": p["id"], "tls": "J1", "plan": 2, "ttl": POL.lease_ttl}]
    applied(gw, p)
    gw.tick(POL.lease_ttl // 3 - 1, {})
    assert len(sent) == 1                      # too early to renew
    gw.tick(POL.lease_ttl // 3, {})
    assert len(sent) == 2                      # renewed
    gw.set_link(False, "operator", POL.lease_ttl // 3 + 1)
    gw.tick(POL.lease_ttl, {})
    assert len(sent) == 2                      # silence: equipment must revert by itself


def test_approval_window_ends_with_release():
    gw, sent = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    applied(gw, p)
    gw.tick(POL.window, {})
    assert sent[-1] == {"op": "release", "id": p["id"]}


def test_only_approved_plans_of_known_commands_can_be_proposed():
    gw, _ = make()
    assert propose(gw, plan=7) is None                            # not in the table of this junction
    assert gw.audit.records[-1]["event"] == "denied"
    assert propose(gw, plan=0) is None                            # the base plan is not something to propose
    assert propose(gw, tls="UNKNOWN", plan=1) is None
    assert gw.propose(0, "power.shutdown", {"tls": "J2", "plan": 1}, "x") is None
    assert propose(gw) is not None
    assert propose(gw, plan=3) is None                            # junction already has an open proposal


def test_a_gateway_without_a_plan_table_refuses_everything():
    assert CommandGateway().propose(0, "tls.select_plan", {"tls": "J1", "plan": 1}, "x") is None


def test_cooldown_after_finish_reject_or_expiry():
    gw, _ = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    applied(gw, p)
    gw.on_equipment({"id": p["id"], "status": "reverted", "reason": "released"}, 100)
    assert propose(gw, t=120) is None
    q = propose(gw, t=100 + POL.cooldown)
    gw.decide(q["id"], False, "operator", 400)
    assert propose(gw, t=410) is None                             # a rejected junction is left alone
    r = propose(gw, t=0, tls="J9", plan=1)
    gw.tick(POL.pending_ttl + 1, {})
    assert r["status"] == "expired" and propose(gw, t=POL.pending_ttl + 2, tls="J9", plan=1) is None


def test_rejection_holds_until_the_problem_clears():
    gw, _ = make()
    p = propose(gw)
    gw.decide(p["id"], False, "operator", 10)
    gw.keep_muted({"J1"})                                         # the imbalance is still there
    assert propose(gw, t=10 + POL.cooldown + 1) is None
    gw.keep_muted(set())                                          # it cleared: the junction may be raised again
    assert propose(gw, t=10 + POL.cooldown + 2) is not None


def test_cannot_approve_while_link_is_down_or_twice():
    gw, sent = make()
    p = propose(gw)
    gw.set_link(False, "operator", 1)
    assert gw.decide(p["id"], True, "operator", 2) is None
    gw.set_link(True, "operator", 3)
    assert gw.decide(p["id"], True, "operator", 4) is not None
    assert gw.decide(p["id"], True, "operator", 5) is None
    assert len(sent) == 1


def test_kill_switch_releases_everything_and_rejects_pending():
    gw, sent = make()
    a, b = propose(gw, tls="J1"), propose(gw, tls="J2")
    gw.decide(a["id"], True, "operator", 0)
    gw.revoke_all("operator", 5)
    assert {"op": "release", "id": a["id"]} in sent
    assert b["status"] == "rejected" and a["status"] == "revoking"
    gw.keep_muted({"J2"})
    assert propose(gw, t=6, tls="J2") is None                     # the kill switch is a "no" for pending ones too


def test_early_release_needs_no_approval_and_keeps_its_reason():
    gw, sent = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    applied(gw, p)
    gw.release(p["id"], "relieved", 120)
    assert sent[-1] == {"op": "release", "id": p["id"]}
    gw.on_equipment({"id": p["id"], "status": "reverted", "reason": "released"}, 121)
    assert p["status"] == "done" and p["result"]["reason"] == "relieved"
    gw.release(p["id"], "again", 130)                             # nothing left to release
    assert len([m for m in sent if m["op"] == "release"]) == 1


def test_shadow_proposals_are_recorded_but_never_sent():
    gw, sent = make()
    rec = gw.propose_shadow(1, "ref", "tls.preempt", "route", "note")
    assert rec["executed"] is False and rec["mode"] == "shadow" and sent == []


def test_pending_proposal_expires_without_a_decision():
    gw, sent = make()
    p = propose(gw)
    gw.tick(POL.pending_ttl + 1, {})
    assert p["status"] == "expired" and sent == []
