"""SNMPv3 authPriv: RFC 3414 key vectors and interoperation with an independent agent (pysnmp)."""
import asyncio
import hashlib
import socket
import threading
import time
import warnings

import pytest

from urban.equipment import snmp
from urban.equipment.adapter import AdapterError
from urban.equipment.ntcip import NtcipAdapter

SYS_CONTACT = "1.3.6.1.2.1.1.4.0"  # writable on the pysnmp agent
SYS_DESCR = "1.3.6.1.2.1.1.1.0"    # read-only: the agent answers notWritable


def test_rfc3414_password_to_key_vectors():
    ku = snmp.password_to_key("maplesyrup", hashlib.sha1)
    assert ku.hex() == "9fb5cc0381497b3793528939ff788d5d79145211"
    engine = bytes.fromhex("000000000000000000000002")
    assert snmp.localize(ku, engine, hashlib.sha1).hex() == "6695febc9288e36282235fc7151f128497b38f3f"
    md5 = snmp.password_to_key("maplesyrup", hashlib.md5)  # the MD5 vector checks the same expansion
    assert md5.hex() == "9faf3283884e92834ebc9847d8edd963"


def test_client_refuses_weak_settings():
    with pytest.raises(ValueError):
        snmp.SnmpV3Client("127.0.0.1", user="u", auth_password="short", priv_password="longenough")
    with pytest.raises(ValueError):
        snmp.SnmpV3Client("127.0.0.1", user="", auth_password="longenough", priv_password="longenough")
    with pytest.raises(ValueError):
        snmp.SnmpV3Client("127.0.0.1", user="u", auth_password="longenough", priv_password="longenough", auth="md5")


pysnmp = pytest.importorskip("pysnmp", reason="pysnmp is a test-only dependency (backend[dev])")


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _agent(auth: str):
    """An independent SNMPv3 agent with user 'urban' in authPriv, on a fresh port."""
    from pysnmp.carrier.asyncio.dgram import udp
    from pysnmp.entity import config, engine
    from pysnmp.entity.rfc3413 import cmdrsp, context
    port, ready = _free_port(), threading.Event()
    proto = {"sha": config.USM_AUTH_HMAC96_SHA, "sha256": config.USM_AUTH_HMAC192_SHA256}[auth]

    def run():
        warnings.simplefilter("ignore")
        asyncio.set_event_loop(asyncio.new_event_loop())
        e = engine.SnmpEngine()
        config.add_transport(e, udp.DOMAIN_NAME, udp.UdpTransport().open_server_mode(("127.0.0.1", port)))
        config.add_v3_user(e, "urban", proto, "auth-pass-123", config.USM_PRIV_CFB128_AES, "priv-pass-123")
        config.add_vacm_user(e, 3, "urban", "authPriv", (1, 3, 6), (1, 3, 6))
        ctx = context.SnmpContext(e)
        cmdrsp.GetCommandResponder(e, ctx)
        cmdrsp.SetCommandResponder(e, ctx)
        e.transport_dispatcher.job_started(1)
        ready.set()
        e.open_dispatcher()
    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    time.sleep(0.3)
    return port


def client(port, auth="sha", auth_pass="auth-pass-123", priv_pass="priv-pass-123"):
    return snmp.SnmpV3Client("127.0.0.1", port, user="urban", auth_password=auth_pass, priv_password=priv_pass,
                             auth=auth, timeout=1.0, retries=1)


@pytest.mark.parametrize("auth", ["sha", "sha256"])
def test_authenticated_encrypted_set_against_an_independent_agent(auth):
    port = _agent(auth)
    c = client(port, auth)
    c.set(SYS_CONTACT, 5)                       # discovery, then an authPriv SET the agent accepts
    assert c.engine and c.boots >= 1
    c.set(SYS_CONTACT, 6)                       # the same engine, a new IV
    with pytest.raises(OSError, match="refused the write: notWritable"):
        c.set(SYS_DESCR, 2)                     # an authenticated, decrypted refusal


def test_wrong_passwords_are_reported_not_ignored():
    port = _agent("sha")
    with pytest.raises(OSError, match="wrong digest|authentication"):
        client(port, auth_pass="not-the-password").set(SYS_CONTACT, 5)
    with pytest.raises(OSError):
        client(port, priv_pass="not-the-password").set(SYS_CONTACT, 5)


def test_clock_drift_is_resynchronised():
    port = _agent("sha")
    c = client(port)
    c.set(SYS_CONTACT, 5)
    c.etime += 1000                             # far outside the 150 s window
    c.set(SYS_CONTACT, 6)                       # the agent reports its time, the client retries once
    assert c.etime < 1000                       # back on the agent's clock


def test_ntcip_over_snmpv3_turns_a_refusal_into_an_adapter_error():
    port = _agent("sha")
    c = client(port)
    ad = NtcipAdapter(lambda tls, oid, value: c.set(oid, value), {"J1": {0, 2}})
    with pytest.raises(AdapterError, match="snmp"):
        ad.select("J1", 2)  # the agent has no NTCIP objects: no positive answer, so never taken as applied
