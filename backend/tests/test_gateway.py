from urban.gateway import POLICY, CommandGateway
from urban.gateway.audit import GENESIS, AuditLog


def make():
    sent = []
    return CommandGateway(sent.append), sent


def propose(gw, t=0, tls="J1", seconds=10):
    return gw.propose(t, "tls.extend_green", {"tls": tls, "approach": "e1", "seconds": seconds, "cam": "CAM-01"},
                      "queue", 9, street="Main")


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
    assert sent == []
    assert gw.applied == 0


def test_approval_sends_one_lease_and_renews_only_while_link_is_up():
    gw, sent = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    assert [m["op"] for m in sent] == ["lease"] and sent[0]["ttl"] == POLICY["tls.extend_green"].lease_ttl
    gw.on_equipment({"id": p["id"], "status": "applied", "seconds": 10}, 1)
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
    gw.on_equipment({"id": p["id"], "status": "applied", "seconds": 10}, 1)
    gw.tick(POLICY["tls.extend_green"].window, {})
    assert sent[-1] == {"op": "release", "id": p["id"]}


def test_policy_clamps_and_blocks():
    gw, _ = make()
    assert propose(gw, seconds=500)["seconds"] == POLICY["tls.extend_green"].max_seconds
    assert propose(gw) is None                                            # junction already has an open proposal
    assert gw.propose(0, "power.shutdown", {"tls": "J2", "approach": "e"}, "x", 1) is None
    assert gw.audit.records[-1]["event"] == "denied"


def test_cooldown_after_a_finished_command():
    gw, _ = make()
    p = propose(gw)
    gw.decide(p["id"], True, "operator", 0)
    gw.on_equipment({"id": p["id"], "status": "applied", "seconds": 10}, 1)
    gw.on_equipment({"id": p["id"], "status": "reverted", "reason": "released"}, 100)
    assert propose(gw, t=120) is None
    assert propose(gw, t=100 + POLICY["tls.extend_green"].cooldown) is not None


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


def test_shadow_proposals_are_recorded_but_never_sent():
    gw, sent = make()
    rec = gw.propose_shadow(1, "ref", "tls.preempt", "route", "note")
    assert rec["executed"] is False and rec["mode"] == "shadow" and sent == []


def test_pending_proposal_expires_without_a_decision():
    gw, sent = make()
    p = propose(gw)
    gw.tick(POLICY["tls.extend_green"].pending_ttl + 1, {})
    assert p["status"] == "expired" and sent == []
