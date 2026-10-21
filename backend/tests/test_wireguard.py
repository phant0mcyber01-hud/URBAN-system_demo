"""The WireGuard tunnel between a junction unit and its district (docs/wireguard-design.md), the parts that need no
kernel: keys in the text form of `wg`, a stable collision-free addressing plan, the peer list a district applies and the
link a unit applies (both signed by the gateway key and refused when forged, misaddressed or replayed), the text handed to
`wg` and `ip`, the handshake ages, and the port plan of the cameras behind a unit."""
import base64
import copy
import json
import os

import pytest

from urban.devices import crypto
from urban.edge.device import envelope_refusal
from urban.net import wireguard as wg

PLAN = wg.Plan.of()
GATEWAY = crypto.new_key()
GATEWAY_PUBLIC = GATEWAY.public_key()
NOW = 1_800_000_000


def key():
    return wg.new_keypair()[1]


def peers(*units, index=1):
    return [wg.Peer(u, key(), PLAN.host(index, 2 + i)) for i, u in enumerate(units)]


def listing(version=1, units=("edge-J-01", "edge-J-02"), plain=True, index=1, district="district-1"):
    return wg.make_peer_list(PLAN, district, index, version, NOW, peers(*units, index=index), plain)


def wire(msg):
    """What the message looks like after the trip over the link."""
    return json.loads(json.dumps(msg))


# ---- keys ------------------------------------------------------------------------------------------------------

def test_a_public_key_is_what_wg_pubkey_says():
    # RFC 7748 section 6.1, Alice: the scalar is given unclamped, and the public key is the clamped scalar's
    private = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    public = bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    assert wg.public_of(base64.b64encode(private).decode()) == base64.b64encode(public).decode()


def test_a_new_key_is_like_wg_genkey_makes_it():
    private, public = wg.new_keypair()
    raw = base64.b64decode(private)
    assert len(private) == len(public) == 44 and private.endswith("=")
    assert raw[0] & 7 == 0 and raw[31] & 128 == 0 and raw[31] & 64 == 64  # clamped
    assert wg.check_key(private) == private and wg.check_key(public) == public
    assert wg.public_of(private) == public
    assert wg.new_private() != wg.new_private()
    assert len(wg.fingerprint(public)) == 19 and wg.fingerprint(public).count("-") == 3
    assert wg.fingerprint(public) == wg.fingerprint(f" {public}\n")


@pytest.mark.parametrize("bad", [
    "", "abc", None, 12, base64.b64encode(bytes(31)).decode(), base64.b64encode(bytes(33)).decode(),
    base64.b64encode(bytes(32)).decode(),                      # all zero
    "A" * 43 + "B",                                            # right length, not the canonical spelling of 32 bytes
    "!" * 44, base64.urlsafe_b64encode(b"\xfb" * 32).decode(),  # not base64 / the url alphabet
])
def test_a_text_that_is_no_key_is_refused(bad):
    with pytest.raises(wg.WireGuardError):
        wg.check_key(bad)


def test_the_key_file_is_private_and_never_overwritten(tmp_path):
    path = tmp_path / "wg" / "edge-J-01.key"
    public = wg.write_private(path)
    assert wg.public_of(wg.read_private(path)) == public
    if os.name != "nt":  # Windows has no such mode bits; the unit is Linux
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        wg.write_private(path)
    assert wg.public_of(wg.read_private(path)) == public  # untouched
    assert wg.write_private(tmp_path / "given.key", wg.new_private())  # a given key is stored as it is


# ---- addresses -------------------------------------------------------------------------------------------------

def test_the_plan_gives_each_district_its_network():
    assert str(PLAN.network(1)) == "10.77.1.0/24" and str(PLAN.network(254)) == "10.77.254.0/24"
    assert PLAN.server(7) == "10.77.7.1" and PLAN.host(7, 2) == "10.77.7.2" and PLAN.host(7, 254) == "10.77.7.254"
    assert str(wg.Plan.of("172.31.0.0/16").network(3)) == "172.31.3.0/24"
    for bad in (0, 255, -1, True, "1", None):
        with pytest.raises(wg.AddressError):
            PLAN.network(bad)
    for bad in (1, 255, 0, True):
        with pytest.raises(wg.AddressError):
            PLAN.host(1, bad)
    for base in ("8.8.0.0/16", "10.0.0.0/8", "10.77.0.0/24", "nonsense", "10.77.1.0/16"):
        with pytest.raises(wg.AddressError):
            wg.Plan.of(base)


