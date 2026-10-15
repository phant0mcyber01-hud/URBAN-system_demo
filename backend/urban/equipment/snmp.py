"""The smallest SNMP that NTCIP needs: one SetRequest of one INTEGER, over UDP.

SnmpClient speaks SNMPv1/v2c: a community string in clear text, acceptable only on an isolated
point-to-point link between the URBAN Edge unit and the controller. SnmpV3Client speaks SNMPv3
with the User-based Security Model in authPriv: every message authenticated (HMAC-SHA-96, RFC 3414,
or HMAC-SHA-256-192, RFC 7860) and encrypted (AES-128-CFB, RFC 3826), with engine discovery and
time synchronisation; use it whenever the controller supports it. Encoding is BER by hand so the
unit needs no SNMP library. Checked against the RFC 3414 key vectors and against an independent
agent (pysnmp) in the tests; not tried on a real controller.
"""
import hashlib
import hmac
import secrets
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:
    from cryptography.hazmat.decrepit.ciphers.modes import CFB  # cryptography >= 47
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers.modes import CFB

INTEGER, OCTET_STRING, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
SET_REQUEST, GET_RESPONSE = 0xA3, 0xA2
ERRORS = {1: "tooBig", 2: "noSuchName", 3: "badValue", 4: "readOnly", 5: "genErr", 6: "noAccess",
          7: "wrongType", 8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue", 11: "noCreation",
          12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed", 15: "undoFailed",
          16: "authorizationError", 17: "notWritable", 18: "inconsistentName"}


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


def encode_get(community: str, oid: str, request_id: int, version: int = 1) -> bytes:
    """A GetRequest for one object."""
    binding = _tlv(SEQUENCE, _oid(oid) + _tlv(NULL, b""))
    pdu = _tlv(0xA0, _int(request_id) + _int(0) + _int(0) + _tlv(SEQUENCE, binding))
    return _tlv(SEQUENCE, _int(version) + _tlv(OCTET_STRING, community.encode()) + pdu)


NUMBERS = {INTEGER, 0x41, 0x42, 0x43}  # INTEGER, Counter32, Gauge32, TimeTicks


def _number(tag: int, body: bytes) -> int:
    """The value of a varbind as an integer; ValueError for anything else (noSuchObject, a string...)."""
    if tag not in NUMBERS:
        raise ValueError(f"not a number (tag 0x{tag:02x})")
    return int.from_bytes(body, "big", signed=tag == INTEGER)


# ---- decoding, only as far as the response status
def _read(data: bytes, at: int) -> tuple[int, bytes, int]:
    """(tag, body, index after) of the TLV at `at`; ValueError if the data ends before the TLV does."""
    if at + 2 > len(data):
        raise ValueError("truncated")
    tag, n, at = data[at], data[at + 1], at + 2
    if n & 0x80:
        size = n & 0x7F
        if size == 0 or at + size > len(data):
            raise ValueError("truncated")
        n = int.from_bytes(data[at:at + size], "big")
        at += size
    if at + n > len(data):
        raise ValueError("truncated")
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


def decode_value(data: bytes) -> int:
    """The first varbind value of a response, as an integer."""
    tag, msg, _ = _read(data, 0)
    _, _, at = _read(msg, 0)
    _, _, at = _read(msg, at)
    _, pdu, _ = _read(msg, at)
    _, _, at = _read(pdu, 0)
    _, _, at = _read(pdu, at)
    _, _, at = _read(pdu, at)
    _, binds, _ = _read(pdu, at)
    _, bind, _ = _read(binds, 0)
    _, _, at = _read(bind, 0)
    tag, body, _ = _read(bind, at)
    return _number(tag, body)


