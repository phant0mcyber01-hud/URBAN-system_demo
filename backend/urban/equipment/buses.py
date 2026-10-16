"""Contact buses: what physically closes the controller's plan-selection inputs.

Every bus has write(states) - all outputs at once, from output 0 - and, when it can, read(count) for the
controller's feedback contacts. The plan logic is in contacts.py and knows nothing of these.

ModbusRtuBus   relay board on RS-485 or USB (Modbus RTU, coils + discrete inputs), the common industrial choice
ModbusTcpBus   relay module on the cabinet's Ethernet (Modbus TCP)
GpioBus        the unit's own GPIO lines (a relay HAT or optocouplers), through the Linux GPIO character device.
               The lines are held by this process only: if it dies, the kernel releases them and the relay
               board's pull resistors open every contact - the one bus that is fail-safe by itself.
"""
import os
import struct

from . import modbus
from .modbus_tcp import ModbusTcp


class ModbusRtuBus:
    def __init__(self, board: "modbus.ModbusRelayBoard", input_start: int = 0):
        self.board, self.input_start = board, input_start

    def write(self, states: list[bool]):
        self.board.write("", states)

    def read(self, count: int) -> list[bool]:
        try:
            link = self.board._open()
            link.write(modbus.read_inputs(self.board.unit, self.input_start, count))
            return modbus.parse_inputs(link.read(5 + (count + 7) // 8), self.board.unit, count)
        except OSError:
            raise
        except Exception as err:  # pyserial's own errors
            raise OSError(f"relay board: {err}") from err


class ModbusTcpBus:
    def __init__(self, client: ModbusTcp, coil_start: int = 0, input_start: int = 0):
        self.client, self.coil_start, self.input_start = client, coil_start, input_start

    def write(self, states: list[bool]):
        self.client.write_coils(self.coil_start, states)

    def read(self, count: int) -> list[bool]:
        return self.client.read_inputs(self.input_start, count)


# ---- Linux GPIO character device, uAPI v2 (linux/gpio.h)
FLAG_ACTIVE_LOW, FLAG_INPUT, FLAG_OUTPUT = 1 << 1, 1 << 2, 1 << 3
ATTR_OUTPUT_VALUES = 2
REQUEST = "<64I32sQI5I" + "IIQQ" * 10 + "II5Ii"   # struct gpio_v2_line_request, 592 bytes
VALUES = "<QQ"                                    # struct gpio_v2_line_values: bits, mask


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (0xB4 << 8) | nr


GET_LINE = _iowr(0x07, struct.calcsize(REQUEST))
GET_VALUES = _iowr(0x0E, struct.calcsize(VALUES))
SET_VALUES = _iowr(0x0F, struct.calcsize(VALUES))


def line_request(offsets: list[int], flags: int, consumer: str, initial: int | None = None) -> bytearray:
    if not 1 <= len(offsets) <= 64:
        raise ValueError("1..64 lines per request")
    attrs = [(0, 0, 0, 0)] * 10
    nattrs = 0
    if initial is not None:  # outputs start open, before the first write
        attrs[0] = (ATTR_OUTPUT_VALUES, 0, initial, (1 << len(offsets)) - 1)
        nattrs = 1
    return bytearray(struct.pack(REQUEST, *(list(offsets) + [0] * (64 - len(offsets))), consumer.encode()[:31],
                                 flags, nattrs, 0, 0, 0, 0, 0, *(v for a in attrs for v in a),
                                 len(offsets), 0, 0, 0, 0, 0, 0, -1))


class GpioBus:
    def __init__(self, chip: str, outputs: list[int], inputs: list[int] = (), active_low: bool = False,
                 consumer: str = "urban-edge", opener=os.open, ioctl=None):
        """outputs/inputs: line offsets on the chip; active_low: a relay board that switches on a low level."""
        if ioctl is None:
            import fcntl
            ioctl = fcntl.ioctl
        self.ioctl, self.outputs, self.inputs = ioctl, list(outputs), list(inputs)
        chip_fd = opener(chip, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        try:
            flags = FLAG_OUTPUT | (FLAG_ACTIVE_LOW if active_low else 0)
            self.out_fd = self._request(chip_fd, self.outputs, flags, initial=0)
            self.in_fd = self._request(chip_fd, self.inputs, FLAG_INPUT) if self.inputs else None
        finally:
            try:
                os.close(chip_fd)
            except OSError:
                pass

    def _request(self, chip_fd: int, offsets: list[int], flags: int, initial: int | None = None) -> int:
        req = line_request(offsets, flags, "urban-edge", initial)
        self.ioctl(chip_fd, GET_LINE, req, True)
        fd = struct.unpack_from("<i", req, len(req) - 4)[0]
        if fd < 0:
            raise OSError("the kernel gave no line handle")
        return fd

    def write(self, states: list[bool]):
        if len(states) > len(self.outputs):
            raise OSError(f"{len(states)} contacts asked, {len(self.outputs)} GPIO lines configured")
        bits = sum(1 << i for i, on in enumerate(states) if on)
        self.ioctl(self.out_fd, SET_VALUES, bytearray(struct.pack(VALUES, bits, (1 << len(self.outputs)) - 1)), True)

    def read(self, count: int) -> list[bool]:
        if self.in_fd is None or count > len(self.inputs):
            raise OSError("feedback input not configured on a GPIO line")
        buf = bytearray(struct.pack(VALUES, 0, (1 << len(self.inputs)) - 1))
        self.ioctl(self.in_fd, GET_VALUES, buf, True)
        bits = struct.unpack(VALUES, buf)[0]
        return [bool(bits >> i & 1) for i in range(count)]
