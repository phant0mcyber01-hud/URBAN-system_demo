"""Contact buses against fake devices: Modbus RTU board, Modbus TCP module, Linux GPIO character device."""
import socket
import struct
import threading

import pytest

from urban.equipment import buses, modbus
from urban.equipment.buses import GpioBus, ModbusRtuBus, ModbusTcpBus
from urban.equipment.contacts import ContactAdapter, ContactMap, Feedback
from urban.equipment.modbus_tcp import ModbusTcp


# ---- Modbus RTU: a board on a serial line
class SerialBoard:
    def __init__(self, inputs=(True,)):
        self.coils, self.inputs, self.pending = [], list(inputs), b""

    def write(self, frame):
        assert modbus.crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")
        unit, fn = frame[0], frame[1]
        if fn == modbus.WRITE_COILS:
            count = int.from_bytes(frame[4:6], "big")
            data = frame[7:7 + frame[6]]
            self.coils = [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]
            self.pending = modbus.with_crc(frame[:6])
        elif fn == modbus.READ_INPUTS:
            count = int.from_bytes(frame[4:6], "big")
            bits = sum(1 << i for i, on in enumerate(self.inputs[:count]) if on)
            n = (count + 7) // 8
            self.pending = modbus.with_crc(bytes([unit, fn, n]) + bits.to_bytes(n, "little"))

    def read(self, n):
        out, self.pending = self.pending[:n], self.pending[n:]
        return out


def test_modbus_rtu_bus_drives_coils_and_reads_feedback():
    board = SerialBoard(inputs=[True])
    bus = ModbusRtuBus(modbus.ModbusRelayBoard("", transport=board))
    a = ContactAdapter(bus, {"T": {0, 1, 2}}, ContactMap("binary", 2, enable=2), Feedback(0), settle=0, sleep=lambda s: None)
    a.select("T", 2)
    assert board.coils == [False, True, True]
    a.base("T")
    assert board.coils == [False, False, False]
    assert modbus.parse_inputs(modbus.with_crc(bytes([1, 2, 1, 0b101])), 1, 3) == [True, False, True]
    with pytest.raises(OSError, match="exception"):
        modbus.parse_inputs(modbus.with_crc(bytes([1, 0x82, 2])), 1, 3)


