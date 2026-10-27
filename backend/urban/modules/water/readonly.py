"""ReadOnlyModbus: a Modbus client that can only read. The pump station's whole link to its automation.

It builds requests for functions 01 (coils), 02 (discrete inputs), 03 (holding registers) and 04 (input registers)
and nothing else: build_pdu() raises WriteRefused for any other function before a byte exists, and both transports
check the function code of what they are handed again before they send it. There is no way to pass a raw request
through. The relay board's Modbus clients (urban/equipment/modbus_tcp.py, modbus.py) can write, so this module does
not import them (tests/test_water_device.py checks it): the framing (MBAP for TCP, address + CRC-16 for RTU) is
built here by hand, as there.

    client = ReadOnlyModbus(TcpTransport("10.0.0.5", 502, unit=1))   # or RtuTransport("/dev/ttyUSB0", 1, 9600)
    client.read(3, 10, 2) -> [0x0012, 0x5A40]    # function, address, count -> registers (or bits for 01/02)

Errors on the wire or from the device are ModbusError (an OSError): the station reports them as its source_error.
"""
from __future__ import annotations

import socket
import struct

READ_COILS, READ_DISCRETE, READ_HOLDING, READ_INPUT = 0x01, 0x02, 0x03, 0x04
READ_FUNCTIONS = frozenset({READ_COILS, READ_DISCRETE, READ_HOLDING, READ_INPUT})
MAX_BITS, MAX_REGISTERS = 2000, 125  # the protocol's limits for one read
EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "device failure",
              5: "acknowledge", 6: "device busy", 10: "gateway path unavailable", 11: "gateway target failed to respond"}


class WriteRefused(ValueError):
    """A request for a function that is not a read: never built, never sent."""


class ModbusError(OSError):
    """The automation did not answer, answered wrongly, or answered with a Modbus exception."""


def _readonly(function) -> int:
    if isinstance(function, bool) or not isinstance(function, int) or function not in READ_FUNCTIONS:
        raise WriteRefused(f"function {function!r} refused: this station only reads (functions 01-04)")
    return function


def build_pdu(function: int, address: int, count: int) -> bytes:
    """The request PDU of one read. The function is checked first, before anything else is looked at."""
    _readonly(function)
    limit = MAX_BITS if function in (READ_COILS, READ_DISCRETE) else MAX_REGISTERS
    for name, v in (("address", address), ("count", count)):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"{name} must be a whole number")
    if not 1 <= count <= limit or not 0 <= address or address + count > 0x10000:
        raise ValueError(f"read of {count} from {address}: count 1..{limit}, within addresses 0..65535")
    return struct.pack(">BHH", function, address, count)


def parse_pdu(request: bytes, body: bytes) -> list:
    """The answer PDU to a read request -> its bits (functions 01, 02) or registers (03, 04)."""
    function, _address, count = struct.unpack(">BHH", request)
    if not body:
        raise ModbusError("empty answer")
    if body[0] == function | 0x80:
        code = body[1] if len(body) > 1 else 0
        raise ModbusError(f"modbus exception: {EXCEPTIONS.get(code, code)}")
    if body[0] != function or len(body) < 2:
        raise ModbusError("answer is for another function")
    data = body[2:2 + body[1]]
    if function in (READ_COILS, READ_DISCRETE):
        if body[1] != (count + 7) // 8 or len(data) != body[1]:
            raise ModbusError("short answer")
        return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]
    if body[1] != 2 * count or len(data) != body[1]:
        raise ModbusError("short answer")
    return list(struct.unpack(f">{count}H", data))


class ReadOnlyModbus:
    """Reads only. `transport` has exchange(pdu) -> answer pdu and close() (TcpTransport, RtuTransport)."""

    def __init__(self, transport):
        self.transport = transport

    def read(self, function: int, address: int, count: int) -> list:
        pdu = build_pdu(function, address, count)
        return parse_pdu(pdu, self.transport.exchange(pdu))

    def read_coils(self, address: int, count: int = 1) -> list[bool]:
        return self.read(READ_COILS, address, count)

    def read_discrete(self, address: int, count: int = 1) -> list[bool]:
        return self.read(READ_DISCRETE, address, count)

    def read_holding(self, address: int, count: int = 1) -> list[int]:
        return self.read(READ_HOLDING, address, count)

    def read_input(self, address: int, count: int = 1) -> list[int]:
        return self.read(READ_INPUT, address, count)

    def close(self):
        self.transport.close()


