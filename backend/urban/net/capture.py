"""Reading a packet capture of the wire between a junction unit and its district, and saying whether anything but
WireGuard crossed it (docs/wireguard-design.md, "Как проверить в QEMU").

QEMU writes the capture itself: `-object filter-dump,id=f0,netdev=<the uplink>,file=wire.pcap` records every Ethernet
frame that enters or leaves that virtual network card, so nothing is needed on the host (a host-side tcpdump cannot see
slirp traffic). This module only reads the file (the classic pcap format), classifies each frame and checks:

- every IP packet between the two machines is UDP between the WireGuard ports and has the shape of a WireGuard message
  (type 1 handshake initiation 148 bytes, type 2 response 92, type 3 cookie 64, type 4 data: 32 bytes plus a multiple of
  16); ARP and IPv6 neighbour discovery are allowed because they carry no data of ours; anything else is a violation;
- none of the given byte patterns (a marker sent as "video", an RTSP verb, a TLS server name) appears in any frame.

The check is only worth anything with a control: a capture of the same transfer sent in the clear, in which the same
patterns MUST be found (Verdict.leaks) and the traffic MUST be reported as not WireGuard. The QEMU check does both.
"""
import struct
from dataclasses import dataclass, field
from typing import Iterable

PCAP_MAGICS = {b"\xd4\xc3\xb2\xa1": ("<", 1_000_000), b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
               b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000), b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000)}
LINKTYPE_ETHERNET = 1
ETH_ARP, ETH_IPV4, ETH_IPV6 = 0x0806, 0x0800, 0x86DD
UDP, TCP, ICMPV6 = 17, 6, 58
ND_TYPES = range(133, 138)   # router/neighbour solicitation and advertisement, redirect
HOP_BY_HOP = 0
MLD_TYPES = (130, 131, 132, 143)  # multicast listener query, report, done; MLDv2 report
WG_PORT = 51820
WG_MTU = 1420               # wireguard.MTU: a full packet is padded only up to it, so not always to a multiple of 16