# ---- Modbus TCP: a module on the cabinet's Ethernet
class FakeModbusTcp:
    """A Modbus TCP device: coils, discrete inputs, holding registers, identification. Records what it was told."""

    def __init__(self, inputs=(), registers=None, identity=None):
        self.coils, self.inputs, self.registers = {}, list(inputs), dict(registers or {})
        self.identity, self.writes = identity, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        with conn:
            while True:
                head = conn.recv(7)
                if len(head) < 7:
                    return
                tid, proto, length, unit = struct.unpack(">HHHB", head)
                pdu = conn.recv(length - 1)
                reply = self._handle(pdu)
                conn.sendall(struct.pack(">HHHB", tid, 0, len(reply) + 1, unit) + reply)

    def _handle(self, pdu):
        fn = pdu[0]
        if fn == 0x0F:
            start, count, n = struct.unpack(">HHB", pdu[1:6])
            for i in range(count):
                self.coils[start + i] = bool(pdu[6 + i // 8] >> (i % 8) & 1)
            self.writes.append(("coils", start, [self.coils[start + i] for i in range(count)]))
            return pdu[:5]
        if fn == 0x02:
            start, count = struct.unpack(">HH", pdu[1:5])
            bits = sum(1 << i for i, on in enumerate(self.inputs[start:start + count]) if on)
            n = (count + 7) // 8
            return bytes([fn, n]) + bits.to_bytes(n, "little")
        if fn == 0x06:
            address, value = struct.unpack(">HH", pdu[1:5])
            self.registers[address] = value
            self.writes.append(("register", address, value))
            return pdu
        if fn == 0x03:
            address, count = struct.unpack(">HH", pdu[1:5])
            if any(address + i not in self.registers for i in range(count)):
                return bytes([fn | 0x80, 2])
            return bytes([fn, 2 * count]) + b"".join(struct.pack(">H", self.registers[address + i]) for i in range(count))
        if fn == 0x2B and self.identity:
            objs = b"".join(bytes([i, len(v)]) + v.encode() for i, v in enumerate(self.identity))
            return bytes([0x2B, 0x0E, 0x01, 0x01, 0x00, 0x00, len(self.identity)]) + objs
        return bytes([fn | 0x80, 1])

    def close(self):
        self.sock.close()


def test_modbus_tcp_bus_drives_coils_and_reads_feedback():
    dev = FakeModbusTcp(inputs=[False, True])
    try:
        bus = ModbusTcpBus(ModbusTcp("127.0.0.1", dev.port, timeout=2), coil_start=16)
        a = ContactAdapter(bus, {"T": {0, 1, 2, 3}}, ContactMap("onehot", 3), Feedback(1), settle=0, sleep=lambda s: None)
        a.select("T", 3)
        assert dev.writes[-1] == ("coils", 16, [False, False, True])
        a.base("T")
        assert dev.writes[-1] == ("coils", 16, [False, False, False])
    finally:
        dev.close()


def test_modbus_tcp_reports_exceptions_and_a_dead_device():
    dev = FakeModbusTcp()
    client = ModbusTcp("127.0.0.1", dev.port, timeout=1)
    with pytest.raises(OSError, match="illegal data address"):
        client.read_holding(100)
    dev.close()
    client.close()
    with pytest.raises(OSError, match="modbus tcp"):
        ModbusTcp("127.0.0.1", dev.port, timeout=0.5).write_coils(0, [True])


# ---- GPIO character device
class FakeGpio:
    """Stands for the kernel: hands out line fds and keeps the values the unit set."""

    def __init__(self, input_bits=0):
        self.requests, self.values, self.input_bits, self.next_fd = [], {}, input_bits, 100

    def ioctl(self, fd, request, buf, mutate):
        if request == buses.GET_LINE:
            fields = struct.unpack(buses.REQUEST, buf)
            offsets, flags, nlines = fields[:64], fields[65], fields[-8]
            self.requests.append({"offsets": list(offsets[:nlines]), "flags": flags})
            self.next_fd += 1
            struct.pack_into("<i", buf, len(buf) - 4, self.next_fd)
            self.values[self.next_fd] = 0
        elif request == buses.SET_VALUES:
            bits, mask = struct.unpack(buses.VALUES, buf)
            self.values[fd] = bits & mask
        elif request == buses.GET_VALUES:
            struct.pack_into(buses.VALUES, buf, 0, self.input_bits, 0)
        return 0


def test_gpio_bus_requests_lines_and_sets_values(monkeypatch):
    kernel = FakeGpio(input_bits=0b1)
    monkeypatch.setattr(buses.os, "close", lambda fd: None)
    bus = GpioBus("/dev/gpiochip0", [17, 27, 22, 23], inputs=[5], active_low=True,
                  opener=lambda path, flags: 3, ioctl=kernel.ioctl)
    out, inp = kernel.requests
    assert out["offsets"] == [17, 27, 22, 23] and out["flags"] == buses.FLAG_OUTPUT | buses.FLAG_ACTIVE_LOW
    assert inp["offsets"] == [5] and inp["flags"] == buses.FLAG_INPUT
    a = ContactAdapter(bus, {"T": {0, 1, 2, 5}}, ContactMap("binary", 3, enable=3), Feedback(0), settle=0,
                       sleep=lambda s: None)
    a.select("T", 5)
    assert kernel.values[bus.out_fd] == 0b1101  # code 101 and the enable line
    a.base("T")
    assert kernel.values[bus.out_fd] == 0
    with pytest.raises(OSError, match="GPIO lines configured"):
        bus.write([True] * 5)
    assert struct.calcsize(buses.REQUEST) == 592 and buses.GET_LINE == 0xC250B407  # linux/gpio.h
