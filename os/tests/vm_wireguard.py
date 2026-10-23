"""Step 8 (G5): the WireGuard tunnel between a junction unit and its district, in QEMU.

    python3 vm.py wireguard     (dispatched by vm.py; this module is not run on its own)

The chain's three machines (vm.chain_vms(link=True)): the centre (the pilot's district,center), the district and a unit
behind it. The district (192.168.77.1) and the unit (192.168.77.2) share a wire of their own, a QEMU socket card each;
the unit's slirp card carries SSH and nothing else (restrict=on: no time server, no other way out). QEMU itself records
the wire (filter-dump on the wire card, at both ends, into the container's /tmp; a copy goes to the logs) and
urban.net.capture judges every capture. A fake camera sits in the unit's cabinet network (a veth pair, the camera in a
network namespace); the district reads it with one RTSP DESCRIBE. The phases, in this order, each ending in its OK line:

  plain        the district enrolled at its address on the wire: the unit's first session crosses the wire in the
               clear, and both captures say so (not WireGuard, the TLS server name urban-center visible): the judge
               can see a plain exchange
  tunnel       from the centre's signed messages alone: wg0 on both, a handshake, 10.77.1.1 and 10.77.1.2 reach each
               other; the unit is proven and its session moves into the tunnel without a restart of its process
  wire         the camera's payload (1 MiB, marked at both ends) reaches the district through the tunnel whole, and both
               captures hold nothing but WireGuard (no mark, no RTSP, no TLS, no server name)
  control      the same exchange in the clear on the wire: the captures catch it (the mark and RTSP/1.0 found)
  vision path  the district's vision (its own user) reads the camera through the tunnel and nothing on the wire
  outsider     a WireGuard key the district does not know and random datagrams get no answer; SSH to the district
               from the tunnel is closed
  relay allow  the district's 8443 answers the unit from the wire and through the tunnel
  mode only    the admin closes the open port: 8443 from the wire is closed, through the tunnel open, the unit online
  down         the district's wg0 down: the unit's service keeps its process and takes no plain path, the camera is
               unreachable even by a route an attacker adds, the wire carries nothing but WireGuard
  clock        the unit started with its clock a year back: no wg0 until the clock is right, then it comes up by itself
  revoke       a revoked unit leaves the district's peers within seconds and its tunnel is dead

The last line is WIREGUARD CHECK PASSED. vm.py's names are taken from the running vm.py (`__main__`): importing it again
as `vm` would make a second Fail class that vm.py's `except Fail` does not catch.
"""
import calendar
import datetime
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import time

vm = sys.modules["__main__"]

sys.path.insert(0, "/work/backend")
from urban.net.capture import audit, classify, read_pcap  # noqa: E402  (the judge, stdlib only: it runs in this container)

DISTRICT_ID, UNIT_ID = "district-1", "edge-J-01"
EDGE = f"urban-edge@{UNIT_ID}"           # the unit's service
WIRE_D, WIRE_U = "192.168.77.1", "192.168.77.2"   # the district and the unit on the wire
TUN_D, TUN_U = "10.77.1.1", "10.77.1.2"           # the same inside the tunnel (district number 1, the unit's .2)
MACHINES = {WIRE_D, WIRE_U}
CAMERA = "192.168.50.11:554"            # the fake camera in the unit's cabinet network
TUNNEL_PORT = 8551                      # the camera's port on the unit's tunnel address (vm.CAMERAS)
CONTROL_PORT = 18554                    # the control: the same server on the unit's wire address, in the clear
SIZE = MIB = 1 << 20                    # the camera's payload: 1 MiB
LINK_MAC = "52:54:00:77:00:0[12]"       # the wire card of the district (:01) or of the unit (:02)
N0_MAC = "52:54:00:12:34:56"            # QEMU's own MAC for a machine's first card: the slirp one
FAKECAM, READER = "/tmp/fakecam.py", "/tmp/wireguard-reader.py"   # on the unit, on the district
CONTROL_RULE = 'insert rule inet urban input ip saddr 192.168.77.1 tcp dport 18554 accept comment "wireguard-check control"'
VISION_RULE = "urban-wg: the district's vision reads cameras only through the tunnel"
TUNNEL_ONLY = "urban-wg: tunnel addresses only through the tunnel"
TUNNEL_ONLINE = re.compile(r"edge: \S+ online at .*platform 10\.77\.1\.1:8443")
PLAIN_ONLINE = re.compile(r"edge: \S+ online at .*platform 192\.168\.77\.1:8443")   # the kit's way: the relay on the wire
# the unit service's lines about its session: online somewhere, or on its way to another session
SESSION = re.compile(r"edge: (\S+ online at |.*(reconnecting|retrying|switching to the tunnel|back to the plain path"
                     r"|waiting for the tunnel status))")

# The fake camera (on the unit, as root): one RTSP DESCRIBE per connection, answered with a payload whose bytes the
# harness knows from the seed. The same file run with --expect prints the payload's length and sha256 (in the container).
FAKECAM_CODE = r'''"""A camera for check-wireguard (os/tests/vm_wireguard.py): it answers an RTSP DESCRIBE with a known payload,
one per connection. The payload is the mark, a sha256 chain from the seed, the mark again: whoever has the seed knows
its sha256. --expect prints the payload's length and sha256 instead of serving (the harness's side of the same code)."""
import argparse
import hashlib
import socket
import threading


def payload(size, seed, mark):
    chain, block = bytearray(), bytes.fromhex(seed)
    while len(chain) < size - 2 * len(mark):
        block = hashlib.sha256(block).digest()
        chain += block
    return mark + bytes(chain[:size - 2 * len(mark)]) + mark


def answer(conn, data):
    with conn:
        conn.settimeout(60)
        asked = b""
        while b"\r\n\r\n" not in asked and len(asked) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                return
            asked += chunk
        if asked.startswith(b"DESCRIBE "):
            conn.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: %d\r\n\r\n" % len(data) + data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", help="host:port")
    ap.add_argument("--size", type=int, required=True)
    ap.add_argument("--seed", required=True, help="hex")
    ap.add_argument("--mark", required=True)
    ap.add_argument("--expect", action="store_true")
    args = ap.parse_args()
    data = payload(args.size, args.seed, args.mark.encode())
    if args.expect:
        print(len(data), hashlib.sha256(data).hexdigest())
        return
    host, _, port = args.listen.rpartition(":")
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, int(port)))
    server.listen(8)
    print(f"fakecam: {len(data)} bytes on {args.listen}", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=answer, args=(conn, data), daemon=True).start()


if __name__ == "__main__":
    main()
'''

