"""Modbus TCP: Ethernet relay modules, and controllers that expose plan selection in registers.

Plain Modbus Application Protocol over TCP (MBAP header + PDU), built by hand like the rest of the unit's
transports, so the unit needs no Modbus library. Covers what plan selection and identification need:
coils and discrete inputs (relay modules), single and multiple holding registers (register-mapped controllers)
and Read Device Identification (who made the device). Not tried on a real device.
"""
import socket
import struct

WRITE_COILS, READ_INPUTS, READ_HOLDING, WRITE_REGISTER, WRITE_REGISTERS, DEVICE_ID = 0x0F, 0x02, 0x03, 0x06, 0x10, 0x2B
EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "device failure",
              5: "acknowledge", 6: "device busy", 10: "gateway path unavailable", 11: "gateway target failed to respond"}


def _bits(states: list[bool]) -> bytes:
    data = bytearray((len(states) + 7) // 8)
    for i, on in enumerate(states):
        if on:
            data[i // 8] |= 1 << (i % 8)
    return bytes(data)


class ModbusTcp:
    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 2.0, connect=socket.create_connection):
        if not 0 <= unit <= 255:
            raise ValueError("unit id 0..255")
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

    def call(self, pdu: bytes) -> bytes:
        """One request PDU -> the response PDU. One reconnect on a broken connection; OSError otherwise."""
        for attempt in (0, 1):
            try:
                if self.sock is None:
                    self.sock = self.connect(self.address, timeout=self.timeout)
                    self.sock.settimeout(self.timeout)
                self.tid = (self.tid + 1) & 0xFFFF
                self.sock.sendall(struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu)
                tid, proto, length, _unit = struct.unpack(">HHHB", self._recv(7))
                body = self._recv(length - 1)
                if tid != self.tid or proto != 0:
                    raise OSError("answer does not match the request")
                break
            except (OSError, struct.error) as err:
                self.close()
                if attempt:
                    raise OSError(f"modbus tcp {self.address[0]}:{self.address[1]}: {err}") from err
        if body[0] == pdu[0] | 0x80:
            code = body[1] if len(body) > 1 else 0
            raise OSError(f"modbus exception: {EXCEPTIONS.get(code, code)}")
        if body[0] != pdu[0]:
            raise OSError("answer is for another function")
        return body

    # ---- coils and inputs
    def write_coils(self, start: int, states: list[bool]):
        data = _bits(states)
        self.call(struct.pack(">BHHB", WRITE_COILS, start, len(states), len(data)) + data)

    def read_inputs(self, start: int, count: int) -> list[bool]:
        body = self.call(struct.pack(">BHH", READ_INPUTS, start, count))
        data = body[2:2 + body[1]]
        if len(data) < (count + 7) // 8:
            raise OSError("short answer")
        return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]

    # ---- registers
    def write_register(self, address: int, value: int):
        if not 0 <= value <= 0xFFFF:
            raise ValueError("register values are 0..65535")
        self.call(struct.pack(">BHH", WRITE_REGISTER, address, value))

    def write_registers(self, address: int, values: list[int]):
        self.call(struct.pack(f">BHHB{len(values)}H", WRITE_REGISTERS, address, len(values), 2 * len(values), *values))

    def read_holding(self, address: int, count: int = 1) -> list[int]:
        body = self.call(struct.pack(">BHH", READ_HOLDING, address, count))
        if body[1] != 2 * count:
            raise OSError("short answer")
        return list(struct.unpack(f">{count}H", body[2:2 + 2 * count]))

    # ---- who is it
    def device_id(self) -> dict[str, str]:
        """Read Device Identification, basic objects: vendor, product code, revision."""
        body = self.call(bytes([DEVICE_ID, 0x0E, 0x01, 0x00]))
        names = {0: "vendor", 1: "product", 2: "revision"}
        count, at, out = body[6], 7, {}
        for _ in range(count):
            oid, n = body[at], body[at + 1]
            out[names.get(oid, f"object{oid}")] = body[at + 2:at + 2 + n].decode("latin-1").strip()
            at += 2 + n
        return out
