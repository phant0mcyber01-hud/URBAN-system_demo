// G5 / step 8: the WireGuard tunnel between a junction unit and its district, in QEMU (os/tests/vm_wireguard.py). Three
// machines under Secure Boot: the centre, a district and a unit behind it, the district (192.168.77.1) and the unit
// (192.168.77.2) on a wire of their own (QEMU socket cards in UDP mode; the unit's slirp card carries SSH and nothing
// else). QEMU itself records the wire at both ends and urban.net.capture judges each capture. Phases, each with its OK
// line: plain (the unit's first session crosses the wire in the clear and the judge catches it: the control), tunnel
// (wg0 on both from the centre's signed messages alone, a handshake, ping both ways, the unit proven and its TLS to the
// centre inside the tunnel without a restart of its process), wire (a fake camera in the unit's cabinet network hands a
// marked 1 MiB to the district through the tunnel whole, and the captures hold nothing but WireGuard), control (the same
// exchange in the clear is caught), vision path (the district's vision reads cameras only through the tunnel), outsider
// and relay allow (an unknown key and random datagrams get no answer, SSH to the district from the tunnel is closed, its
// 8443 answers from the wire and through the tunnel), mode only (8443 from the wire closed, through the tunnel open),
// down (the district's wg0 down: the camera unreachable even by a route an attacker adds, the unit's process kept, only
// WireGuard on the wire), clock (the unit's clock a year back: no wg0, then up by itself once it is right), revoke (the
// unit leaves the district's peers within seconds). The scenario prints WIREGUARD CHECK PASSED only when every phase
// passed. Emulated: a long run; one QEMU scenario at a time.
import { build, fail, scenario } from "./lib.mjs";

build("1", { image: true });
if (scenario("wireguard") !== 0) fail("wireguard scenario");
