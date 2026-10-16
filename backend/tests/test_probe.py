"""The probe: who is the controller, what does it speak, which adapter - without writing anything."""
import socket
import threading

from test_contact_buses import FakeModbusTcp
from urban.equipment import snmp
from urban.equipment.ntcip import SYSTEM_PATTERN_CONTROL, UNIT_BACKUP_TIME
from urban.equipment.probe import enterprise, probe


def snmp_agent(objects: dict):
    """A v1/v2c agent answering GetRequests from `objects` (oid -> int, str, or ("oid", dotted)); records requests."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    seen = []

    def value(v):
        if v is None:
            return snmp._tlv(0x80, b"")  # noSuchObject
        if isinstance(v, tuple):
            return snmp._oid(v[1])
        if isinstance(v, str):
            return snmp._tlv(snmp.OCTET_STRING, v.encode())
        return snmp._int(v)

    def serve():
        while True:
            try:
                data, peer = sock.recvfrom(1500)
            except OSError:
                return
            _, msg, _ = snmp._read(data, 0)
            _, version, at = snmp._read(msg, 0)
            _, community, at = snmp._read(msg, at)
            tag, pdu, _ = snmp._read(msg, at)
            _, rid, at = snmp._read(pdu, 0)
            _, _, at = snmp._read(pdu, at)
            _, _, at = snmp._read(pdu, at)
            _, binds, _ = snmp._read(pdu, at)
            _, bind, _ = snmp._read(binds, 0)
            _, oid_body, _ = snmp._read(bind, 0)
            oid = snmp._decode_oid(oid_body)
            seen.append((tag, oid))
            vb = snmp._tlv(snmp.SEQUENCE, snmp._oid(oid) + value(objects.get(oid)))
            reply = snmp._tlv(snmp.GET_RESPONSE, snmp._tlv(snmp.INTEGER, rid) + snmp._int(0) + snmp._int(0)
                              + snmp._tlv(snmp.SEQUENCE, vb))
            sock.sendto(snmp._tlv(snmp.SEQUENCE, snmp._tlv(snmp.INTEGER, version) + snmp._tlv(snmp.OCTET_STRING, community)
                                  + reply), peer)
    threading.Thread(target=serve, daemon=True).start()
    return sock, seen


def test_an_ntcip_controller_is_recognised_and_nothing_is_written():
    agent, seen = snmp_agent({"1.3.6.1.2.1.1.1.0": "ASC/3 traffic controller fw 4.2",
                              "1.3.6.1.2.1.1.2.0": ("oid", "1.3.6.1.4.1.1206.3.21.2.2"),
                              UNIT_BACKUP_TIME: 0, SYSTEM_PATTERN_CONTROL: 0})
    try:
        client = snmp.SnmpClient("127.0.0.1", agent.getsockname()[1], community="public", timeout=1, retries=0)
        report = probe("127.0.0.1", client, modbus_port=None)
    finally:
        agent.close()
    s = report["snmp"]
    assert s["descr"].startswith("ASC/3") and s["enterprise"] == 1206 and s["ntcip_1202"] is True
    assert report["recommend"]["adapter"] == "ntcip"
    assert all(tag == 0xA0 for tag, _ in seen)  # GetRequest only: a probe never sets anything


def test_snmp_without_ntcip_and_a_modbus_device():
    agent, _ = snmp_agent({"1.3.6.1.2.1.1.1.0": "Vendor controller", "1.3.6.1.2.1.1.2.0": ("oid", "1.3.6.1.4.1.99999.1")})
    dev = FakeModbusTcp(identity=["Some Vendor", "TSC-8", "2.1"])
    try:
        client = snmp.SnmpClient("127.0.0.1", agent.getsockname()[1], community="public", timeout=1, retries=0)
        report = probe("127.0.0.1", client, modbus_port=dev.port, timeout=1)
    finally:
        agent.close()
        dev.close()
    assert report["snmp"]["ntcip_1202"] is False and report["snmp"]["enterprise"] == 99999
    assert "look up 99999" in report["snmp"]["enterprise_name"]  # no guessing about vendors we do not know
    assert report["modbus"]["identity"] == {"vendor": "Some Vendor", "product": "TSC-8", "revision": "2.1"}
    assert report["recommend"]["adapter"] == "profile"


def test_a_silent_controller_gets_contacts():
    free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    client = snmp.SnmpClient("127.0.0.1", port, timeout=0.3, retries=0)
    report = probe("127.0.0.1", client, modbus_port=1, timeout=0.3)
    assert report["snmp"]["answers"] is False and report["modbus"]["answers"] is False
    assert report["recommend"]["adapter"] == "contacts"
    assert enterprise("1.3.6.1.4.1.1206.4") == 1206 and enterprise("1.3.6.1.2.1") is None
