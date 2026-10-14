"""Fixes from the full product review: untrusted health fields, account reload, chain checks, retention,
SNMP robustness, NTCIP re-arming, endpoint parsing, field-mode honesty."""
import json
import time

import pytest

from urban.accounts import Accounts
from urban.devices.health import TEXT_MAX, assess, clean
from urban.devices.hub import DeviceHub
from urban.edge.__main__ import parse_endpoint
from urban.equipment import snmp
from urban.equipment.ntcip import SYSTEM_PATTERN_CONTROL, UNIT_BACKUP_TIME, NtcipAdapter
from urban.gateway.audit import AuditLog
from urban.store import Store


def test_health_is_reduced_to_numbers_and_short_words():
    dirty = {"temp": "<img src=x onerror=1>", "leases": "<b>", "door": "<s>", "controller": "x" * 500,
             "version": {"a": 1}, "uptime": True, "extra": [1, 2]}
    assert clean(dirty) == {"controller": "x" * TEXT_MAX}
    assert clean({"temp": 41.26, "leases": 1, "door": "open", "controller": "ok", "version": "0.1.0", "uptime": 5}) == \
        {"temp": 41.3, "leases": 1, "door": "open", "controller": "ok", "version": "0.1.0", "uptime": 5}
    assert clean("nonsense") == {} and clean({"temp": float("nan")}) == {}
    row = {"id": "e", "status": "active", "online": True, "age": 1, "health": clean(dirty)}
    assert [a["kind"] for a in assess(row)] == ["controller"]


def test_hub_keeps_the_last_health_across_a_reconnect():
    hub = DeviceHub.__new__(DeviceHub)
    hub.seen = {"edge-1": {"at": 1.0, "health": {"door": "open", "temp": 70.0, "version": "0.1.0"}}}
    seen = hub.reconnected("edge-1", "0.1.0")
    assert seen["health"] == {"door": "open", "temp": 70.0, "version": "0.1.0"} and seen["at"] > 1.0
    assert hub.reconnected("edge-2", None)["health"] == {}


def test_accounts_pick_up_changes_made_by_another_process(tmp_path):
    path = tmp_path / "users.json"
    server = Accounts(path)
    server.create("ops", "first password here", "operator")
    session = server.sign_in("ops", "first password here")
    time.sleep(0.01)
    cli = Accounts(path)                      # python -m urban.users in another process
    cli.create("newbie", "another long password", "viewer")
    cli.set_password("ops", "second password here")
    assert server.sign_in("newbie", "another long password") is not None   # seen without a restart
    assert server.session(session["token"]) is None                         # old password: session ended
    assert server.sign_in("ops", "second password here") is not None
    time.sleep(0.01)
    cli.set_enabled("newbie", False)
    assert server.sign_in("newbie", "another long password") is None


def test_check_verifies_a_password_without_a_session_and_throttles(tmp_path):
    a = Accounts(tmp_path / "users.json")
    a.create("ops", "correct horse battery", "operator")
    assert a.check("ops", "correct horse battery") and not a.check("ops", "wrong")
    for _ in range(5):
        a.check("ops", "wrong")
    assert not a.check("ops", "correct horse battery") and a.sessions == {}


def test_chain_is_checked_incrementally_and_fully(tmp_path):
    clock = [1000.0]
    s = Store(tmp_path / "h.db", clock=lambda: clock[0])
    assert s.chain["intact"] is None and s.chain["records"] == 0
    log = AuditLog(store=s)
    log.append(1, "proposed", "c1", "tls.select_plan", "J")
    log.append(2, "approved", "c1", "tls.select_plan", "J", actor="ops")
    assert s.audit_check()["intact"] and s.chain["records"] == 2 and s.chain["checked"] == 1000.0
    log.append(3, "released", "c1", "tls.select_plan", "J")
    assert s.audit_check()["records"] == 3
    row = json.loads(s.db.execute("SELECT data FROM audit WHERE seq = 1").fetchone()[0])
    row["note"] = "edited"
    s.db.execute("UPDATE audit SET data = ? WHERE seq = 1", (json.dumps(row),))
    assert s.audit_check()["intact"]              # the cheap check only walks new records
    assert s.audit_verify()["broken_at"] == 1     # the full check finds it
    assert s.audit_check()["intact"] is False     # and the verdict sticks


def test_samples_are_pruned_and_counts_are_cached(tmp_path):
    clock = [0.0]
    s = Store(tmp_path / "h.db", clock=lambda: clock[0], retention_days=1)
    s.add_queues({"J1": {"a": 1}})
    s.add_device_samples([{"id": "e", "online": True}])
    clock[0] = 2 * 86400.0
    s.add_queues({"J1": {"a": 2}})
    assert [q["metres"] for q in s.queues("J1", 0)] == [2] and s.device_samples("e", 0) == []
    first = s.counts()
    s.add_queues({"J1": {"a": 3}})
    assert s.counts() is first                     # cached
    clock[0] += 60
    assert s.counts()["queues"] == 2


def test_truncated_snmp_answers_are_refused_not_fatal():
    with pytest.raises(ValueError):
        snmp.decode_response(b"\x30\x05\x02")
    with pytest.raises(ValueError):
        snmp.decode_response(b"\x30\x82")
    assert snmp.decode_response(snmp._tlv(snmp.SEQUENCE, snmp._int(1) + snmp._tlv(snmp.OCTET_STRING, b"p")
                                          + snmp._tlv(snmp.GET_RESPONSE, snmp._int(7) + snmp._int(0) + snmp._int(0)
                                                      + snmp._tlv(snmp.SEQUENCE, b"")))) == (7, 0)


def test_ntcip_rearms_the_backup_timer_on_every_write():
    sets = []
    ad = NtcipAdapter(lambda tls, oid, v: sets.append(oid), {"J1": {0, 2}}, backup_time=120)
    ad.select("J1", 2)
    ad.keepalive("J1", 2)
    assert sets == [UNIT_BACKUP_TIME, SYSTEM_PATTERN_CONTROL, UNIT_BACKUP_TIME, SYSTEM_PATTERN_CONTROL]


def test_endpoint_parsing_handles_ipv6():
    assert parse_endpoint("192.168.10.2:161", 161) == ("192.168.10.2", 161)
    assert parse_endpoint("controller.local", 161) == ("controller.local", 161)
    assert parse_endpoint("[fd00::2]:1161", 161) == ("fd00::2", 1161)
    assert parse_endpoint("[fd00::2]", 161) == ("fd00::2", 161)
    assert parse_endpoint("fd00::2", 161) == ("fd00::2", 161)
