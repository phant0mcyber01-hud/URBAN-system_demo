"""The judge of the wire (urban/net/capture.py) judged itself: a synthetic capture of a WireGuard exchange passes, and the
same transfer in the clear is caught on every count (not WireGuard, the marker found), so the QEMU check cannot pass because
its judge looks at the wrong thing."""
import os
import struct

import pytest

from urban.net import capture as cap

UNIT, DISTRICT, OTHER = "192.168.77.2", "192.168.77.1", "192.168.77.99"
MARKER = b"URBAN-CAM-MARKER-7f3a9c0d11e2b4a5"


def ip(addr):
    return bytes(int(x) for x in addr.split("."))


def ether(payload, kind=0x0800):
    return b"\x52\x54\x00\x12\x34\x56" + b"\x52\x54\x00\x12\x34\x57" + struct.pack("!H", kind) + payload


def ipv4(src, dst, proto, segment, flags_frag=0x4000):
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(segment), 1, flags_frag, 64, proto, 0, ip(src), ip(dst))
    return ether(header + segment)


def udp(src, dst, sport, dport, payload):
    return ipv4(src, dst, 17, struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload)


def tcp(src, dst, sport, dport, payload):
    return ipv4(src, dst, 6, struct.pack("!HHIIBBHHH", sport, dport, 1, 1, 5 << 4, 0x18, 65535, 0, 0) + payload)


def wg(kind, size):
    return bytes([kind, 0, 0, 0]) + os.urandom(size - 4)


def pcap(frames, big=False, nano=False):
    order = ">" if big else "<"
    magic = {(False, False): b"\xd4\xc3\xb2\xa1", (True, False): b"\xa1\xb2\xc3\xd4",
             (False, True): b"\x4d\x3c\xb2\xa1", (True, True): b"\xa1\xb2\x3c\x4d"}[(big, nano)]
    out = magic + struct.pack(order + "HHiIII", 2, 4, 0, 0, 65536, 1)
    for i, f in enumerate(frames):
        out += struct.pack(order + "IIII", 100 + i, 5, len(f), len(f)) + f
    return out


def tunnel_exchange():
    return [ether(b"\x00" * 28, 0x0806),                                   # ARP
            udp(UNIT, DISTRICT, 40001, 51820, wg(1, 148)), udp(DISTRICT, UNIT, 51820, 40001, wg(2, 92)),
            udp(UNIT, DISTRICT, 40001, 51820, wg(4, 32)),                     # a keepalive
            *[udp(DISTRICT, UNIT, 51820, 40001, wg(4, 32 + 16 * k)) for k in (1, 40, 90)],
            udp(UNIT, DISTRICT, 40001, 51820, wg(3, 64))]


def test_a_wireguard_exchange_is_clean():
    v = cap.audit(cap.read_pcap(pcap(tunnel_exchange())), [UNIT, DISTRICT], [MARKER, b"RTSP/1.0", b"DESCRIBE"])
    assert v.clean and v.frames == 8 and v.wg_frames == 7
    assert v.by_type == {1: 1, 2: 1, 4: 4, 3: 1} and v.wg_bytes == 148 + 92 + 32 + 48 + 672 + 1472 + 64
    assert "7 WireGuard" in v.summary() and "0 not WireGuard" in v.summary()


def test_the_same_transfer_in_the_clear_is_caught_twice():
    """The control: what the unit would put on the wire without the tunnel is seen both ways, so a quiet result for the
    tunnel is a statement about the tunnel and not about a judge that sees nothing."""
    clear = [tcp(DISTRICT, UNIT, 41000, 554, b"DESCRIBE rtsp://192.168.77.2/stream1 RTSP/1.0\r\n\r\n"),
             tcp(UNIT, DISTRICT, 554, 41000, b"RTSP/1.0 200 OK\r\n\r\n" + MARKER * 3)]
    v = cap.audit(cap.read_pcap(pcap(clear)), [UNIT, DISTRICT], [MARKER, b"RTSP/1.0", b"DESCRIBE"])
    assert not v.clean and v.wg_frames == 0
    assert len(v.violations) == 2 and "protocol 6 port 554" in v.violations[0]
    assert v.leaks == {MARKER: 1, b"RTSP/1.0": 2, b"DESCRIBE": 1}
    # a leak alone (the marker riding in a UDP packet on the WireGuard port, shaped like nothing) is both
    odd = [udp(UNIT, DISTRICT, 40001, 51820, MARKER * 3)]
    v = cap.audit(cap.read_pcap(pcap(odd)), [UNIT, DISTRICT], [MARKER])
    assert not v.clean and "not shaped like a WireGuard message" in v.violations[0] and v.leaks == {MARKER: 1}


@pytest.mark.parametrize("frame,why", [
    (tcp(UNIT, DISTRICT, 40000, 8443, b"\x16\x03\x01" + b"x" * 40), "protocol 6 port 8443"),               # TLS to the relay, plain
    (udp(UNIT, DISTRICT, 40001, 5353, wg(4, 64)), "not the WireGuard port"),                                # right shape, wrong port
    (udp(UNIT, DISTRICT, 40001, 51820, wg(1, 147)), "not shaped like a WireGuard message"),                # wrong length
    (udp(UNIT, DISTRICT, 40001, 51820, wg(4, 40)), "not shaped like a WireGuard message"),                  # data not a multiple of 16
    (udp(UNIT, DISTRICT, 40001, 51820, wg(7, 64)), "not shaped like a WireGuard message"),                  # no such type
    (udp(UNIT, DISTRICT, 40001, 51820, b"\x04\x00\x00\x01" + os.urandom(60)), "not shaped like a WireGuard message"),  # reserved bytes
    (udp(UNIT, OTHER, 40001, 51820, wg(4, 64)), "not between the two machines"),                            # a third party
    (ipv4(UNIT, DISTRICT, 1, b"\x08\x00" + b"\x00" * 30), "protocol 1"),                                    # ping
    (ipv4(UNIT, DISTRICT, 17, b"\x00" * 40, flags_frag=0x2000), "fragment"),
    (ether(b"\x60" + b"\x00" * 60, 0x86DD), "ipv6"),                                                       # IPv6 that is not discovery
    (ether(b"\x00" * 30, 0x88CC), "other"),
])
def test_anything_but_wireguard_between_the_machines_is_a_violation(frame, why):
    v = cap.audit(cap.read_pcap(pcap([frame])), [UNIT, DISTRICT])
    assert not v.clean and why in v.violations[0] and v.wg_frames == 0