@pytest.mark.parametrize("ip", ["10.77.1.1", "10.77.1.0", "10.77.1.255", "10.77.2.5", "192.168.1.5", "fd00::2", "10.77.1", "", None])
def test_only_a_unit_address_of_the_districts_own_network_is_a_unit_address(ip):
    with pytest.raises(wg.AddressError):
        PLAN.check_unit_ip(1, ip)


def test_a_units_address_is_the_lowest_free_and_never_moves():
    assigned = {}
    for i, unit in enumerate(["edge-J-01", "edge-J-02", "edge-J-03"]):
        assigned[unit] = wg.assign_unit(PLAN, 1, unit, assigned)
        assert assigned[unit] == f"10.77.1.{2 + i}"
    assert wg.assign_unit(PLAN, 1, "edge-J-02", assigned) == "10.77.1.3"        # asked again: the same
    del assigned["edge-J-02"]
    assert wg.assign_unit(PLAN, 1, "edge-J-09", assigned) == "10.77.1.3"        # a hole is filled
    assert wg.assign_unit(PLAN, 1, "edge-J-01", assigned) == "10.77.1.2"        # the others stayed where they were
    assert wg.assign_unit(PLAN, 2, "edge-J-01", {}) == "10.77.2.2"              # another district, another network


def test_a_revoked_units_address_is_not_given_to_another_unit():
    assigned = {"edge-J-01": "10.77.1.2"}
    retired = ["10.77.1.3", "10.77.1.4"]
    assert wg.assign_unit(PLAN, 1, "edge-J-02", assigned, retired) == "10.77.1.5"
    assert wg.assign_unit(PLAN, 1, "edge-J-01", assigned, retired) == "10.77.1.2"


def test_a_collision_or_a_stray_address_in_the_records_is_refused():
    with pytest.raises(wg.AddressError, match="both have 10.77.1.2"):
        wg.assign_unit(PLAN, 1, "edge-J-03", {"edge-J-01": "10.77.1.2", "edge-J-02": "10.77.1.2"})
    with pytest.raises(wg.AddressError):
        wg.assign_unit(PLAN, 1, "edge-J-03", {"edge-J-01": "10.77.2.2"})  # another district's address
    with pytest.raises(wg.AddressError):
        wg.assign_unit(PLAN, 1, "edge-J-03", {"edge-J-01": "10.77.1.1"})  # the district's own
    for bad in ("district-1", "center", "edge-", "edge J-1", "../edge-J-1", ""):
        with pytest.raises(wg.AddressError):
            wg.assign_unit(PLAN, 1, bad, {})
    assert wg.assign_unit(PLAN, 1, "light-C-1", {}) == "10.77.1.2"  # a lighting cabinet's unit is a peer too


def test_a_full_district_network_is_said_so_not_wrapped_around():
    assigned = {}
    for i in range(wg.MAX_PEERS):
        assigned[f"edge-J-{i:03d}"] = wg.assign_unit(PLAN, 1, f"edge-J-{i:03d}", assigned)
    assert len(set(assigned.values())) == 253 and assigned["edge-J-252"] == "10.77.1.254"
    with pytest.raises(wg.AddressError, match="no free address"):
        wg.assign_unit(PLAN, 1, "edge-X-1", assigned)
    assert wg.assign_unit(PLAN, 1, "edge-J-100", assigned) == assigned["edge-J-100"]  # the ones it has still answer