# The district's reader (system python3, as root or as vision's user): one DESCRIBE, the answer's body read whole.
READER_CODE = r'''"""The district's side of check-wireguard (os/tests/vm_wireguard.py): one RTSP DESCRIBE to host:port and the answer's
body read whole (60 s without a byte, or 300 s in all, is a failure). Prints READ <bytes> <sha256> <mark found 0|1>,
or FAIL <what went wrong>."""
import hashlib
import re
import socket
import sys
import time

host, port, mark = sys.argv[1], int(sys.argv[2]), sys.argv[3].encode()
end = time.monotonic() + 300
try:
    with socket.create_connection((host, port), timeout=60) as s:
        s.sendall(f"DESCRIBE rtsp://{host}:{port}/stream1 RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
        data, size = bytearray(), None
        while size is None or len(data) < size:
            if time.monotonic() > end:
                raise TimeoutError("the whole answer took over 300 s")
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            data += chunk
            if size is None and b"\r\n\r\n" in data:
                head, _, rest = bytes(data).partition(b"\r\n\r\n")
                length = re.search(rb"(?im)^content-length:\s*(\d+)\s*$", head)
                if not head.startswith(b"RTSP/1.0 200") or not length:
                    raise ValueError(f"not an RTSP answer with a length: {head[:60]!r}")
                data, size = bytearray(rest), int(length.group(1))
    if size is None:
        raise ConnectionError("closed before the answer's header")
    body = bytes(data[:size])
    print(f"READ {len(body)} {hashlib.sha256(body).hexdigest()} {int(body.startswith(mark) and body.endswith(mark))}")
except (OSError, ValueError) as err:
    print(f"FAIL {type(err).__name__}: {getattr(err, 'strerror', None) or err}")
'''

# (a) of the outsider, on the unit: a WireGuard interface of its own with a fresh key the district has never heard of,
# peered with the district's real key at its real endpoint. Prints wg9's UDP port.
WG9 = r"""set -e
(umask 077; wg genkey > /tmp/wg9.key)
ip link add dev wg9 type wireguard
wg set wg9 private-key /tmp/wg9.key peer "$1" endpoint 192.168.77.1:51820 allowed-ips 10.99.0.1/32
ip address add 10.99.0.2/32 dev wg9
ip link set dev wg9 up
ip route add 10.99.0.1/32 dev wg9
wg show wg9 listen-port
"""

# (b) of the outsider, on the unit: what anyone on the wire can throw at the district's WireGuard port: datagrams shaped
# like a handshake initiation (148 bytes, type 1) and random ones, and one to the port next to it; then 5 s for any
# answer. Prints the sender's port, then SILENT or what came back.
GARBAGE = r"""python3 - <<'PY'
import os, random, socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("192.168.77.2", 0))
print("PORT", s.getsockname()[1], flush=True)
for _ in range(5):
    s.sendto(b"\x01\x00\x00\x00" + os.urandom(144), ("192.168.77.1", 51820))
for _ in range(3):
    s.sendto(os.urandom(random.randint(1, 1200)), ("192.168.77.1", 51820))
s.sendto(os.urandom(148), ("192.168.77.1", 51821))
s.settimeout(5)
try:
    data, peer = s.recvfrom(65536)
    print("ANSWER", peer[0], peer[1], len(data))
except socket.timeout:
    print("SILENT")
PY
"""


# ---------------------------------------------------------------- helpers

def until(what, timeout, test, every=3):
    """test() again and again until it gives something true, which is returned; at most `timeout` seconds."""
    end = time.time() + timeout
    while True:
        got = test()
        if got:
            return got
        if time.time() > end:
            raise vm.Fail(f"{what}: not within {timeout} s")
        time.sleep(every)


def new_mark():
    return "URBAN-MARK-" + secrets.token_hex(12)


def nic(m, mac):
    """The name of the machine's network card with this MAC address (a grep pattern)."""
    line = m.ssh(f"ip -o link | grep -i '{mac}'", check=False).stdout.strip().splitlines()
    if not line:
        raise vm.Fail(f"{m.name} has no network card with MAC {mac}")
    return line[0].split(": ")[1].split("@")[0].strip()


def pcap(phase, side):
    return f"/tmp/wireguard-{phase}-{side}.pcap"  # the container's own /tmp, not the /out bind mount


def capture_start(phase, ends):
    """QEMU records every frame of the machine's wire card (netdev up0) from now on, one pcap per end. The monitor reads
    no replies: the capture's file must appear (with its 24-byte pcap header) within 10 s."""
    for m, side in ends:
        if os.path.exists(pcap(phase, side)):
            os.remove(pcap(phase, side))
        vm.monitor(m, f"object_add filter-dump,id=cap-{phase},netdev=up0,file={pcap(phase, side)}")
    for m, side in ends:
        until(f"{phase}: {m.name} starting the capture {pcap(phase, side)} (object_add filter-dump)", 10,
              lambda: os.path.exists(pcap(phase, side)) and os.path.getsize(pcap(phase, side)) >= 24, every=0.5)


def capture_stop(phase, ends):
    """Ends the captures; each is copied to the logs and its frames are read from /tmp: {side: frames}."""
    for m, side in ends:
        vm.monitor(m, f"object_del cap-{phase}")
    frames = {}
    for m, side in ends:
        path = pcap(phase, side)
        with open(path, "rb") as f:
            raw = f.read()
        with open(f"{vm.LOGS}/{os.path.basename(path)}", "wb") as f:
            f.write(raw)
        frames[side] = read_pcap(raw)
    return frames