def test_a_full_mtu_data_message_is_wireguard():
    # WireGuard pads to 16 only up to the MTU: a full 1420-byte packet travels as a 1452-byte message (seen in QEMU)
    assert cap.wg_message_type(wg(4, 32 + 1420)) == 4
    frames = [udp(UNIT, DISTRICT, 43887, 51820, wg(4, 32 + 1420)) for _ in range(3)]
    assert cap.audit(cap.read_pcap(pcap(frames)), [UNIT, DISTRICT]).clean
    # one byte off either way is not a WireGuard shape
    assert cap.wg_message_type(wg(4, 32 + 1419)) is None and cap.wg_message_type(wg(4, 32 + 1421)) is None


def test_a_multicast_listener_report_is_link_control_but_still_searched():
    # MLDv2 report as a firmware sends it at power-on (seen in QEMU): IPv6, hop-by-hop header, ICMPv6 type 143
    def mld(extra=b""):
        hop = bytes([58, 0, 5, 2, 0, 0, 1, 0])  # next ICMPv6, router alert
        icmp = bytes([143, 0, 0, 0]) + b"\x00\x00\x00\x01" + b"\x00" * 20 + extra
        ip6 = b"\x60\x00\x00\x00" + (len(hop) + len(icmp)).to_bytes(2, "big") + bytes([0, 1]) + b"\x00" * 32
        return ether(ip6 + hop + icmp, 0x86DD)
    assert cap.audit(cap.read_pcap(pcap([mld()])), [UNIT, DISTRICT]).clean
    # a pattern inside one is still a leak
    v = cap.audit(cap.read_pcap(pcap([mld(b"URBAN-MARK-x")])), [UNIT, DISTRICT], [b"URBAN-MARK-x"])
    assert v.leaks and not v.clean
    # another IPv6 packet behind a hop-by-hop header is not
    other = ether(b"\x60\x00\x00\x00\x00\x10\x00\x01" + b"\x00" * 32 + bytes([17, 0, 5, 2, 0, 0, 1, 0]) + b"\x00" * 8, 0x86DD)
    assert not cap.audit(cap.read_pcap(pcap([other])), [UNIT, DISTRICT]).clean


def test_neighbour_discovery_and_arp_are_not_data():
    nd = ether(b"\x60\x00\x00\x00\x00\x20\x3a\xff" + b"\x00" * 32 + bytes([135]) + b"\x00" * 31, 0x86DD)
    assert cap.audit(cap.read_pcap(pcap([nd, ether(b"\x00" * 28, 0x0806)])), [UNIT, DISTRICT]).clean


def test_a_marker_inside_a_wireguard_looking_frame_is_found():
    frames = tunnel_exchange()
    leaky = udp(DISTRICT, UNIT, 51820, 40001, wg(4, 64)[:20] + MARKER + wg(4, 64)[20:][:12])  # the marker inside
    v = cap.audit(cap.read_pcap(pcap([*frames, leaky])), [UNIT, DISTRICT], [MARKER])
    assert v.leaks == {MARKER: 1}


def test_ethernet_padding_does_not_count_as_payload():
    short = udp(UNIT, DISTRICT, 40001, 51820, wg(4, 32)) + b"\x00" * 10   # a runt padded by the card
    v = cap.audit(cap.read_pcap(pcap([short])), [UNIT, DISTRICT])
    assert v.clean and v.wg_bytes == 32


@pytest.mark.parametrize("big,nano", [(False, False), (True, False), (False, True), (True, True)])
def test_every_flavour_of_pcap_is_read(big, nano):
    frames = tunnel_exchange()
    got = cap.read_pcap(pcap(frames, big, nano))
    assert [f.data for f in got] == frames and got[0].time == pytest.approx(100 + (5e-9 if nano else 5e-6))


def test_a_capture_cut_short_keeps_its_whole_frames_and_junk_is_refused():
    data = pcap(tunnel_exchange())
    assert len(cap.read_pcap(data[:-10])) == 7
    assert cap.read_pcap(data[:24]) == []
    for bad in (b"", b"GIF89a" + b"\x00" * 40, data[:20]):
        with pytest.raises(cap.CaptureError):
            cap.read_pcap(bad)
    wifi = bytearray(data)
    wifi[20:24] = struct.pack("<I", 105)
    with pytest.raises(cap.CaptureError, match="link type 105"):
        cap.read_pcap(bytes(wifi))


def test_only_the_first_violations_are_kept():
    frames = [tcp(UNIT, DISTRICT, 40000 + i, 8443, b"x" * 10) for i in range(50)]
    v = cap.audit(cap.read_pcap(pcap(frames)), [UNIT, DISTRICT], max_violations=5)
    assert len(v.violations) == 5 and v.frames == 50 and "5 not WireGuard" in v.summary()
