"""The smallest SNMP that NTCIP needs: one SetRequest of one INTEGER, SNMPv1 or v2c, over UDP.

A traffic controller in a cabinet sits on an isolated point-to-point link with the URBAN Edge
unit, which is why the community string of SNMPv1/v2c is acceptable there; SNMPv3 with
authentication is the step for shared networks. Encoding is BER by hand so the unit needs no
SNMP library. Not tried on a real controller.
"""
import secrets
import socket

INTEGER, OCTET_STRING, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
SET_REQUEST, GET_RESPONSE = 0xA3, 0xA2
ERRORS = {1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr", 6: "noAccess",
          17: "notWritable", 18: "inconsistentName"}


# ---- encoding
def _tlv(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        return bytes([tag, n]) + body
    size = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(size)]) + size + body


def _int(value: int) -> bytes:
    n = max(1, (value.bit_length() + 8) // 8)  # one spare bit for the sign
    return _tlv(INTEGER, value.to_bytes(n, "big", signed=True))


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.strip(".").split(".")]
    if len(parts) < 2 or parts[0] > 2 or parts[1] > 39:
        raise ValueError(f"bad oid {dotted!r}")
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(OID, bytes(out))


def encode_set(community: str, oid: str, value: int, request_id: int, version: int = 1) -> bytes:
    """A SetRequest message. version: 0 = SNMPv1, 1 = SNMPv2c."""
    binding = _tlv(SEQUENCE, _oid(oid) + _int(value))
    pdu = _tlv(SET_REQUEST, _int(request_id) + _int(0) + _int(0) + _tlv(SEQUENCE, binding))
    return _tlv(SEQUENCE, _int(version) + _tlv(OCTET_STRING, community.encode()) + pdu)


# ---- decoding, only as far as the response status
def _read(data: bytes, at: int) -> tuple[int, bytes, int]:
    """(tag, body, index after) of the TLV at `at`."""
    tag, n, at = data[at], data[at + 1], at + 2
    if n & 0x80:
        size = n & 0x7F
        n = int.from_bytes(data[at:at + size], "big")
        at += size
    return tag, data[at:at + n], at + n


def decode_response(data: bytes) -> tuple[int, int]:
    """(request id, error status) of a GetResponse; ValueError if it is not one."""
    tag, msg, _ = _read(data, 0)
    if tag != SEQUENCE:
        raise ValueError("not an SNMP message")
    _, _, at = _read(msg, 0)        # version
    _, _, at = _read(msg, at)       # community
    tag, pdu, _ = _read(msg, at)
    if tag != GET_RESPONSE:
        raise ValueError("not a response")
    _, rid, at = _read(pdu, 0)
    _, status, _ = _read(pdu, at)
    return int.from_bytes(rid, "big", signed=True), int.from_bytes(status, "big", signed=True)


class SnmpClient:
    """Sets one integer object at a time and waits for the controller to confirm it."""

    def __init__(self, host: str, port: int = 161, community: str = "private", version: int = 1,
                 timeout: float = 2.0, retries: int = 2):
        self.address, self.community, self.version = (host, port), community, version
        self.timeout, self.retries = timeout, retries

    def set(self, oid: str, value: int) -> None:
        """Raises OSError when the controller does not answer or refuses the write."""
        request_id = secrets.randbelow(0x7FFFFFFF)
        packet = encode_set(self.community, oid, value, request_id, self.version)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            last = None
            for _ in range(1 + self.retries):
                sock.sendto(packet, self.address)
                try:
                    data, _ = sock.recvfrom(1500)
                    rid, status = decode_response(data)
                except (socket.timeout, ValueError) as err:
                    last = err
                    continue
                if rid != request_id:
                    continue  # an answer to something else
                if status:
                    raise OSError(f"controller refused the write: {ERRORS.get(status, status)}")
                return
        raise OSError(f"no answer from the controller ({last.__class__.__name__ if last else 'silent'})")