def test_a_district_keeps_its_number():
    assigned = {}
    for name, want in (("district-1", 1), ("district-north", 2), ("district-3", 3)):
        assigned[name] = wg.assign_index(name, assigned)
        assert assigned[name] == want
    assert wg.assign_index("district-north", assigned) == 2
    del assigned["district-north"]
    assert wg.assign_index("district-new", assigned) == 2
    with pytest.raises(wg.WireGuardError):
        wg.assign_index("edge-J-01", {})
    with pytest.raises(wg.AddressError):
        wg.assign_index("district-9", {"district-1": 300})
    full = {f"district-{i}": i for i in range(1, 255)}
    with pytest.raises(wg.AddressError, match="taken"):
        wg.assign_index("district-x", full)


# ---- the peer list ---------------------------------------------------------------------------------------------

def test_a_peer_list_is_the_same_whatever_order_it_was_made_in():
    ps = peers("edge-J-02", "edge-J-01", "edge-J-03")
    a = wg.make_peer_list(PLAN, "district-1", 1, 4, NOW, ps)
    b = wg.make_peer_list(PLAN, "district-1", 1, 4, NOW, list(reversed(ps)))
    assert a == b and [p.unit for p in a.peers] == ["edge-J-01", "edge-J-02", "edge-J-03"]
    assert crypto.canonical(a.body()) == crypto.canonical(b.body())
    assert a.by_unit()["edge-J-01"].ip == "10.77.1.3"  # peers() numbered them in the order given (J-02 first)


def test_a_peer_list_that_would_clash_is_not_made():
    k, other = key(), key()
    with pytest.raises(wg.WireGuardError, match="same unit"):
        wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [wg.Peer("edge-J-01", k, "10.77.1.2"), wg.Peer("edge-J-01", other, "10.77.1.3")])
    with pytest.raises(wg.WireGuardError, match="same key"):
        wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [wg.Peer("edge-J-01", k, "10.77.1.2"), wg.Peer("edge-J-02", k, "10.77.1.3")])
    with pytest.raises(wg.WireGuardError, match="same address"):
        wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [wg.Peer("edge-J-01", k, "10.77.1.2"), wg.Peer("edge-J-02", other, "10.77.1.2")])
    with pytest.raises(wg.AddressError):
        wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [wg.Peer("edge-J-01", k, "10.77.2.2")])
    with pytest.raises(wg.WireGuardError):
        wg.make_peer_list(PLAN, "edge-J-01", 1, 1, NOW, [])                                       # not a district
    with pytest.raises(wg.WireGuardError):
        wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [wg.Peer("district-2", k, "10.77.1.2")])  # a district is no peer
    with pytest.raises(wg.WireGuardError):
        wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [{"unit": "edge-J-01"}])
    with pytest.raises(wg.WireGuardError):
        wg.make_peer_list(PLAN, "district-1", 1, -1, NOW, [])
    with pytest.raises(wg.WireGuardError):
        wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [], plain_relay="yes")
    empty = wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [])
    assert empty.peers == ()  # a district with no unit yet is a valid list (it listens and nobody can come in)


def test_a_signed_peer_list_is_obeyed_by_its_district():
    lst = listing(version=5)
    msg = wire(wg.sign_message(GATEWAY, lst.body()))
    got = wg.parse_peer_list(msg, GATEWAY_PUBLIC, PLAN, "district-1", last_version=4)
    assert got == lst
    assert wg.parse_peer_list(msg, GATEWAY_PUBLIC, PLAN, "district-1", last_version=5) == lst  # again: the same, harmless
    assert wg.parse_peer_list(msg, GATEWAY_PUBLIC, PLAN, "district-1") == lst


