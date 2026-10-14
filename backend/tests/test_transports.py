"""Controller transports without hardware: SNMP against a fake controller, Modbus RTU against a fake board."""
import socket
import threading

import pytest

from urban.equipment import modbus, snmp
from urban.equipment.adapter import AdapterError
from urban.equipment.ntcip import SYSTEM_PATTERN_CONTROL, NtcipAdapter
from urban.equipment.relay import RelayAdapter


# ---- SNMP
def test_set_request_is_valid_ber():
    packet = snmp.encode_set("private", "1.3.6.1.4.1.1206.4.2.1.4.14.0", 3, 7, version=0)
    assert packet[0] == snmp.SEQUENCE and packet[1] == len(packet) - 2
    assert b"private" in packet
    # the OID: 1.3 -> 0x2B, then 6 1 4 1, 1206 -> 0x89 0x36, 4 2 1 4 14 0
    assert bytes([0x2B, 6, 1, 4, 1, 0x89, 0x36, 4, 2, 1, 4, 14, 0]) in packet
    assert snmp._int(127) == b"\x02\x01\x7f" and snmp._int(128) == b"\x02\x02\x00\x80" and snmp._int(0) == b"\x02\x01\x00"
    with pytest.raises(ValueError):
        snmp._oid("3.1")


def _fake_controller(answer_status, swallow=0):
    """A UDP controller that replies GetResponse with the same request id."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    seen = []

    def serve():
        for i in range(1 + swallow):
            data, peer = sock.recvfrom(1500)
            seen.append(data)
            if i < swallow:
                continue
            _, msg, _ = snmp._read(data, 0)
            _, _, at = snmp._read(msg, 0)
            _, _, at = snmp._read(msg, at)
            _, pdu, _ = snmp._read(msg, at)
            _, rid, _ = snmp._read(pdu, 0)
            reply = snmp._tlv(snmp.GET_RESPONSE, snmp._tlv(snmp.INTEGER, rid) + snmp._int(answer_status) + snmp._int(0)
                              + snmp._tlv(snmp.SEQUENCE, b""))
            sock.sendto(snmp._tlv(snmp.SEQUENCE, snmp._int(1) + snmp._tlv(snmp.OCTET_STRING, b"private") + reply), peer)
        sock.close()
    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1], seen


def test_snmp_client_sets_and_handles_refusal_and_silence():
    port, seen = _fake_controller(0)
    snmp.SnmpClient("127.0.0.1", port, timeout=1).set(SYSTEM_PATTERN_CONTROL, 3)
    assert len(seen) == 1 and seen[0][0] == snmp.SEQUENCE

    port, _ = _fake_controller(3)  # badValue
    with pytest.raises(OSError, match="badValue"):
        snmp.SnmpClient("127.0.0.1", port, timeout=1).set(SYSTEM_PATTERN_CONTROL, 99)

    port, seen = _fake_controller(0, swallow=1)  # the first request is lost, the retry succeeds
    snmp.SnmpClient("127.0.0.1", port, timeout=0.3, retries=2).set(SYSTEM_PATTERN_CONTROL, 2)
    assert len(seen) == 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))  # nobody answers here
    with pytest.raises(OSError, match="no answer"):
        snmp.SnmpClient("127.0.0.1", sock.getsockname()[1], timeout=0.2, retries=1).set(SYSTEM_PATTERN_CONTROL, 2)
    sock.close()


def test_ntcip_adapter_turns_transport_failures_into_adapter_errors():
    port, _ = _fake_controller(5)  # genErr
    client = snmp.SnmpClient("127.0.0.1", port, timeout=1)
    ad = NtcipAdapter(lambda tls, oid, v: client.set(oid, v), {})
    ad.learn("tls1", {0, 2})
    with pytest.raises(AdapterError, match="snmp"):
        ad.select("tls1", 2)


# ---- Modbus RTU
def test_modbus_frames():
    assert modbus.crc16(bytes.fromhex("01 03 00 00 00 0A")) == 0xCDC5  # the textbook example: ... C5 CD on the wire
    frame = modbus.write_coils(1, 0, [True, True, False])
    assert frame[:-2] == bytes.fromhex("01 0F 00 00 00 03 01 03") and modbus.crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")
    with pytest.raises(ValueError):
        modbus.write_coils(0, 0, [True])


class FakeBoard:
    def __init__(self, reply=None):
        self.frames, self.reply = [], reply

    def write(self, data):
        self.frames.append(bytes(data))

    def read(self, n):
        if self.reply is not None:
            return self.reply
        return modbus.with_crc(self.frames[-1][:6])  # echo, as a real board does


def test_relay_board_drives_the_plan_code_and_reports_failures():
    board = FakeBoard()
    ad = RelayAdapter(modbus.ModbusRelayBoard("", unit=2, transport=board).write, {})
    ad.learn("tls1", {0, 3, 5})
    ad.select("tls1", 5)  # 5 = 101 on three relays
    assert board.frames[-1][:8] == bytes.fromhex("02 0F 00 00 00 03 01 05")
    ad.base("tls1")
    assert board.frames[-1][:8] == bytes.fromhex("02 0F 00 00 00 03 01 00")

    silent = FakeBoard(reply=b"")
    ad = RelayAdapter(modbus.ModbusRelayBoard("", unit=1, transport=silent).write, {"tls1": {0, 1}})
    with pytest.raises(AdapterError, match="relay board"):
        ad.select("tls1", 1)
    refusing = FakeBoard(reply=modbus.with_crc(bytes([1, 0x8F, 2])))
    ad = RelayAdapter(modbus.ModbusRelayBoard("", unit=1, transport=refusing).write, {"tls1": {0, 1}})
    with pytest.raises(AdapterError, match="exception 2"):
        ad.select("tls1", 1)