def sent_from(frames, src, sport):
    """How many UDP datagrams came from src's port sport."""
    return sum(1 for f in frames
               if (p := classify(f.data)).kind == "ipv4" and p.proto == 17 and p.src == src and p.sport == sport)


def answers(frames, src, ports):
    """Frames from src that answer one of these UDP ports: a datagram to it, or an ICMP error about a datagram from it.
    (UDP only: a TCP connection of the same phase may use the same port number by chance.)"""
    n = 0
    for f in frames:
        p = classify(f.data)
        if p.kind != "ipv4" or p.src != src or p.fragment:
            continue
        if p.proto == 17 and p.dport in ports:
            n += 1
        elif p.proto == 1:  # an ICMP error quotes the IP header and the first 8 bytes of the packet it is about
            ip = f.data[14:]
            icmp = ip[(ip[0] & 15) * 4:]
            if len(icmp) >= 8 + 20 + 4 and icmp[0] in (3, 11, 12):
                inner = icmp[8:]
                at = (inner[0] & 15) * 4
                if inner[9] == 17 and len(inner) >= at + 2 and struct.unpack("!H", inner[at:at + 2])[0] in ports:
                    n += 1
    return n


def put_text(m, text, path):
    """A script of ours onto the machine, readable by every user there; returns the container's copy."""
    local = f"/tmp/wg-local-{os.path.basename(path)}"
    with open(local, "w") as f:
        f.write(text)
    m.put(local, path)
    m.ssh(f"chmod 644 {path}")
    return local


def expected(local_fakecam, seed, mark):
    """(length, sha256) of the payload the fake camera serves for this seed and mark: the same script, run here."""
    out = vm.sh(["python3", local_fakecam, "--expect", "--size", str(SIZE), "--seed", seed, "--mark", mark]).stdout.split()
    return int(out[0]), out[1]


def read_camera(district, host, port, mark, vision=False):
    """The district's reader against host:port, as root or as vision's own user (urban-vision, uid 991).
    {"ok", "bytes", "sha", "mark", "said"}; ok is False with what it said when nothing was read."""
    who = "setpriv --reuid=urban-vision --regid=urban --clear-groups " if vision else ""
    # from /tmp: root's home, the SSH session's folder, is closed to vision's user
    out = district.ssh(f"cd /tmp && {who}python3 {READER} {host} {port} {mark}", check=False, timeout=420).stdout.strip()
    said = out.splitlines()[-1] if out else "(nothing)"
    got = re.fullmatch(r"READ (\d+) ([0-9a-f]{64}) ([01])", said)
    if not got:
        return {"ok": False, "said": said}
    return {"ok": True, "bytes": int(got.group(1)), "sha": got.group(2), "mark": got.group(3) == "1", "said": said}


def intact(got, want):
    return got["ok"] and (got["bytes"], got["sha"]) == want and got["mark"]


def wg_status(m):
    """The root helper's status file (/run/urban/wg.json), {} while there is none."""
    try:
        data = json.loads(m.ssh("cat /run/urban/wg.json", check=False).stdout)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def handshakes(m):
    """`wg show wg0 latest-handshakes`: {public key: unix time of the last handshake, 0 = none}."""
    out = m.ssh("wg show wg0 latest-handshakes", check=False).stdout
    return {p[0]: int(p[1]) for p in (line.split() for line in out.splitlines()) if len(p) == 2 and p[1].isdigit()}


def wg_tx(m):
    """Bytes wg0 has sent to its peers (`wg show wg0 transfer`: key, received, sent)."""
    out = m.ssh("wg show wg0 transfer", check=False).stdout
    return sum(int(p[2]) for p in (line.split() for line in out.splitlines()) if len(p) == 3 and p[2].isdigit())


def established(m):
    """The local and peer address:port of every established TCP connection of the machine."""
    return {tok for line in m.ssh("ss -Htn state established", check=False).stdout.splitlines() for tok in line.split()}


def tcp(m, host, port):
    """Does a TCP connection from the machine to host:port open within 8 s?"""
    return m.ssh(f"timeout 8 bash -c 'exec 3<>/dev/tcp/{host}/{port}'", check=False).returncode == 0


def service_state(m):
    """(MainPID, NRestarts) of the unit's service."""
    out = m.ssh(f"systemctl show -p MainPID -p NRestarts {EDGE}").stdout
    state = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return state.get("MainPID", ""), state.get("NRestarts", "")


def nft_counter(m, chain, comment):
    """Packets counted by the rule(s) with this comment in a chain of the firewall (inet urban)."""
    listing = m.ssh(f"nft list chain inet urban {chain}", check=False).stdout
    hits = [line for line in listing.splitlines() if f'comment "{comment}"' in line]
    if not hits:
        raise vm.Fail(f"{m.name}: no rule '{comment}' in chain {chain}:\n{listing[-800:]}")
    return sum(int(c) for line in hits for c in re.findall(r"counter packets (\d+)", line))


def drop_rule(m, chain, comment):
    """The rules with this comment taken out of a chain of the firewall (inet urban), by their handles."""
    for line in m.ssh(f"nft -a list chain inet urban {chain}").stdout.splitlines():
        if f'comment "{comment}"' in line and "# handle " in line:
            m.ssh(f"nft delete rule inet urban {chain} handle {line.rsplit('# handle ', 1)[1].strip()}")


def edge_log(unit, pid=None):
    """The unit service's journal of this boot; with pid, only the lines that process wrote."""
    which = f"_SYSTEMD_UNIT={EDGE}.service _PID={pid}" if pid else f"-u {EDGE}"
    return unit.ssh(f"journalctl -b {which} --no-pager -o cat", check=False).stdout


def tunnel_online_count(unit):
    return len(TUNNEL_ONLINE.findall(edge_log(unit)))