def test_a_peer_list_that_is_forged_or_misdirected_is_refused():
    lst = listing(version=5)
    msg = wire(wg.sign_message(GATEWAY, lst.body()))
    attacker = crypto.new_key()
    refuse = lambda m, **kw: pytest.raises(wg.MessageRefused, match=kw.get("match", "."))
    with refuse(msg, match="bad signature"):  # signed by someone who is not the gateway
        wg.parse_peer_list(wire(wg.sign_message(attacker, lst.body())), GATEWAY_PUBLIC, PLAN, "district-1")
    for tamper in (lambda m: m["peers"].append({"unit": "edge-X-1", "public": key(), "ip": "10.77.1.99"}),
                   lambda m: m["peers"][0].update(public=key()),
                   lambda m: m["peers"][0].update(ip="10.77.1.200"),
                   lambda m: m.update(version=99), lambda m: m.update(plain_relay=False),
                   lambda m: m.update(district="district-2")):
        forged = copy.deepcopy(msg)
        tamper(forged)
        with refuse(forged):
            wg.parse_peer_list(forged, GATEWAY_PUBLIC, PLAN, "district-1")
    with refuse(msg, match="addressed to district-1, this is district-2"):
        wg.parse_peer_list(msg, GATEWAY_PUBLIC, PLAN, "district-2")
    with refuse(msg, match="older than the 6"):
        wg.parse_peer_list(msg, GATEWAY_PUBLIC, PLAN, "district-1", last_version=6)  # a replay of an old list
    with refuse(msg, match="no gateway key"):
        wg.parse_peer_list(msg, None, PLAN, "district-1")
    for bad in (None, [], "x", {}, {"type": "cmd"}):
        with refuse(msg):
            wg.parse_peer_list(bad, GATEWAY_PUBLIC, PLAN, "district-1")
    unsigned = {k: v for k, v in msg.items() if k != "sig"}
    with refuse(msg, match="bad signature"):
        wg.parse_peer_list(unsigned, GATEWAY_PUBLIC, PLAN, "district-1")


def test_a_correctly_signed_but_malformed_list_is_refused_too():
    base = listing().body()
    for change in ({"extra": 1}, {"peers": "none"}, {"index": 300}, {"plain_relay": "no"}, {"district": "edge-J-01"}):
        body = {**base, **change}
        with pytest.raises(wg.MessageRefused):
            wg.parse_peer_list(wire(wg.sign_message(GATEWAY, body)), GATEWAY_PUBLIC, PLAN,
                               body["district"] if isinstance(body["district"], str) else "district-1")
    # the centre's own command envelope for a unit is not a peer list even though it is signed by the same key
    env = {"device": "district-1", "seq": 1, "t": NOW, "body": {"update": {}}}
    with pytest.raises(wg.MessageRefused, match="not a wg-peers"):
        wg.parse_peer_list({**env, "sig": crypto.sign(GATEWAY, env)}, GATEWAY_PUBLIC, PLAN, "district-1")


def test_the_peer_list_travels_inside_the_signed_command_and_stays_checkable_afterwards():
    """The hub sends it as the body of an ordinary command (its envelope is checked as always); the district keeps the
    inner message as it came and checks it again, without the clock, when it is applied or read back after a reboot."""
    inner = wire(wg.sign_message(GATEWAY, listing(version=3).body()))
    env = {"device": "district-1", "seq": 7, "t": NOW, "body": {"wg": inner}}
    cmd = wire({**env, "sig": crypto.sign(GATEWAY, env)})
    assert envelope_refusal(cmd, "district-1", GATEWAY_PUBLIC, 6, lambda: NOW + 2) is None
    assert envelope_refusal(cmd, "district-1", GATEWAY_PUBLIC, 6, lambda: NOW + 3600) == "stale command"
    assert wg.parse_peer_list(cmd["body"]["wg"], GATEWAY_PUBLIC, PLAN, "district-1", 2).version == 3
    assert wg.parse_peer_list(cmd["body"]["wg"], GATEWAY_PUBLIC, PLAN, "district-1", 2).version == 3  # a day later: no clock in it


# ---- the link of a unit ----------------------------------------------------------------------------------------

def link(version=1, unit="edge-J-01", address="10.77.1.2", endpoint="203.0.113.7", **kw):
    return wg.make_link(PLAN, unit, "district-1", 1, version, NOW, address, kw.get("server_public") or key(), endpoint)