class CaptureError(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    time: float
    data: bytes


@dataclass(frozen=True)
class Packet:
    """One frame, as far as it was understood."""
    kind: str                # arp | ipv4 | ipv6-nd | ipv6 | other
    proto: int | None = None
    src: str | None = None
    dst: str | None = None
    sport: int | None = None
    dport: int | None = None
    payload: bytes = b""     # the UDP or TCP payload
    fragment: bool = False


def read_pcap(data: bytes) -> list[Frame]:
    """The frames of a classic pcap file of Ethernet frames."""
    if len(data) < 24 or data[:4] not in PCAP_MAGICS:
        raise CaptureError("not a classic pcap file")
    order, per_second = PCAP_MAGICS[data[:4]]
    _, _, _, _, _, link = struct.unpack(order + "HHiIII", data[4:24])
    if link != LINKTYPE_ETHERNET:
        raise CaptureError(f"link type {link}: only Ethernet captures are read")
    frames, pos = [], 24
    while pos + 16 <= len(data):
        sec, frac, caplen, _ = struct.unpack(order + "IIII", data[pos:pos + 16])
        pos += 16
        if pos + caplen > len(data):
            break  # a capture cut short by the machine stopping: the whole frames before it count
        frames.append(Frame(sec + frac / per_second, data[pos:pos + caplen]))
        pos += caplen
    return frames


def _ip4(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def classify(frame: bytes) -> Packet:
    if len(frame) < 14:
        return Packet("other")
    ether = struct.unpack("!H", frame[12:14])[0]
    body = frame[14:]
    if ether == ETH_ARP:
        return Packet("arp")
    if ether == ETH_IPV6:
        if len(body) >= 41 and body[6] == ICMPV6 and len(body) > 40 and body[40] in ND_TYPES:
            return Packet("ipv6-nd")
        # multicast listener reports (MLD, ICMPv6 behind a hop-by-hop header): a NIC's own link control, like ND
        # (a firmware's network stack sends them at power-on, before any system runs)
        if len(body) >= 49 and body[6] == HOP_BY_HOP and body[40] == ICMPV6 and body[48] in MLD_TYPES:
            return Packet("ipv6-nd")
        return Packet("ipv6")
    if ether != ETH_IPV4 or len(body) < 20 or body[0] >> 4 != 4:
        return Packet("other")
    ihl = (body[0] & 15) * 4
    total = struct.unpack("!H", body[2:4])[0]
    flags_frag = struct.unpack("!H", body[6:8])[0]
    fragment = bool(flags_frag & 0x1FFF or flags_frag & 0x2000)
    proto, src, dst = body[9], _ip4(body[12:16]), _ip4(body[16:20])
    segment = body[ihl:total] if ihl <= total <= len(body) else body[ihl:]  # Ethernet padding is not part of the packet
    sport = dport = None
    payload = b""
    if not fragment and proto == UDP and len(segment) >= 8:
        sport, dport = struct.unpack("!HH", segment[:4])
        payload = segment[8:]
    elif not fragment and proto == TCP and len(segment) >= 20:
        sport, dport = struct.unpack("!HH", segment[:4])
        payload = segment[(segment[12] >> 4) * 4:]
    return Packet("ipv4", proto, src, dst, sport, dport, payload, fragment)


def wg_message_type(payload: bytes) -> int | None:
    """The WireGuard message type (1..4) if the UDP payload has the shape of one, else None. The shape: the type byte,
    three zero bytes, and a length fixed per type (data messages are 32 bytes plus a multiple of 16, or plus the tunnel's
    MTU exactly: WireGuard pads to 16 only up to the MTU, so a full packet of 1420 bytes stays 1420)."""
    if len(payload) < 32 or payload[1:4] != b"\x00\x00\x00":
        return None
    kind, size = payload[0], len(payload)
    if (kind, size) in ((1, 148), (2, 92), (3, 64)):
        return kind
    if kind == 4 and ((size - 32) % 16 == 0 or size - 32 == WG_MTU):
        return 4
    return None


@dataclass
class Verdict:
    frames: int = 0
    wg_frames: int = 0
    wg_bytes: int = 0
    by_type: dict = field(default_factory=dict)
    violations: list = field(default_factory=list)   # what crossed the wire that is not WireGuard
    leaks: dict = field(default_factory=dict)         # pattern -> number of frames it appears in

    @property
    def clean(self) -> bool:
        """Only WireGuard (and ARP / neighbour discovery) on the wire, and none of the patterns anywhere."""
        return not self.violations and not self.leaks

    def summary(self) -> str:
        types = ", ".join(f"type {t}: {n}" for t, n in sorted(self.by_type.items())) or "none"
        return (f"{self.frames} frames, {self.wg_frames} WireGuard ({self.wg_bytes} bytes; {types}), "
                f"{len(self.violations)} not WireGuard, patterns found in {sum(self.leaks.values())} frame(s)")


def audit(frames: Iterable[Frame], machines: Iterable[str], patterns: Iterable[bytes] = (), port: int = WG_PORT,
          max_violations: int = 20) -> Verdict:
    """Check a capture of the wire between `machines` (their uplink addresses). A frame between them that is not a
    WireGuard message on the WireGuard port is listed in violations (with the first 20 kept); a pattern found anywhere
    in any frame is counted in leaks. Frames to or from other addresses (a DHCP server, a time server) are violations too:
    the uplink of a unit in tunnel mode talks to the district and to no one else in the test."""
    machines = set(machines)
    patterns = [bytes(p) for p in patterns]
    v = Verdict()
    for n, frame in enumerate(frames):
        v.frames += 1
        for p in patterns:
            if p and p in frame.data:
                v.leaks[p] = v.leaks.get(p, 0) + 1
        pkt = classify(frame.data)
        if pkt.kind in ("arp", "ipv6-nd"):
            continue
        why = None
        if pkt.kind != "ipv4":
            why = f"a {pkt.kind} frame"
        elif pkt.fragment:
            why = "an IP fragment"
        elif pkt.src not in machines or pkt.dst not in machines:
            why = f"{pkt.src} -> {pkt.dst}, not between the two machines"
        elif pkt.proto != UDP:
            why = f"{pkt.src} -> {pkt.dst} protocol {pkt.proto}" + (f" port {pkt.dport}" if pkt.dport else "")
        elif port not in (pkt.sport, pkt.dport):
            why = f"UDP {pkt.src}:{pkt.sport} -> {pkt.dst}:{pkt.dport}, not the WireGuard port"
        elif (kind := wg_message_type(pkt.payload)) is None:
            why = f"UDP {pkt.src}:{pkt.sport} -> {pkt.dst}:{pkt.dport} that is not shaped like a WireGuard message"
        if why is not None:
            if len(v.violations) < max_violations:
                v.violations.append(f"frame {n}: {why}")
            continue
        v.wg_frames += 1
        v.wg_bytes += len(pkt.payload)
        v.by_type[kind] = v.by_type.get(kind, 0) + 1
    return v