def wait_tunnel_online(unit, before, timeout):
    """The unit's service online through the tunnel: a connection established to 10.77.1.1:8443 and either a session
    begun after the `before` such lines of this boot, or (before > 0) the one before still the last word of its session
    (nothing since about closing or retrying). Returns which."""
    def online():
        lines = edge_log(unit).splitlines()
        if f"{TUN_D}:8443" not in established(unit):
            return None
        tunnel = [line for line in lines if TUNNEL_ONLINE.search(line)]
        last = [line for line in lines if SESSION.search(line)][-1:]
        if len(tunnel) > before:
            return "a new session"
        if before and len(tunnel) == before and last and TUNNEL_ONLINE.search(last[0]):
            return "its session kept"
        return None

    return until(f"the unit's service online through the tunnel ({TUN_D}:8443)", timeout, online, every=5)


def devices(api):
    """GET /api/devices as the admin: {device id: row}."""
    code, data = api.call("GET", "/api/devices")
    if code != 200 or not isinstance(data, list):
        raise vm.Fail(f"GET /api/devices answered {code}: {str(data)[:300]}")
    return {d.get("id"): d for d in data if isinstance(d, dict)}


def wg_row(rows, device):
    return (rows.get(device) or {}).get("wg") or {}


def wait_devices(api, what, ok, timeout=120):
    end, rows = time.time() + timeout, {}
    while time.time() < end:
        rows = devices(api)
        if ok(rows):
            return rows
        time.sleep(5)
    shown = {d: {"online": (rows.get(d) or {}).get("online"), "wg": wg_row(rows, d)} for d in (DISTRICT_ID, UNIT_ID)}
    raise vm.Fail(f"{what}: not within {timeout} s; Devices shows {json.dumps(shown, sort_keys=True)[:900]}")


def power_off(m):
    """A clean poweroff; the machine stays off (disk, firmware variables and TPM state kept) until power_on()
    (a copy of vm_pcr7.power_off)."""
    m._cycling = True
    m.ssh("systemctl poweroff", check=False)
    try:
        m.proc.wait(timeout=300)
    except subprocess.TimeoutExpired:
        pass
    m.cut_power()  # whatever is left of QEMU and the software TPM ends; nothing restarts the machine


# ---------------------------------------------------------------- the scenario