def test_a_signed_link_is_obeyed_by_its_unit_and_by_no_other():
    lk = link(version=2)
    msg = wire(wg.sign_message(GATEWAY, lk.body()))
    assert wg.parse_link(msg, GATEWAY_PUBLIC, PLAN, "edge-J-01", last_version=1) == lk
    assert lk.endpoint == "203.0.113.7:51820"  # the port defaults to WireGuard's
    with pytest.raises(wg.MessageRefused, match="addressed to edge-J-01, this is edge-J-02"):
        wg.parse_link(msg, GATEWAY_PUBLIC, PLAN, "edge-J-02")
    with pytest.raises(wg.MessageRefused, match="older than the 3"):
        wg.parse_link(msg, GATEWAY_PUBLIC, PLAN, "edge-J-01", last_version=3)
    with pytest.raises(wg.MessageRefused, match="bad signature"):
        wg.parse_link(wire(wg.sign_message(crypto.new_key(), lk.body())), GATEWAY_PUBLIC, PLAN, "edge-J-01")
    forged = copy.deepcopy(msg)
    forged["endpoint"] = "198.51.100.9:51820"  # a hijacked path pointing the unit at its own "district"
    with pytest.raises(wg.MessageRefused, match="bad signature"):
        wg.parse_link(forged, GATEWAY_PUBLIC, PLAN, "edge-J-01")
    with pytest.raises(wg.MessageRefused, match="not a wg-link"):
        wg.parse_link(wire(wg.sign_message(GATEWAY, listing().body())), GATEWAY_PUBLIC, PLAN, "edge-J-01")


@pytest.mark.parametrize("endpoint,ok", [
    ("203.0.113.7", "203.0.113.7:51820"), ("203.0.113.7:4000", "203.0.113.7:4000"), ("district-1.city", "district-1.city:51820"),
    ("[fd00::2]:51820", "[fd00::2]:51820"), ("[fd00::2]", "[fd00::2]:51820"),
    ("10.77.1.1", None), ("10.77.9.9:51820", None), ("", None), ("a b", None), ("host:99999", None),
])
def test_the_endpoint_is_where_the_district_really_is(endpoint, ok):
    if ok is None:
        with pytest.raises(wg.WireGuardError):
            wg.check_endpoint(PLAN, endpoint)
    else:
        assert wg.check_endpoint(PLAN, endpoint) == ok


def test_a_link_with_the_wrong_address_or_key_is_not_made():
    with pytest.raises(wg.AddressError):
        link(address="10.77.1.1")           # the district's own address
    with pytest.raises(wg.AddressError):
        link(address="10.77.2.2")           # another district's network
    with pytest.raises(wg.WireGuardError):
        link(server_public="short")
    with pytest.raises(wg.WireGuardError):
        link(unit="district-1")


# ---- what is handed to wg and ip -------------------------------------------------------------------------------

def test_the_district_config_lets_each_unit_use_its_own_address_only():
    lst = listing(units=("edge-J-01", "edge-J-02"))
    text = wg.district_conf(PLAN, lst)
    assert "PrivateKey" not in text and "ListenPort = 51820" in text
    for p in lst.peers:
        assert f"# {p.unit}\n[Peer]\nPublicKey = {p.public}\nAllowedIPs = {p.ip}/32\n" in text
    assert text.count("[Peer]") == 2 and "0.0.0.0/0" not in text and "/24" not in text
    assert "PrivateKey = " + (k := wg.new_private()) in wg.district_conf(PLAN, lst, k)
    assert "ListenPort = 4000" in wg.district_conf(PLAN, lst, port=4000)
    assert wg.district_conf(PLAN, wg.make_peer_list(PLAN, "district-1", 1, 1, NOW, [])).count("[Peer]") == 0
    with pytest.raises(wg.WireGuardError):
        wg.district_conf(PLAN, lst, "not a key")


def test_the_unit_config_reaches_the_district_and_nothing_else():
    lk = link()
    text = wg.unit_conf(PLAN, lk)
    assert f"PublicKey = {lk.server_public}" in text and "Endpoint = 203.0.113.7:51820" in text
    assert "AllowedIPs = 10.77.1.1/32\n" in text and "PersistentKeepalive = 25" in text
    assert text.count("AllowedIPs") == 1 and "0.0.0.0/0" not in text and "10.77.1.0/24" not in text
    assert "PrivateKey" not in text and "PrivateKey = " + (k := wg.new_private()) in wg.unit_conf(PLAN, lk, k)