class SnmpClient:
    """Sets or reads one integer object at a time and waits for the controller's answer."""

    def __init__(self, host: str, port: int = 161, community: str = "private", version: int = 1,
                 timeout: float = 2.0, retries: int = 2):
        self.address, self.community, self.version = (host, port), community, version
        self.timeout, self.retries = timeout, retries

    def set(self, oid: str, value: int) -> None:
        """Raises OSError when the controller does not answer or refuses the write."""
        request_id = secrets.randbelow(0x7FFFFFFF)
        self._request(encode_set(self.community, oid, value, request_id, self.version), request_id, "write")

    def get(self, oid: str) -> int:
        """Read one numeric object. Raises OSError when there is no usable answer."""
        request_id = secrets.randbelow(0x7FFFFFFF)
        data = self._request(encode_get(self.community, oid, request_id, self.version), request_id, "read")
        try:
            return decode_value(data)
        except (ValueError, IndexError) as err:
            raise OSError(f"controller answered {oid} with {err}") from err

    def _request(self, packet: bytes, request_id: int, what: str) -> bytes:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            last = None
            for _ in range(1 + self.retries):
                sock.sendto(packet, self.address)
                try:
                    data, _ = sock.recvfrom(1500)
                    rid, status = decode_response(data)
                except (socket.timeout, ValueError, IndexError) as err:
                    last = err
                    continue
                if rid != request_id:
                    continue  # an answer to something else
                if status:
                    raise OSError(f"controller refused the {what}: {ERRORS.get(status, status)}")
                return data
        raise OSError(f"no answer from the controller ({last.__class__.__name__ if last else 'silent'})")


# ---------------------------------------------------------------- SNMPv3, USM authPriv
GET_REQUEST, REPORT = 0xA0, 0xA8
USM = 3
AUTH = {"sha": (hashlib.sha1, 12), "sha256": (hashlib.sha256, 24)}  # hash, length of the MAC on the wire
TIME_WINDOW, UNKNOWN_ENGINE = "1.3.6.1.6.3.15.1.1.2.0", "1.3.6.1.6.3.15.1.1.4.0"
REPORTS = {  # usmStats counters an agent returns in a Report PDU
    "1.3.6.1.6.3.15.1.1.1.0": "unsupported security level",
    TIME_WINDOW: "not in time window",
    "1.3.6.1.6.3.15.1.1.3.0": "unknown user name",
    UNKNOWN_ENGINE: "unknown engine id",
    "1.3.6.1.6.3.15.1.1.5.0": "wrong digest: the authentication password does not match",
    "1.3.6.1.6.3.15.1.1.6.0": "decryption error: the privacy password does not match",
}