# ------------------------------------------------------------------ Modbus TCP: MBAP header + PDU

class TcpTransport:
    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 2.0,
                 connect=socket.create_connection):
        if isinstance(unit, bool) or not isinstance(unit, int) or not 0 <= unit <= 255:
            raise ValueError("--slave (Modbus unit id) is 0..255")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("--port is 1..65535")
        self.address, self.unit, self.timeout, self.connect = (host, port), unit, timeout, connect
        self.sock, self.tid = None, 0

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise OSError("connection closed by the device")
            out += chunk
        return out

    def exchange(self, pdu: bytes) -> bytes:
        """One read request -> its answer. One reconnect on a broken connection; ModbusError otherwise."""
        _readonly(pdu[0] if pdu else None)  # the second check: nothing but a read leaves this machine
        for attempt in (0, 1):
            try:
                if self.sock is None:
                    self.sock = self.connect(self.address, timeout=self.timeout)
                    self.sock.settimeout(self.timeout)
                self.tid = (self.tid + 1) & 0xFFFF
                self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu)
                tid, proto, length, _unit = struct.unpack(">HHHB", self._recv(7))
                if not 2 <= length <= 254:
                    raise OSError("bad answer length")
                body = self._recv(length - 1)
                if tid != self.tid or proto != 0:
                    raise OSError("answer does not match the request")
                return body
            except (OSError, struct.error) as err:
                self.close()
                if attempt:
                    raise ModbusError(f"modbus tcp {self.address[0]}:{self.address[1]}: "
                                      f"{str(err) or err.__class__.__name__}") from err
        raise ModbusError("unreachable")  # pragma: no cover - the loop returns or raises


# ------------------------------------------------------------------ Modbus RTU: unit + PDU + CRC-16, on a serial line

def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(frame: bytes) -> bytes:
    return frame + crc16(frame).to_bytes(2, "little")


class RtuTransport:
    """RS-485 / USB serial. pyserial is imported only when the port is opened; `serial` may be any object with
    write(bytes) and read(n) -> bytes instead (tests)."""

    def __init__(self, port: str | None, unit: int = 1, baud: int = 9600, timeout: float = 1.0, serial=None):
        if isinstance(unit, bool) or not isinstance(unit, int) or not 1 <= unit <= 247:
            raise ValueError("--slave (Modbus unit id) on a serial line is 1..247")
        self.port, self.unit, self.baud, self.timeout, self.link = port, unit, baud, timeout, serial

    def _open(self):
        if self.link is None:
            import serial  # pyserial, optional dependency "edge"
            self.link = serial.Serial(self.port, self.baud, timeout=self.timeout)
        return self.link

    def close(self):
        if self.port and self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass
            self.link = None

    def _read(self, link, n: int) -> bytes:
        data = link.read(n)
        if len(data) != n:
            raise ModbusError("no answer from the automation (timeout)")
        return bytes(data)

    def exchange(self, pdu: bytes) -> bytes:
        _readonly(pdu[0] if pdu else None)  # the second check: nothing but a read goes onto the line
        try:
            link = self._open()
            link.write(with_crc(bytes([self.unit]) + pdu))
            head = self._read(link, 3)          # unit, function, byte count (or exception code)
            rest = self._read(link, (0 if head[1] & 0x80 else head[2]) + 2)
            frame = head + rest
            if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
                raise ModbusError("bad CRC in the answer")
            if frame[0] != self.unit:
                raise ModbusError("answer from another unit")
            return frame[1:-2]
        except ModbusError:
            self.close()
            raise
        except Exception as err:  # pyserial raises its own errors; the station understands OSError
            self.close()
            raise ModbusError(f"modbus rtu {self.port}: {err.__class__.__name__}") from err
