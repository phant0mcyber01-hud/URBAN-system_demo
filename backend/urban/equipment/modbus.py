"""Relay boards on Modbus RTU: the plan number as a binary code on a few coils.

Most industrial relay boards (RS-485 or USB) speak Modbus RTU with function 0x0F, write
multiple coils. The frame is built by hand; the serial port comes from pyserial, which is
only imported when a board is opened, so tests and the platform need no serial port.
Not tried on a real board.
"""
WRITE_COILS = 0x0F


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(frame: bytes) -> bytes:
    return frame + crc16(frame).to_bytes(2, "little")


def write_coils(unit: int, start: int, states: list[bool]) -> bytes:
    """Request frame: set `states` on the coils from `start` on."""
    if not 1 <= unit <= 247 or not states or len(states) > 1968:
        raise ValueError("bad unit or coil count")
    n = len(states)
    data = bytearray((n + 7) // 8)
    for i, on in enumerate(states):
        if on:
            data[i // 8] |= 1 << (i % 8)
    return with_crc(bytes([unit, WRITE_COILS]) + start.to_bytes(2, "big") + n.to_bytes(2, "big")
                    + bytes([len(data)]) + bytes(data))


def check_response(response: bytes, unit: int, start: int, count: int) -> None:
    """The board echoes unit, function, start and count; an exception frame has the function | 0x80."""
    if len(response) < 5 or crc16(response[:-2]) != int.from_bytes(response[-2:], "little"):
        raise OSError("relay board: bad or missing answer")
    if response[1] == (WRITE_COILS | 0x80):
        raise OSError(f"relay board: exception {response[2]}")
    if response[:6] != bytes([unit, WRITE_COILS]) + start.to_bytes(2, "big") + count.to_bytes(2, "big"):
        raise OSError("relay board: answer does not match the request")


class ModbusRelayBoard:
    """`write(tls, states)` for RelayAdapter: one board per unit, so tls is ignored."""

    def __init__(self, port: str, unit: int = 1, baud: int = 9600, start: int = 0, timeout: float = 1.0, transport=None):
        """transport: an object with write(bytes) and read(n) -> bytes, instead of opening `port`."""
        self.port, self.unit, self.baud, self.start, self.timeout = port, unit, baud, start, timeout
        self.transport = transport

    def _open(self):
        if self.transport is None:
            import serial  # pyserial, optional dependency "edge"
            self.transport = serial.Serial(self.port, self.baud, timeout=self.timeout)
        return self.transport

    def write(self, tls: str, states: list[bool]) -> None:
        try:
            link = self._open()
            link.write(write_coils(self.unit, self.start, states))
            check_response(link.read(8), self.unit, self.start, len(states))
        except OSError:
            self.transport = None if self.port else self.transport  # reopen the port next time
            raise
        except Exception as err:  # pyserial raises its own errors; the adapter understands OSError
            self.transport = None if self.port else self.transport
            raise OSError(f"relay board: {err}") from err