def test_a_new_peer_list_becomes_the_smallest_set_of_wg_set_commands():
    a, b, c = peers("edge-J-01", "edge-J-02", "edge-J-03")
    assert wg.set_commands([], [a, b]) == [["wg", "set", "wg0", "peer", k, "allowed-ips", p.ip + "/32"]
                                           for k, p in sorted({a.public: a, b.public: b}.items())]
    assert wg.set_commands([a, b], [a, b]) == []                                  # nothing changed: nothing run
    assert wg.set_commands([a, b], [a]) == [["wg", "set", "wg0", "peer", b.public, "remove"]]  # a revoked unit
    moved = wg.Peer(a.unit, a.public, "10.77.1.50")
    assert wg.set_commands([a, b], [moved, b]) == [["wg", "set", "wg0", "peer", a.public, "allowed-ips", "10.77.1.50/32"]]
    swapped = wg.Peer(a.unit, key(), a.ip)                                        # a unit with a new key: old out first
    cmds = wg.set_commands([a, b], [swapped, b])
    assert cmds[0] == ["wg", "set", "wg0", "peer", a.public, "remove"] and cmds[1][4] == swapped.public and len(cmds) == 2
    assert wg.set_commands([a], [c], iface="wg7")[0][2] == "wg7"


def test_the_interface_commands_give_the_district_a_network_and_a_unit_one_route():
    up = wg.district_up_commands(PLAN, 3, "/var/lib/urban/etc/pki/wg/district-1.key")
    assert ["ip", "address", "add", "10.77.3.1/24", "dev", "wg0"] in up
    assert ["wg", "set", "wg0", "private-key", "/var/lib/urban/etc/pki/wg/district-1.key", "listen-port", "51820"] in up
    assert up[0] == ["ip", "link", "add", "dev", "wg0", "type", "wireguard"] and up[-1][-1] == "up"
    unit = wg.unit_up_commands(PLAN, link(), "/k")
    assert ["ip", "address", "add", "10.77.1.2/32", "dev", "wg0"] in unit
    assert [c for c in unit if c[:3] == ["ip", "route", "add"]] == [["ip", "route", "add", "10.77.1.1/32", "dev", "wg0"]]
    assert all("--" not in " ".join(c) and ";" not in " ".join(c) for c in up + unit)  # argument lists, not shell text


def test_handshake_ages_are_read_from_wg_show():
    a, b, c = key(), key(), key()
    text = f"{a}\t{NOW - 30}\n{b}\t0\n{c}\t{NOW - 600}\nnot a key\t5\n\n{a[:-1]}\tx\n"
    seen = wg.parse_latest_handshakes(text)
    assert seen == {a: NOW - 30, b: 0, c: NOW - 600}
    assert wg.peers_up(seen, NOW) == {a}                          # b never, c too long ago
    assert wg.peers_up(seen, NOW, within=900) == {a, c}
    assert wg.peers_up({a: NOW + 5000}, NOW) == set()             # a handshake from the future proves nothing
    assert wg.parse_latest_handshakes("") == {}


# ---- cameras behind a unit -------------------------------------------------------------------------------------

def test_each_camera_gets_a_tunnel_port_that_does_not_move():
    cams = [{"name": "cam-1", "host": "192.168.1.11"}, {"name": "cam-2", "host": "192.168.1.12", "port": 8554},
            {"name": "cam-3", "host": "192.168.1.13", "tunnel_port": 8551}]
    got = wg.plan_camera_forwards(cams)
    assert [(c.name, c.tunnel_port, c.port) for c in got] == [("cam-3", 8551, 554), ("cam-1", 8552, 554), ("cam-2", 8553, 8554)]
    assert got[1].url("10.77.1.2", "stream1") == "rtsp://10.77.1.2:8552/stream1"
    assert got[1].url("10.77.1.2") == "rtsp://10.77.1.2:8552/"
    pinned = [{"name": c.name, "host": c.host, "port": c.port, "tunnel_port": c.tunnel_port} for c in got]
    pinned = [pinned[1], pinned[0], {"name": "cam-4", "host": "192.168.1.14"}, pinned[2]]  # another order, one added
    again = wg.plan_camera_forwards(pinned)
    assert {c.name: c.tunnel_port for c in again if c.name != "cam-4"} == {c.name: c.tunnel_port for c in got}  # added one, none moved
    assert [c for c in again if c.name == "cam-4"][0].tunnel_port == 8554
    assert wg.plan_camera_forwards([]) == []