def password_to_key(password: str, hash_fn) -> bytes:
    """RFC 3414 A.2: hash of the password repeated to one megabyte."""
    data = password.encode()
    if not data:
        raise ValueError("empty password")
    stream = data * (1048576 // len(data) + 1)
    return hash_fn(stream[:1048576]).digest()


def localize(key: bytes, engine_id: bytes, hash_fn) -> bytes:
    """RFC 3414 A.2: the key bound to one engine, so a key learned from one controller opens no other."""
    return hash_fn(key + engine_id + key).digest()


def _octets(b: bytes) -> bytes:
    return _tlv(OCTET_STRING, b)


def _seq(*parts: bytes) -> bytes:
    return _tlv(SEQUENCE, b"".join(parts))


def _items(body: bytes) -> list[tuple[int, bytes]]:
    out, at = [], 0
    while at < len(body):
        tag, value, at = _read(body, at)
        out.append((tag, value))
    return out


def _decode_oid(b: bytes) -> str:
    if not b:
        raise ValueError("empty oid")
    parts, n = [b[0] // 40, b[0] % 40], 0
    for byte in b[1:]:
        n = (n << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(n)
            n = 0
    return ".".join(map(str, parts))


def _pdu(tag: int, request_id: int, oid: str | None, value: int | None) -> bytes:
    binding = _seq(_oid(oid) + (_int(value) if value is not None else _tlv(NULL, b""))) if oid else b""
    return _tlv(tag, _int(request_id) + _int(0) + _int(0) + _seq(binding))


def _message(msg_id: int, flags: int, usm: bytes, data: bytes) -> bytes:
    header = _seq(_int(msg_id), _int(65507), _octets(bytes([flags])), _int(USM))
    return _seq(_int(3), header, _octets(usm), data)


def _usm(engine: bytes, boots: int, etime: int, user: bytes, auth: bytes, priv: bytes) -> bytes:
    return _seq(_octets(engine), _int(boots), _int(etime), _octets(user), _octets(auth), _octets(priv))


def _parse_message(data: bytes) -> dict:
    tag, body, _ = _read(data, 0)
    if tag != SEQUENCE:
        raise ValueError("not an SNMP message")
    items = _items(body)
    if len(items) != 4 or items[0] != (INTEGER, b"\x03"):
        raise ValueError("not an SNMPv3 message")
    header = _items(items[1][1])
    usm = _items(_read(items[2][1], 0)[1])
    if len(header) != 4 or len(usm) != 6:
        raise ValueError("malformed SNMPv3 header")
    return {"msg_id": int.from_bytes(header[0][1], "big"), "flags": header[2][1][0] if header[2][1] else 0,
            "engine": usm[0][1], "boots": int.from_bytes(usm[1][1], "big"), "time": int.from_bytes(usm[2][1], "big"),
            "user": usm[3][1], "auth": usm[4][1], "priv": usm[5][1], "data": items[3]}


def _parse_scoped(data: bytes) -> tuple[int, int, int, list[str], list[tuple[int, bytes]]]:
    """ScopedPDU -> (pdu tag, request id, error status, oids in the varbinds, their values as (tag, bytes))."""
    tag, body, _ = _read(data, 0)
    if tag != SEQUENCE:
        raise ValueError("not a scoped PDU")
    items = _items(body)
    pdu_tag, pdu = items[2]
    fields = _items(pdu)
    binds = [_items(vb) for _, vb in _items(fields[3][1])]
    return (pdu_tag, int.from_bytes(fields[0][1], "big", signed=True),
            int.from_bytes(fields[1][1], "big", signed=True),
            [_decode_oid(b[0][1]) for b in binds], [b[1] for b in binds if len(b) > 1])


class SnmpV3Client:
    """SNMPv3 authPriv: one authenticated, encrypted SetRequest at a time, as NtcipAdapter needs."""

    def __init__(self, host: str, port: int = 161, user: str = "", auth_password: str = "", priv_password: str = "",
                 auth: str = "sha", timeout: float = 2.0, retries: int = 2):
        if auth not in AUTH:
            raise ValueError(f"auth must be one of {sorted(AUTH)}")
        if not user or len(auth_password) < 8 or len(priv_password) < 8:
            raise ValueError("SNMPv3 needs a user and passwords of at least 8 characters (RFC 3414)")
        self.address, self.user = (host, port), user.encode()
        self.hash, self.mac_len = AUTH[auth]
        self._auth_ku = password_to_key(auth_password, self.hash)
        self._priv_ku = password_to_key(priv_password, self.hash)
        self.timeout, self.retries = timeout, retries
        self.engine: bytes | None = None
        self.boots = self.etime = 0
        self._synced = 0.0  # monotonic time when boots and time were learned
        self._salt = secrets.randbits(64)

    # ---- engine discovery and time (RFC 3414 section 4)
    def _exchange(self, sock, packet: bytes, msg_id: int) -> tuple[dict, bytes]:
        last = None
        for _ in range(1 + self.retries):
            sock.sendto(packet, self.address)
            try:
                while True:
                    data, _ = sock.recvfrom(65535)
                    msg = _parse_message(data)
                    if msg["msg_id"] == msg_id:
                        return msg, data
            except (socket.timeout, ValueError, IndexError) as err:
                last = err
        raise OSError(f"no answer from the controller ({last.__class__.__name__ if last else 'silent'})")

    def _learn(self, msg: dict):
        if msg["engine"] != self.engine:
            self.engine = msg["engine"]
            self._auth_key = localize(self._auth_ku, self.engine, self.hash)
            self._priv_key = localize(self._priv_ku, self.engine, self.hash)[:16]
        self.boots, self.etime, self._synced = msg["boots"], msg["time"], time.monotonic()

    def discover(self, sock):
        msg_id = secrets.randbelow(0x7FFFFFFF)
        scoped = _seq(_octets(b""), _octets(b""), _pdu(GET_REQUEST, secrets.randbelow(0x7FFFFFFF), None, None))
        msg, _ = self._exchange(sock, _message(msg_id, 0x04, _usm(b"", 0, 0, b"", b"", b""), scoped), msg_id)
        if not msg["engine"]:
            raise OSError("the controller did not reveal its engine id")
        self._learn(msg)

    # ---- authPriv messages
    def _set_packet(self, oid: str, value: int | None, msg_id: int, request_id: int, tag: int = SET_REQUEST) -> bytes:
        boots, etime = self.boots, self.etime + int(time.monotonic() - self._synced)
        self._salt = (self._salt + 1) % (1 << 64)  # a new IV for every message
        salt = self._salt.to_bytes(8, "big")
        scoped = _seq(_octets(self.engine), _octets(b""), _pdu(tag, request_id, oid, value))
        enc = Cipher(algorithms.AES(self._priv_key), CFB(struct.pack(">II", boots, etime) + salt)).encryptor()
        data = _octets(enc.update(scoped) + enc.finalize())
        blank = _octets(b"\x00" * self.mac_len)
        usm = _usm(self.engine, boots, etime, self.user, b"\x00" * self.mac_len, salt)
        whole = _message(msg_id, 0x07, usm, data)
        # the MAC goes where the zeros are; they sit inside the security parameters, ahead of the encrypted data
        at = whole.index(_octets(usm)) + whole[whole.index(_octets(usm)):].index(blank) + 2
        mac = hmac.new(self._auth_key, whole, self.hash).digest()[:self.mac_len]
        return whole[:at] + mac + whole[at + self.mac_len:]

    def _open(self, msg: dict, raw: bytes) -> tuple:
        """Check and decrypt an answer. Unauthenticated answers are accepted only as Reports (RFC 3414 3.2)."""
        if msg["flags"] & 0x01:
            if len(msg["auth"]) != self.mac_len or msg["engine"] != self.engine:
                raise ValueError("bad authentication parameters")
            field = _octets(msg["auth"])
            at = raw.index(field) + 2
            blank = raw[:at] + b"\x00" * self.mac_len + raw[at + self.mac_len:]
            if not hmac.compare_digest(hmac.new(self._auth_key, blank, self.hash).digest()[:self.mac_len], msg["auth"]):
                raise ValueError("the answer failed authentication")
        tag, body = msg["data"]
        if msg["flags"] & 0x02:
            if tag != OCTET_STRING or len(msg["priv"]) != 8:
                raise ValueError("bad privacy parameters")
            iv = struct.pack(">II", msg["boots"], msg["time"]) + msg["priv"]
            dec = Cipher(algorithms.AES(self._priv_key), CFB(iv)).decryptor()
            scoped = dec.update(body) + dec.finalize()
        else:
            scoped = _tlv(tag, body)
        result = _parse_scoped(scoped)
        if not msg["flags"] & 0x01 and result[0] != REPORT:
            raise ValueError("unauthenticated answer")
        return result

    def set(self, oid: str, value: int) -> None:
        """Raises OSError when the controller does not answer, does not authenticate, or refuses the write."""
        self._request(SET_REQUEST, oid, value)

    def get(self, oid: str) -> int:
        """Read one numeric object over authPriv. Raises OSError when there is no usable answer."""
        values = self._request(GET_REQUEST, oid, None)
        try:
            return _number(*values[0])
        except (ValueError, IndexError) as err:
            raise OSError(f"controller answered {oid} with {err}") from err

    def _request(self, pdu_tag: int, oid: str, value: int | None) -> list:
        what = "write" if pdu_tag == SET_REQUEST else "read"
        family = socket.AF_INET6 if ":" in self.address[0] else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            if self.engine is None:
                self.discover(sock)
            for attempt in range(2):  # once more after resynchronising time or engine
                msg_id, request_id = secrets.randbelow(0x7FFFFFFF), secrets.randbelow(0x7FFFFFFF)
                msg, raw = self._exchange(sock, self._set_packet(oid, value, msg_id, request_id, pdu_tag), msg_id)
                try:
                    tag, rid, status, oids, values = self._open(msg, raw)
                except (ValueError, IndexError) as err:
                    raise OSError(f"snmpv3: {err}") from err
                if tag == REPORT:
                    reason = oids[0] if oids else ""
                    if attempt == 0 and reason in (TIME_WINDOW, UNKNOWN_ENGINE) and msg["engine"]:
                        self._learn(msg)  # the agent told us its boots and time, or a new engine id
                        continue
                    raise OSError(f"controller refused the request: {REPORTS.get(reason, 'report ' + reason)}")
                if rid != request_id:
                    raise OSError("snmpv3: answer to another request")
                if status:
                    raise OSError(f"controller refused the {what}: {ERRORS.get(status, status)}")
                return values
