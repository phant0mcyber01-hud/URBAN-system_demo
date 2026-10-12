from urban.gateway import POLICY, CommandGateway
from urban.gateway.audit import GENESIS, AuditLog

POL = POLICY["tls.rebalance"]


def make():
    sent = []
    return CommandGateway(sent.append), sent


def propose(gw, t=0, tls="J1", seconds=10, give=0, take=2):
    return gw.propose(t, "tls.rebalance", {"tls": tls, "cam": "CAM-01", "give": give, "take": take, "seconds": seconds,
                                           "street": "Main", "from_street": "Side", "queue": 9.0, "other": 1.0}, "reason")


def applied(gw, p, t=1, seconds=10):
    gw.on_equipment({"id": p["id"], "status": "applied", "seconds": seconds}, t)


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


def test_approval_sends_one_lease_and_renews_only_while_link_is_up():
    gw, sent = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    assert [m["op"] for m in sent] == ["lease"]
    assert sent[0]["ttl"] == POL.lease_ttl and (sent[0]["give"], sent[0]["take"]) == (0, 2)
    applied(gw, p)
    gw.tick(10, {})
    assert len(sent) == 1                      # too early to renew
    gw.tick(25, {})
    assert len(sent) == 2                      # renewed
    gw.set_link(False, "operator", 26)
    gw.tick(60, {})
    assert len(sent) == 2                      # silence: equipment must revert by itself


def test_approval_window_ends_with_release():
    gw, sent = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    applied(gw, p)
    gw.tick(POL.window, {})
    assert sent[-1] == {"op": "release", "id": p["id"]}


def test_policy_clamps_and_blocks():
    gw, _ = make()
    assert propose(gw, seconds=500)["seconds"] == POL.max_seconds
    assert propose(gw) is None                                    # junction already has an open proposal
    assert gw.propose(0, "power.shutdown", {"tls": "J2", "give": 0, "take": 2}, "x") is None
    assert gw.audit.records[-1]["event"] == "denied"
    assert propose(gw, tls="J3", give=0, take=0) is None          # needs two different phases


def test_cooldown_after_finish_reject_or_expiry():
    gw, _ = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    applied(gw, p)
    gw.on_equipment({"id": p["id"], "status": "reverted", "reason": "released"}, 100)
    assert propose(gw, t=120) is None
    q = propose(gw, t=100 + POL.cooldown)
    gw.decide(q["id"], False, "operator", 200)
    assert propose(gw, t=210) is None                             # a rejected junction is left alone for a while


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