@pytest.mark.parametrize("cams", [
    [{"name": "a", "host": "192.168.1.1"}, {"name": "a", "host": "192.168.1.2"}],                             # same name
    [{"name": "a", "host": "192.168.1.1", "tunnel_port": 8551}, {"name": "b", "host": "192.168.1.2", "tunnel_port": 8551}],
    [{"name": "a", "host": "192.168.1.1", "tunnel_port": 80}],                                                # outside the range
    [{"name": "a", "host": "192.168.1.1", "port": 0}], [{"name": "a", "host": "192.168.1.1", "port": True}],
    [{"name": "a", "host": "127.0.0.1"}], [{"name": "a", "host": "10.77.1.1"}],                               # not the cabinet's network
    [{"name": "a", "host": "camera.local"}], [{"name": "a"}], [{"host": "192.168.1.1"}], [{"name": "a b", "host": "192.168.1.1"}],
    [{"name": f"c{i}", "host": f"192.168.1.{i + 1}"} for i in range(wg.CAMERA_PORT_LAST - wg.CAMERA_PORT_FIRST + 2)],  # too many
])
def test_cameras_that_cannot_be_forwarded_are_refused(cams):
    with pytest.raises(wg.WireGuardError):
        wg.plan_camera_forwards(cams)


# ---- the command line the root helpers use ---------------------------------------------------------------------

def test_the_command_line_checks_the_message_before_it_prints_a_config(tmp_path, capsys):
    gw = tmp_path / "gateway.pub"
    gw.write_text(crypto.public_hex(GATEWAY))
    keyfile = tmp_path / "district.key"
    assert wg.main(["keygen", str(keyfile)]) == 0
    public = capsys.readouterr().out.strip()
    assert wg.main(["pubkey", str(keyfile)]) == 0 and capsys.readouterr().out.strip() == public
    assert wg.main(["keygen", str(keyfile)]) == 1 and "exists" in capsys.readouterr().err.lower()  # never overwritten

    lst = listing(version=4)
    msg = tmp_path / "peers.json"
    msg.write_text(json.dumps(wg.sign_message(GATEWAY, lst.body())))
    args = ["district-conf", "--message", str(msg), "--gateway-pub", str(gw), "--district", "district-1", "--key", str(keyfile)]
    assert wg.main(args) == 0
    out = capsys.readouterr().out
    assert out.count("[Peer]") == 2 and f"PrivateKey = {wg.read_private(keyfile)}" in out
    assert wg.main([*args, "--last-version", "5"]) == 1 and "older" in capsys.readouterr().err
    assert wg.main([*args[:-4], "--district", "district-2", "--key", str(keyfile)]) == 1
    assert "addressed to" in capsys.readouterr().err
    msg.write_text(json.dumps({**wg.sign_message(GATEWAY, lst.body()), "plain_relay": False}))
    assert wg.main(args) == 1 and "bad signature" in capsys.readouterr().err

    lk = link()
    msg.write_text(json.dumps(wg.sign_message(GATEWAY, lk.body())))
    assert wg.main(["unit-conf", "--message", str(msg), "--gateway-pub", str(gw), "--unit", "edge-J-01", "--key", str(keyfile)]) == 0
    assert "PersistentKeepalive = 25" in capsys.readouterr().out
    assert wg.main(["unit-conf", "--message", str(tmp_path / "missing.json"), "--gateway-pub", str(gw),
                    "--unit", "edge-J-01", "--key", str(keyfile)]) == 1
    assert "cannot read" in capsys.readouterr().err