def wireguard():
    centre, district, unit = vm.chain_vms("wireguard", link=True)
    ends = ((district, "district"), (unit, "unit"))
    video_mark, control_mark = new_mark(), new_mark()
    # what must never cross the wire outside WireGuard: the marks, RTSP, the TLS server name (no 3-byte TLS header:
    # random ciphertext would hold it by chance)
    patterns = [video_mark.encode(), control_mark.encode(), b"RTSP/1.0", b"DESCRIBE", b"urban-center"]

    def judge(frames):
        return audit(frames, MACHINES, patterns)

    try:
        # ---- 0. boot: three machines under Secure Boot, the wire between the district and the unit works
        for m in (centre, district, unit):
            m.start()
        booted = {"centre": centre.booted("1", "A")}
        district.wait(r"URBAN-RELAY units -> 10\.0\.2\.2:28443 in the kernel")
        booted["district"] = district.booted("1", "A")
        booted["unit"] = unit.booted("1", "A")
        for who, line in booted.items():
            if "secureboot=on" not in line:
                raise vm.Fail(f"{who}: the self-check line lacks secureboot=on: {line}")
        # The unit has no time server (restrict=on), so its clock is the container's, which is the host's: on a host
        # whose own UTC is off (a laptop in the wrong time zone: 12 h) every command of the centre would reach the unit
        # stale. The true time is the centre's (it has its time server); the unit is set to it, as a real unit is by its
        # network, and every later clock step of this scenario counts from it too.
        def true_now():
            return int(centre.ssh("date -u +%s").stdout.strip())
        drift = int(unit.ssh("date -u +%s").stdout.strip()) - true_now()
        unit.ssh(f"date -u -s @{true_now()}")
        wire = {side: nic(m, LINK_MAC) for m, side in ends}
        slirp = {side: nic(m, N0_MAC) for m, side in ends}
        until(f"the unit's wire card {wire['unit']} with {WIRE_U}/24", 120,
              lambda: f"inet {WIRE_U}/24 " in unit.ssh(f"ip -4 -o addr show dev {wire['unit']}", check=False).stdout)
        until(f"a ping from the unit to the district on the wire ({WIRE_D})", 120,
              lambda: unit.ssh(f"ping -c 2 {WIRE_D}", check=False).returncode == 0)
        vm.push_city(centre)
        ctx = vm.centre_ca(centre)
        vm.health(ctx)
        # A TEST SETTING, not the product's default: no second factor for any role, so that the admin the centre made
        # at its first start can act through the API in this scenario (MFA_ROLES empty). Authentication is not what is
        # tested here (the accounts have their own tests); the tunnel's admin actions (mode only, revoke) are.
        centre.ssh("echo 'MFA_ROLES=' >> /var/lib/urban/etc/platform.env && systemctl restart urban-platform")
        vm.health(ctx)
        api = vm.CentreApi(centre, ctx)
        print(f"   boot: centre, district and unit up with secureboot=on; the wire: district {wire['district']} "
              f"{WIRE_D}, unit {wire['unit']} {WIRE_U} (ping), slirp cards {slirp['district']} / {slirp['unit']}; the unit's clock "
              f"set to the centre's ({drift:+d} s off before)", flush=True)

        # ---- 1. plain: the unit's first session over the wire in the clear, caught by the judge (the control)
        m0 = district.mark
        capture_start("plain", ends)
        vm.enrol_chain(centre, district, unit, address=WIRE_D)
        vm.wait_online(district, f"urban-district@{DISTRICT_ID}", "district")
        vm.wait_online(unit, EDGE, "edge")
        plain = {side: judge(frames) for side, frames in capture_stop("plain", ends).items()}
        env = unit.ssh(f"cat /etc/urban/edge-{UNIT_ID}.env").stdout
        platform = re.search(r"^PLATFORM=(\S+)\s*$", env, re.M)
        if not platform or platform.group(1) != f"{WIRE_D}:8443":
            raise vm.Fail(f"plain: the unit's kit says PLATFORM={platform.group(1) if platform else None}, want {WIRE_D}:8443")
        for side, v in plain.items():
            if not v.violations or b"urban-center" not in v.leaks:
                raise vm.Fail(f"plain ({side}): the control failed: the unit's first session in the clear is not caught "
                              f"(want something not WireGuard and the server name urban-center): {v.summary()}")
        print(f"   OK plain: the district enrolled at {WIRE_D}, the unit's kit says PLATFORM={WIRE_D}:8443 and its first "
              f"session crossed the wire in the clear: both captures name it not WireGuard and find the TLS server name "
              f"urban-center (district: {plain['district'].summary()}; unit: {plain['unit'].summary()})", flush=True)
        pid, _ = service_state(unit)

        # ---- 2. tunnel: from the centre's signed messages alone; the unit proven, its session inside the tunnel
        district.wait(r"URBAN-WG up district address=10\.77\.1\.1 port=51820", 600)
        district.mark = m0  # the list with the unit in it may come before the interface or after it
        district.wait(r"URBAN-WG applied district version=\d+ peers=1 mode=allow", 600)
        unit.wait(r"URBAN-WG applied unit version=\d+ address=10\.77\.1\.2 endpoint=192\.168\.77\.1:51820", 600)
        unit.wait(r"URBAN-WG up unit address=10\.77\.1\.2", 120)
        unit_key = str(wg_status(unit).get("public", ""))
        if len(unit_key) != 44:
            raise vm.Fail(f"tunnel: the unit's /run/urban/wg.json has no tunnel public key: {wg_status(unit)}")
        shook = until("tunnel: a handshake of the unit's key on the district (wg show wg0 latest-handshakes)", 120,
                      lambda: handshakes(district).get(unit_key, 0))
        for m, dst in ((district, TUN_U), (unit, TUN_D)):
            if m.ssh(f"ping -c 3 -W 2 {dst}", check=False).returncode != 0:
                raise vm.Fail(f"tunnel: {m.name} does not reach {dst} through the tunnel")
        unit.wait(r"URBAN-WG proven district=district-1 server=10\.77\.1\.1", 180)
        until("tunnel: the unit's journal 'edge: ... online at ... platform 10.77.1.1:8443'", 180,
              lambda: TUNNEL_ONLINE.search(edge_log(unit)), every=5)
        until(f"tunnel: an established connection of the unit to {TUN_D}:8443", 60,
              lambda: f"{TUN_D}:8443" in established(unit))
        until(f"tunnel: no established connection of the unit to {WIRE_D}:8443 any more", 120,
              lambda: f"{WIRE_D}:8443" not in established(unit))

        def tunnel_rows(rows):
            u, d = wg_row(rows, UNIT_ID), wg_row(rows, DISTRICT_ID)
            return ((rows.get(UNIT_ID) or {}).get("online") and u.get("proven") is True and u.get("address") == TUN_U
                    and u.get("behind") is False and d.get("index") == 1 and d.get("mode") == "allow"
                    and d.get("behind") is False)

        wait_devices(api, "tunnel: Devices shows edge-J-01 online and proven at 10.77.1.2, district-1 number 1 in mode "
                     "allow, neither behind", tunnel_rows)
        if service_state(unit)[0] != pid:
            raise vm.Fail(f"tunnel: the unit's service was restarted on its way into the tunnel (MainPID {pid} -> "
                          f"{service_state(unit)[0]})")
        own = edge_log(unit, pid)  # both sessions, the plain one and the one through the tunnel, opened by that process
        if not PLAIN_ONLINE.search(own) or not TUNNEL_ONLINE.search(own):
            raise vm.Fail(f"tunnel: the unit's process {pid} did not open both its plain session ({WIRE_D}:8443) and its "
                          f"session through the tunnel ({TUN_D}:8443); its journal:\n{own[-1200:]}")
        busy_u, busy_d = vm.busy_pct(unit), vm.busy_pct(district)
        print(f"   OK tunnel: from the centre's signed messages alone: the district's wg0 {TUN_D} on port 51820 with 1 peer "
              f"(mode allow), the unit's wg0 {TUN_U} to {WIRE_D}:51820; the unit's key shook hands with the district "
              f"(at {shook}), ping both ways; the unit proven and online through {TUN_D}:8443, nothing left to "
              f"{WIRE_D}:8443, by the process that opened its plain session (MainPID {pid}, not restarted); Devices: "
              f"edge-J-01 proven at {TUN_U}, district-1 "
              f"number 1 in mode allow, neither behind (busy unit {busy_u} %, district {busy_d} %)", flush=True)

        # ---- 3. wire, control and vision path: the camera through the tunnel, the same in the clear, vision's reach
        seed, control_seed = secrets.token_hex(16), secrets.token_hex(16)
        local = put_text(unit, FAKECAM_CODE, FAKECAM)
        put_text(district, READER_CODE, READER)
        want, control_want = expected(local, seed, video_mark), expected(local, control_seed, control_mark)
        unit.ssh(vm.CABINET)
        unit.ssh(f"systemd-run --unit=urban-fakecam ip netns exec cab /usr/bin/python3 {FAKECAM} --listen {CAMERA} "
                 f"--size {SIZE} --seed {seed} --mark {video_mark}")
        until(f"the fake camera listening on {CAMERA}", 60,
              lambda: re.search(r":554\b", unit.ssh("ip netns exec cab ss -Hltn", check=False).stdout))
        unit.ssh(vm.CAMERAS)
        unit.wait(r"URBAN-WG cameras 1 forwarded", 300)
        keys = {"net.ipv4.ip_forward": "0", "net.ipv4.conf.all.forwarding": "0", "net.ipv4.conf.wg0.forwarding": "1",
                "net.ipv4.conf.cab0.forwarding": "1", f"net.ipv4.conf.{wire['unit']}.forwarding": "0",
                f"net.ipv4.conf.{slirp['unit']}.forwarding": "0"}
        out = unit.ssh("for k in " + " ".join(keys) + "; do echo \"$k $(sysctl -n $k 2>/dev/null)\"; done").stdout
        got = {line.split(" ", 1)[0]: line.partition(" ")[2].strip() for line in out.splitlines() if line.strip()}
        wrong = {k: (got.get(k), v) for k, v in keys.items() if got.get(k) != v}
        if wrong:
            raise vm.Fail(f"wire: the unit's forwarding is not on for wg0 and cab0 only: {wrong} (got, want)")

        tx0 = wg_tx(unit)
        capture_start("wire", ends)
        got = read_camera(district, TUN_U, TUNNEL_PORT, video_mark)
        wire_v = {side: judge(frames) for side, frames in capture_stop("wire", ends).items()}
        tx = wg_tx(unit) - tx0
        if not intact(got, want):
            raise vm.Fail(f"wire: the district did not read the camera whole through {TUN_U}:{TUNNEL_PORT}: '{got['said']}' "
                          f"(want {want[0]} bytes, sha256 {want[1]}, the mark at both ends)")
        for side, v in wire_v.items():
            if not v.clean or 4 not in v.by_type or v.wg_bytes < MIB:
                raise vm.Fail(f"wire ({side}): the capture is not WireGuard alone carrying the payload (want clean, data "
                              f"messages, at least {MIB} bytes): {v.summary()}; {v.violations[:5]}; "
                              f"{ {p.decode(errors='replace'): n for p, n in v.leaks.items()} }")
        if tx < MIB:
            raise vm.Fail(f"wire: the unit's wg0 sent {tx} bytes during the read, less than the payload ({MIB})")
        print(f"   OK wire: the district read the camera through {TUN_U}:{TUNNEL_PORT}: {got['bytes']} bytes, sha256 "
              f"{got['sha'][:16]}..., the mark at both ends; both captures hold nothing but WireGuard (district: "
              f"{wire_v['district'].summary()}; unit: {wire_v['unit'].summary()}); the unit's wg0 sent {tx} bytes", flush=True)

        capture_start("control", ends)
        unit.ssh(f"systemd-run --unit=urban-fakecam-control /usr/bin/python3 {FAKECAM} --listen {WIRE_U}:{CONTROL_PORT} "
                 f"--size {SIZE} --seed {control_seed} --mark {control_mark}")
        unit.ssh(f"nft '{CONTROL_RULE}'")
        until(f"the control server listening on {WIRE_U}:{CONTROL_PORT}", 60,
              lambda: f"{WIRE_U}:{CONTROL_PORT}" in unit.ssh("ss -Hltn", check=False).stdout)
        got = read_camera(district, WIRE_U, CONTROL_PORT, control_mark)
        control_v = {side: judge(frames) for side, frames in capture_stop("control", ends).items()}
        if not intact(got, control_want):
            raise vm.Fail(f"control: the same exchange in the clear did not arrive whole: '{got['said']}'")
        for side, v in control_v.items():
            if not v.violations or control_mark.encode() not in v.leaks or b"RTSP/1.0" not in v.leaks:
                raise vm.Fail(f"control ({side}): the judge does not catch the exchange in the clear (want something not "
                              f"WireGuard, the mark and RTSP/1.0): {v.summary()}")
        print(f"   OK control: the same exchange in the clear ({WIRE_U}:{CONTROL_PORT}) arrived whole and both captures catch "
              f"it: not WireGuard, the mark and RTSP/1.0 found (district: {control_v['district'].summary()}; unit: "
              f"{control_v['unit'].summary()})", flush=True)

        got = read_camera(district, TUN_U, TUNNEL_PORT, video_mark, vision=True)
        if not intact(got, want):
            raise vm.Fail(f"vision path: the district's vision does not read the camera whole through the tunnel: '{got['said']}'")
        before = nft_counter(district, "wg_vision", VISION_RULE)
        got = read_camera(district, WIRE_U, CONTROL_PORT, control_mark, vision=True)
        after = nft_counter(district, "wg_vision", VISION_RULE)
        if got["ok"]:
            raise vm.Fail(f"vision path: the district's vision reads {WIRE_U}:{CONTROL_PORT} on the wire: '{got['said']}'")
        if after <= before:
            raise vm.Fail(f"vision path: the read on the wire failed, but not by the rule '{VISION_RULE}' ({before} -> {after})")
        print(f"   OK vision path: as urban-vision the district reads the camera through the tunnel whole, and not "
              f"{WIRE_U}:{CONTROL_PORT} on the wire ('{got['said']}'), which root just read; the counter of "
              f"'{VISION_RULE}' rose {before} -> {after}", flush=True)
        drop_rule(unit, "input", "wireguard-check control")
        unit.ssh("systemctl stop urban-fakecam-control", check=False)

        # ---- 4. outsider (mode allow): an unknown key and random datagrams get no answer; the relay from both sides
        capture_start("outsider", ((district, "district"),))
        district_key = str(wg_status(district).get("public", ""))
        if len(district_key) != 44:
            raise vm.Fail(f"outsider: the district's /run/urban/wg.json has no tunnel public key: {wg_status(district)}")
        port9 = vm.run_script(unit, WG9, district_key).stdout.split()[-1]
        t9 = time.time()
        if unit.ssh("ping -c 5 10.99.0.1", check=False).returncode == 0:
            raise vm.Fail("outsider: a WireGuard key the district does not know got through to it (ping 10.99.0.1)")
        time.sleep(max(0.0, t9 + 20 - time.time()))
        shakes = [p[1] for p in (line.split() for line in unit.ssh("wg show wg9 latest-handshakes").stdout.splitlines())
                  if len(p) == 2]
        if shakes != ["0"]:
            raise vm.Fail(f"outsider: the unknown key shook hands with the district (wg9 latest-handshakes {shakes})")
        thrown = vm.run_script(unit, GARBAGE).stdout.split()
        if "SILENT" not in thrown:
            raise vm.Fail(f"outsider: garbage to the district's WireGuard port got an answer: {' '.join(thrown)}")
        port_g = thrown[thrown.index("PORT") + 1]
        relay_wire, relay_tunnel = tcp(unit, WIRE_D, 8443), tcp(unit, TUN_D, 8443)
        ssh_tunnel, ssh_wire = tcp(unit, TUN_D, 22), tcp(unit, WIRE_D, 22)
        frames = capture_stop("outsider", ((district, "district"),))["district"]
        unit.ssh("ip link del wg9; rm -f /tmp/wg9.key", check=False)
        if not port9.isdigit() or not port_g.isdigit():
            raise vm.Fail(f"outsider: no UDP port of wg9 ({port9!r}) or of the garbage sender ({port_g!r})")
        sent9, sent_g = sent_from(frames, WIRE_U, int(port9)), sent_from(frames, WIRE_U, int(port_g))
        back = answers(frames, WIRE_D, {int(port9), int(port_g)})
        if not sent9 or not sent_g:
            raise vm.Fail(f"outsider: control failed: the district's capture holds {sent9} frame(s) from wg9's port {port9} "
                          f"and {sent_g} from the garbage sender's port {port_g} (want at least one of each)")
        if back:
            raise vm.Fail(f"outsider: the district answered the unknown key or the garbage: {back} frame(s) back to "
                          f"ports {port9} / {port_g}")
        if ssh_tunnel:
            raise vm.Fail(f"outsider: SSH to the district is open from the tunnel ({TUN_D}:22)")
        if not ssh_wire:
            raise vm.Fail(f"outsider: control failed: SSH to the district from the wire ({WIRE_D}:22) does not open")
        print(f"   OK outsider: wg9 with a key the district never heard of (port {port9}) got no handshake (ping fails, "
              f"latest handshake 0 after 20 s); 8 garbage datagrams to 51820 and one to 51821 got no answer in 5 s; the "
              f"district's capture: {sent9} frame(s) from wg9's port and {sent_g} from the sender's (control), none back "
              f"to either; SSH to the district from the tunnel ({TUN_D}:22) closed, from the wire open (control)", flush=True)
        if not relay_wire or not relay_tunnel:
            raise vm.Fail(f"relay allow: the district's 8443 from the wire {'answers' if relay_wire else 'does not answer'}, "
                          f"through the tunnel {'answers' if relay_tunnel else 'does not answer'} (want both in mode allow)")
        print(f"   OK relay allow: the district's 8443 answers the unit from the wire ({WIRE_D}:8443) and through the "
              f"tunnel ({TUN_D}:8443)", flush=True)

        # ---- 5. mode only: the admin closes the district's open port
        m5 = district.mark
        code, data = api.call("POST", f"/api/devices/{DISTRICT_ID}/wg-mode", {"mode": "only"})
        if code != 200 or not isinstance(data, dict) or data.get("unproven") != []:
            raise vm.Fail(f"mode only: POST wg-mode answered {code} {str(data)[:300]} (want 200 with unproven [])")
        district.wait(r"URBAN-WG applied district version=\d+ peers=1 mode=only", 120)
        district.mark = m5  # the relay's line and the helper's come from two services
        district.wait(r"URBAN-RELAY mode only", 120)
        if tcp(unit, WIRE_D, 8443):
            raise vm.Fail(f"mode only: the district's 8443 still answers from the wire ({WIRE_D}:8443)")
        if not tcp(unit, TUN_D, 8443):
            raise vm.Fail(f"mode only: the district's 8443 no longer answers through the tunnel ({TUN_D}:8443)")
        wait_devices(api, "mode only: Devices shows edge-J-01 online and district-1 in mode only",
                     lambda rows: (rows.get(UNIT_ID) or {}).get("online") and wg_row(rows, DISTRICT_ID).get("mode") == "only")
        print(f"   OK mode only: the admin's POST wg-mode only (unproven []): the district applied the list with mode=only "
              f"and its relay switched; 8443 no longer answers from the wire, still through the tunnel; edge-J-01 stays "
              f"online at the centre", flush=True)

        # ---- 6. down: the district's wg0 down; no plain path for the unit's session, the camera unreachable
        sessions = tunnel_online_count(unit)
        capture_start("down", ends)
        t_down = time.time()
        district.ssh("ip link set dev wg0 down")
        before = nft_counter(district, "output", TUNNEL_ONLY)
        # the route an attacker on the wire would add (replace: the same whether or not the kernel kept wg0's own)
        district.ssh(f"ip route replace 10.77.1.0/24 via {WIRE_U} dev {wire['district']}")
        # the reader has 10 s: a SYN dropped on its way out leaves connect() waiting, so "fails" is "reads nothing" (it
        # says FAIL, or timeout ends it), and the counter below says where its packets stopped
        said = district.ssh(f"timeout 10 python3 {READER} {TUN_U} {TUNNEL_PORT} {video_mark}; echo \"rc $?\"",
                            check=False).stdout.strip().splitlines()
        after = nft_counter(district, "output", TUNNEL_ONLY)
        district.ssh(f"ip route del 10.77.1.0/24 via {WIRE_U}", check=False)
        read = next((line for line in said if line.startswith(("READ ", "FAIL "))), "")
        if read.startswith("READ "):
            raise vm.Fail(f"down: the district read the camera at {TUN_U}:{TUNNEL_PORT} over the wire with its tunnel down: "
                          f"'{read}'")
        read = read or ("no byte in 10 s" if "rc 124" in said else f"nothing read: {' '.join(said)[-120:]}")
        if after <= before:
            raise vm.Fail(f"down: the read failed ({read}), but not by the rule '{TUNNEL_ONLY}' ({before} -> {after})")
        while time.time() < t_down + 60:
            plain_left = sorted(t for t in established(unit) if t.startswith(f"{WIRE_D}:"))
            if plain_left:
                raise vm.Fail(f"down: the unit opened a connection to the district outside the tunnel: {plain_left}")
            time.sleep(5)
        now_pid, restarts = service_state(unit)
        if now_pid != pid or restarts != "0":
            raise vm.Fail(f"down: the unit's service did not keep its process (MainPID {pid} -> {now_pid}, {restarts} restarts)")
        down_v = {side: judge(frames) for side, frames in capture_stop("down", ends).items()}
        for side, v in down_v.items():
            if not v.clean or not v.wg_frames:
                raise vm.Fail(f"down ({side}): with the district's tunnel down the wire carries something else, or nothing "
                              f"of WireGuard: {v.summary()}; {v.violations[:5]}")
        district.ssh("ip link set dev wg0 up")
        how = wait_tunnel_online(unit, sessions, 300)
        print(f"   OK down: the district's wg0 down for {time.time() - t_down:.0f} s: its reader to {TUN_U}:{TUNNEL_PORT}, "
              f"with an attacker's route over the wire, fails ({read}) and the counter of '{TUNNEL_ONLY}' rose "
              f"{before} -> {after}; the unit's service kept its process ({pid}, 0 restarts) and opened nothing to "
              f"{WIRE_D}; both captures hold only WireGuard (district: {down_v['district'].summary()}; unit: "
              f"{down_v['unit'].summary()}); wg0 up again: the unit online through the tunnel ({how})", flush=True)

        # ---- 7. clock: the unit started with its clock a year back
        power_off(unit)
        # the baseline: the district's last handshake time of the unit's key, read once the unit is off (a handshake its
        # shutdown still made is one from before the clock went back)
        baseline = handshakes(district).get(unit_key, 0)
        capture_start("clock", ((district, "district"),))  # from before power-on until the unit is back in the tunnel
        unit.rtc = (datetime.datetime.fromtimestamp(true_now(), datetime.timezone.utc)
                    - datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
        unit.mark = m7 = len(unit.lines)
        unit.power_on()
        unit.booted("1", "A")
        unit.rtc = None
        built = unit.ssh("sed -n 's/^BUILT=//p' /etc/urban-os-release").stdout.strip()
        try:
            built_s = calendar.timegm(time.strptime(built, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            raise vm.Fail(f"clock: /etc/urban-os-release has no BUILT= the clock rule can use ({built!r})")
        clock_s = int(unit.ssh("date -u +%s").stdout.strip())
        if clock_s >= built_s:
            raise vm.Fail(f"clock: control failed: the unit's clock ({clock_s}) is not before the build ({built}, {built_s}): "
                          "something set it forward at boot")
        unit.mark = m7  # the helper's first run comes early in the boot, maybe before the self-check line
        unit.wait(r"URBAN-WG clock waiting", 300)
        st = until("clock: the unit's wg.json in state clock", 60, lambda: (s := wg_status(unit)).get("state") == "clock" and s)
        if st.get("proven") is not True:
            raise vm.Fail(f"clock: the unit is no longer proven while it waits for the clock: {st}")
        if unit.ssh("ip link show wg0", check=False).returncode == 0:
            raise vm.Fail("clock: wg0 exists although the unit's clock is before the build")
        watch = time.time() + 30  # 30 s: nothing of the unit's reaches the district's tunnel (0: the 600 s peer reset)
        while time.time() < watch:
            seen = handshakes(district).get(unit_key, 0)
            if seen > baseline:
                raise vm.Fail(f"clock: a new handshake of the unit on the district ({seen} > {baseline}) with its clock back")
            time.sleep(3)
        set_at = int(district.ssh("date +%s").stdout.strip())  # the district's own clock: its handshake times are its own
        unit.ssh(f"date -u -s @{true_now()}")
        unit.wait(r"URBAN-WG up unit address=10\.77\.1\.2", 90)
        shook = until(f"clock: a handshake of the unit on the district at {set_at} or later", 120,
                      lambda: (t := handshakes(district).get(unit_key, 0)) >= set_at and t)
        how = wait_tunnel_online(unit, 0, 300)
        clock_v = judge(capture_stop("clock", ((district, "district"),))["district"])
        if not clock_v.clean or not {1, 2, 4} <= set(clock_v.by_type):
            raise vm.Fail(f"clock: the capture from before power-on until the unit was back in the tunnel is not WireGuard "
                          f"alone with handshakes and data (types 1, 2, 4): {clock_v.summary()}; {clock_v.violations[:5]}")
        print(f"   OK clock: the unit started with its clock a year back ({clock_s} < the build {built}): 'URBAN-WG clock "
              f"waiting', wg.json state clock and still proven, no wg0, no new handshake on the district for 30 s; the "
              f"clock set: wg0 up by itself, a handshake at {shook} (>= {set_at}), online through the tunnel ({how}); the "
              f"capture from before power-on until then holds only WireGuard with types 1, 2 and 4 (no plain fallback at "
              f"boot, the TLS handshake inside the tunnel): {clock_v.summary()}", flush=True)

        # ---- 8. revoke (last): the unit leaves the district's peers within seconds
        m8 = district.mark
        t0 = time.time()
        code, data = api.call("POST", f"/api/devices/{UNIT_ID}/status", {"status": "revoked"})
        if code != 200:
            raise vm.Fail(f"revoke: POST status revoked answered {code}: {str(data)[:300]}")
        until("revoke: the unit's key gone from the district's wg0 (wg show wg0 peers)", 30,
              lambda: unit_key not in district.ssh("wg show wg0 peers", check=False).stdout.split(), every=2)
        t1 = time.time()
        district.mark = m8
        district.wait(r"URBAN-WG applied district version=\d+ peers=0 mode=only", 120)
        if unit.ssh(f"ping -c 3 {TUN_D}", check=False).returncode == 0:
            raise vm.Fail(f"revoke: the revoked unit still reaches {TUN_D} through the tunnel")
        print(f"   OK revoke: edge-J-01 revoked through the API: its key left the district's wg0 (the list with peers=0 "
              f"mode=only applied), its ping to {TUN_D} fails, in {t1 - t0:.0f} s", flush=True)
        print("WIREGUARD CHECK PASSED", flush=True)
    finally:
        for m in (centre, district, unit):
            vm.drop(m)
