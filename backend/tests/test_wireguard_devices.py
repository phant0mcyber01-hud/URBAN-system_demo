"""The tunnel on the device side (edge/tunnel.py): a junction unit and a district take the centre's signed link or peer
list out of a verified {"wg": ...} command and put it in the inbox for the root helper only if it verifies and is not
older than the one applied (a message under a newer gateway key is never held back by an older version); every refusal
is reported; every other command is handled as before; the health carries the helper's status; a unit dials the
district's tunnel address once the helper says the link is proven, waits while it cannot tell, and moves its session
there without restarting; the district's vision reads RTSP over TCP only."""
import argparse
import asyncio
import json
import os
import re
import ssl
import sys
import threading
import time
import types

import pytest

import urban.edge.__main__ as edge_main
from urban.devices import crypto, trust
from urban.devices.registry import provision
from urban.edge.device import EdgeDevice
from urban.edge.tunnel import MAX_MESSAGE, Tunnel
from urban.edge.update import Updater, make_updater
from urban.equipment.adapter import ControllerAdapter
from urban.net import wireguard as wg
from urban.node.district import DistrictDevice
from urban.vision.source import VideoSource

PLAN = wg.Plan.of()
GATEWAY = crypto.new_key()
GATEWAY_PUBLIC = GATEWAY.public_key()
WALL = 1_800_000_000
UNIT, DISTRICT = "edge-J-01", "district-1"
UNIT_TUNNEL_KEY, SERVER_KEY = wg.new_keypair()[1], wg.new_keypair()[1]
PLATFORM = "192.168.77.1:8443"  # the kit's: the district's relay on the link between them


def link(version=5, unit=UNIT, key=GATEWAY, address="10.77.1.2", **extra):
    body = wg.make_link(PLAN, unit, DISTRICT, 1, version, WALL, address, SERVER_KEY, "192.168.77.1:51820").body()
    return wire(wg.sign_message(key, {**body, **extra}))


def peer_list(version=5, district=DISTRICT, key=GATEWAY, plain=True):
    body = wg.make_peer_list(PLAN, district, 1, version, WALL, [wg.Peer(UNIT, UNIT_TUNNEL_KEY, "10.77.1.2")], plain).body()
    return wire(wg.sign_message(key, body))


def wire(msg):
    """What a message looks like after the trip over the link."""
    return json.loads(json.dumps(msg))


class Centre:
    """The centre's command envelope, as urban/devices/hub.py makes it: gateway-signed, a rising number, the time."""

    def __init__(self, key=GATEWAY, wall=WALL):
        self.key, self.wall, self.seq = key, wall, 1000

    def cmd(self, device, body):
        self.seq += 1
        env = {"device": device, "seq": self.seq, "t": self.wall, "body": body}
        return {"type": "cmd", **env, "sig": crypto.sign(self.key, env)}


class Controller(ControllerAdapter):
    def __init__(self):
        self.calls = []

    def plans(self, tls):
        return {0, 1, 2}

    def select(self, tls, plan):
        self.calls.append(("select", tls, plan))

    def base(self, tls):
        self.calls.append(("base", tls))

    def keepalive(self, tls, plan):
        self.calls.append(("keepalive", tls, plan))


@pytest.fixture
def inbox(tmp_path):
    path = tmp_path / "wg-inbox"
    path.mkdir()
    return path


@pytest.fixture
def status(tmp_path):
    return tmp_path / "wg.json"


def helper_says(path, **fields):
    """The root helper's status file (urban-wg writes it, contract of /run/urban/wg.json)."""
    path.write_text(json.dumps({"role": "unit", "public": UNIT_TUNNEL_KEY, "state": "none", "applied": 0, "epoch": 0,
                                "district": None, "index": None, "address": None, "server": None, "proven": False,
                                "handshake": 0, "peers": 0, "peers_up": 0, "mode": "off", "reason": "", "t": WALL,
                                **fields}))


def queued(inbox):
    return sorted(p.name for p in inbox.iterdir())


def unit(tmp_path, inbox, status, adapter=None, **kw):
    return EdgeDevice(UNIT, "T1", crypto.new_key(), kw.pop("gateway_public", GATEWAY_PUBLIC), adapter or Controller(),
                      state_path=tmp_path / "unit.seq", wall=lambda: WALL, tunnel=Tunnel("unit", UNIT, inbox, status), **kw)


def district(tmp_path, inbox, status, **kw):
    return DistrictDevice(DISTRICT, crypto.new_key(), gateway_public=GATEWAY_PUBLIC, state_path=tmp_path / "district.seq",
                          wall=lambda: WALL, tunnel=Tunnel("district", DISTRICT, inbox, status), **kw)


def last_reason(dev):
    return dev.report({})["events"][-1]["reason"]


# ---------------------------------------------------------------- what a unit queues

def test_a_unit_queues_a_verified_link_for_the_root_helper_exactly_as_it_came(tmp_path, inbox, status):
    ctl = Controller()
    dev = unit(tmp_path, inbox, status, ctl)
    dev.tick(100)
    centre = Centre()
    msg = link()
    env = centre.cmd(UNIT, {"wg": msg})  # no "tls" in it: it is about the unit, not its junction
    assert dev.on_command(env) is True
    assert queued(inbox) == ["message.json"]  # no .tmp left behind
    assert (inbox / "message.json").read_text(encoding="utf-8") == json.dumps(msg)  # the inner message, no wrapper
    assert wg.parse_link(json.loads((inbox / "message.json").read_text()), GATEWAY_PUBLIC, PLAN, UNIT).version == 5
    assert dev.last_seq == env["seq"] and (tmp_path / "unit.seq").read_text() == str(env["seq"])
    assert dev.report({})["events"] == [] and ctl.calls == [] and dev.controller.leases == {}  # never the controller's
    # a newer one replaces it, and so does the same version again (it is the same link)
    for version in (6, 6):
        assert dev.on_command(centre.cmd(UNIT, {"wg": link(version)})) is True
        assert json.loads((inbox / "message.json").read_text())["version"] == version
    assert queued(inbox) == ["message.json"]


def test_a_unit_refuses_a_link_that_does_not_verify_or_is_older_than_the_applied_one_and_says_why(tmp_path, inbox, status):
    dev = unit(tmp_path, inbox, status)
    helper_says(status, state="up", applied=5, epoch=0)
    centre = Centre()
    stranger = crypto.new_key()
    tampered = link(7)
    tampered["address"] = "10.77.1.3"
    cases = [(link(7, key=stranger), "wg: bad signature"),
             (tampered, "wg: bad signature"),
             (link(7, unit="edge-J-02"), "wg: not addressed here"),
             (link(4), "wg: older version"),
             (link(7, extra=1), "wg: malformed"),       # signed, but a field the link does not have
             (peer_list(7), "wg: malformed"),           # a district's message
             ("junk", "wg: malformed"),
             ({**link(7), "pad": "x" * MAX_MESSAGE}, "wg: message too large")]
    for msg, reason in cases:
        assert dev.on_command(centre.cmd(UNIT, {"wg": msg})) is False, reason
        assert last_reason(dev) == reason and len(reason) <= 120
        assert queued(inbox) == [], reason
    assert dev.last_seq == centre.seq  # the envelopes were fine: only the inner message was refused
    # without a gateway key yet nothing is queued either
    keyless = unit(tmp_path, inbox, status, gateway_public=None)
    assert keyless.tunnel.take(link(7), None, 0) == "wg: no gateway key yet" and queued(inbox) == []
    # the same version as the applied one is queued (applying it again changes nothing), a newer one too
    assert dev.on_command(centre.cmd(UNIT, {"wg": link(5)})) is True
    assert dev.on_command(centre.cmd(UNIT, {"wg": link(9)})) is True
    assert json.loads((inbox / "message.json").read_text())["version"] == 9 and dev.report({})["events"] == []


def test_a_message_under_a_newer_gateway_key_is_not_held_back_by_the_old_version(tmp_path, inbox, status):
    """The service passes its own gateway key epoch: the applied version only counts against a message under the same
    key's epoch or an older one. After a rotation the genuine message replaces anything signed under the old key."""
    root = crypto.new_key()
    cert = tmp_path / "edge-J-01.gateway.json"
    cert.write_text(json.dumps(trust.issue(root, crypto.public_hex(GATEWAY), 2)))
    dev = unit(tmp_path, inbox, status, gateway_public=None, root_public=root.public_key(), gateway_state=cert)
    assert dev.trust.epoch == 2 and dev.gateway_public is not None
    centre = Centre()
    for helper_epoch, version, took in ((1, 50, True),     # the helper's message is under epoch 1: the old number is moot
                                        (2, 60, False),    # the same epoch: 60 is older than 9000
                                        (2, 9000, True),   # equal
                                        (3, 8000, False)):  # the helper is ahead of this service: its number holds
        (inbox / "message.json").unlink(missing_ok=True)
        helper_says(status, applied=9000, epoch=helper_epoch, state="up")
        assert dev.on_command(centre.cmd(UNIT, {"wg": link(version)})) is took, (helper_epoch, version)
        assert queued(inbox) == (["message.json"] if took else []), (helper_epoch, version)
        if not took:
            assert last_reason(dev) == "wg: older version"
    # no status, or one that does not read, counts as nothing applied under epoch 0
    for junk in (None, "{", "[]", json.dumps({"applied": "9000", "epoch": 9}), json.dumps({"applied": -1, "epoch": True})):
        status.unlink(missing_ok=True)
        if junk is not None:
            status.write_text(junk)
        (inbox / "message.json").unlink(missing_ok=True)
        assert dev.tunnel.take(link(1), dev.gateway_public, dev.trust.epoch) is None, junk
        assert queued(inbox) == ["message.json"]
    # Tunnel.take on its own: the epoch is the caller's
    helper_says(status, applied=100, epoch=1)
    tunnel = Tunnel("unit", UNIT, inbox, status)
    assert tunnel.take(link(99), GATEWAY_PUBLIC, 1) == "wg: older version"
    assert tunnel.take(link(99), GATEWAY_PUBLIC, 0) == "wg: older version"
    assert tunnel.take(link(99), GATEWAY_PUBLIC, 2) is None


def test_a_wg_body_is_still_a_command_under_the_gateway_key(tmp_path, inbox, status):
    dev = unit(tmp_path, inbox, status)
    centre = Centre()
    env = centre.cmd(UNIT, {"wg": link()})
    assert dev.on_command(env) is True
    (inbox / "message.json").unlink()
    assert dev.on_command(env) is False and last_reason(dev) == "replayed or out of order"
    assert dev.on_command(Centre(key=crypto.new_key()).cmd(UNIT, {"wg": link(6)})) is False
    assert last_reason(dev) == "bad signature"
    assert dev.on_command(centre.cmd("edge-J-02", {"wg": link(6)})) is False
    assert last_reason(dev) == "addressed to another device"
    stale = Centre(wall=WALL - 600)
    stale.seq = centre.seq + 10
    assert dev.on_command(stale.cmd(UNIT, {"wg": link(6)})) is False and last_reason(dev) == "stale command"
    assert queued(inbox) == []


def test_every_other_command_reaches_a_unit_as_before(tmp_path, inbox, status):
    """The same envelopes to a unit with the tunnel and to one without: the same answers, the same controller calls,
    the same reports, and nothing in the inbox. Without a tunnel a wg body is what it was, not for this junction."""
    centre = Centre()
    lease = {"op": "lease", "id": "c-1", "tls": "T1", "plan": 2, "ttl": 60}
    bodies = [lease, {"op": "lease", "id": "c-2", "tls": "T9", "plan": 1, "ttl": 60},
              {"op": "noop", "tls": "T1", "wg": link()},     # a wg field among others: an ordinary command
              {"update": {"version": 3}},                     # an update offer: no updater here, taken and ignored
              {"op": "release", "id": "c-1", "tls": "T1"}]
    envs = [centre.cmd(UNIT, body) for body in bodies]
    with_tunnel = unit(tmp_path, inbox, status)
    without = EdgeDevice(UNIT, "T1", crypto.new_key(), GATEWAY_PUBLIC, Controller(), wall=lambda: WALL)
    for dev in (with_tunnel, without):
        dev.tick(100)
    answers = [[dev.on_command(env) for env in envs] for dev in (with_tunnel, without)]
    assert answers[0] == answers[1] == [True, False, True, True, True]
    assert with_tunnel.adapter.calls == without.adapter.calls == [("select", "T1", 2), ("base", "T1")]
    events = [dev.report({})["events"] for dev in (with_tunnel, without)]
    assert events[0] == events[1] and [e.get("reason") for e in events[0] if e.get("status") == "refused"] == ["not my junction"]
    assert queued(inbox) == []
    plain = centre.cmd(UNIT, {"wg": link()})
    assert without.on_command(plain) is False and last_reason(without) == "not my junction"


# ---------------------------------------------------------------- what a district queues

def test_a_district_queues_only_a_verified_peer_list_not_older_than_the_applied_one(tmp_path, inbox, status):
    dev = district(tmp_path, inbox, status)
    centre = Centre()
    env = centre.cmd(DISTRICT, {"wg": peer_list(5)})
    dev.on_command(env)
    assert queued(inbox) == ["message.json"] and (inbox / "message.json").read_text() == json.dumps(peer_list(5))
    assert (tmp_path / "district.seq").read_text() == str(env["seq"]) and dev.make_report({}, 1)["events"] == []
    (inbox / "message.json").unlink()
    helper_says(status, role="district", applied=5, epoch=0, state="up", mode="allow")
    # signed, but a peer whose name is no unit id and reads like another refusal: the words never come from the message
    odd = peer_list(6)
    odd["peers"][0]["unit"] = "edge-J-01 is older than 1"
    odd = wire(wg.sign_message(GATEWAY, {k: v for k, v in odd.items() if k != "sig"}))
    for msg in (peer_list(4), peer_list(6, district="district-2"), peer_list(6, key=crypto.new_key()), link(6), [1, 2], odd):
        dev.on_command(centre.cmd(DISTRICT, {"wg": msg}))
    reasons = [e["reason"] for e in dev.make_report({}, 2)["events"]]
    assert reasons == ["wg: older version", "wg: not addressed here", "wg: bad signature", "wg: malformed", "wg: malformed",
                       "wg: malformed"]
    assert queued(inbox) == []
    dev.on_command(centre.cmd(DISTRICT, {"wg": peer_list(5)}))  # equal: queued
    dev.on_command(centre.cmd(DISTRICT, {"wg": peer_list(6, plain=False)}))  # mode only
    assert dev.make_report({}, 3)["events"] == []
    assert json.loads((inbox / "message.json").read_text()) == peer_list(6, plain=False)
    # a newer gateway key epoch at the service than the applied list's: the old number does not hold it back
    dev.trust.epoch = 1
    helper_says(status, role="district", applied=9000, epoch=0)
    dev.on_command(centre.cmd(DISTRICT, {"wg": peer_list(7)}))
    assert dev.make_report({}, 4)["events"] == [] and json.loads((inbox / "message.json").read_text())["version"] == 7


def test_a_district_still_takes_no_other_command(tmp_path, inbox, status):
    class Offers:
        def __init__(self):
            self.taken = []

        def offer(self, spec):
            self.taken.append(spec)

        def health(self):
            return {"os": 2}

    centre = Centre()
    offers = Offers()
    dev = district(tmp_path, inbox, status, updater=offers)
    plain = DistrictDevice(DISTRICT, crypto.new_key(), gateway_public=GATEWAY_PUBLIC, wall=lambda: WALL)  # no tunnel
    for body in ({"op": "lease", "tls": "T1"}, {"wg": peer_list(), "x": 1}, {"wg": peer_list(), "tls": "T1"}, "wg"):
        dev.on_command(centre.cmd(DISTRICT, body))
    dev.on_command({"type": "cmd", "body": {"wg": peer_list()}})  # an envelope with nothing signed: refused as before
    plain.on_command(centre.cmd(DISTRICT, {"wg": peer_list()}))
    reasons = [e["reason"] for e in dev.make_report({}, 1)["events"]]
    assert reasons == ["a district server takes no commands"] * 4 + ["malformed command"]
    assert [e["reason"] for e in plain.make_report({}, 1)["events"]] == ["a district server takes no commands"]
    dev.on_command(centre.cmd(DISTRICT, {"update": {"version": 3}}))  # an update offer goes where it went
    assert offers.taken == [{"version": 3}] and queued(inbox) == []


def test_no_inbox_and_a_failed_write_are_refused_and_reported(tmp_path, status):
    dev = unit(tmp_path, tmp_path / "absent", status)
    assert dev.on_command(Centre().cmd(UNIT, {"wg": link()})) is False and last_reason(dev) == "wg: no inbox"
    inbox = tmp_path / "wg-inbox"
    (inbox / "message.json.tmp").mkdir(parents=True)  # something that cannot be written in its place
    why = Tunnel("unit", UNIT, inbox, status).take(link(), GATEWAY_PUBLIC)
    assert why.startswith("wg: write error") and len(why) <= 120 and not (inbox / "message.json").exists()


def test_the_inbox_and_the_status_are_the_os_paths_unless_overridden(tmp_path, monkeypatch):
    tunnel = Tunnel("unit", UNIT)
    monkeypatch.setenv("URBAN_WG_INBOX", str(tmp_path / "in"))
    monkeypatch.setenv("URBAN_WG_STATUS", str(tmp_path / "s.json"))
    (tmp_path / "in").mkdir()
    helper_says(tmp_path / "s.json", state="up", proven=True, server="10.77.1.1")
    assert tunnel.take(link(), GATEWAY_PUBLIC) is None and queued(tmp_path / "in") == ["message.json"]
    assert tunnel.health()["wg"]["state"] == "up" and tunnel.platform(PLATFORM) == "10.77.1.1:8443"
    with pytest.raises(ValueError):
        Tunnel("center", "center")


# ---------------------------------------------------------------- what is reported

def test_the_health_carries_the_helpers_status_and_nothing_more(tmp_path, inbox, status):
    dev = unit(tmp_path, inbox, status, updater=Updater(lambda: None, version=2, inbox=tmp_path / "u", status=tmp_path / "u.json"))
    report = dev.report({})
    assert "wg" not in report["health"] and report["health"]["os"] == 2  # no status: a machine without the helper
    helper_says(status, state="up", applied=1792000000123, epoch=1, district=DISTRICT, index=1, address="10.77.1.2",
                server="10.77.1.1", proven=True, handshake=1792000123, peers=1, peers_up=1)
    report = dev.report({})
    assert set(report) == {"type", "t", "queues", "events", "health", "epoch"}
    assert report["health"]["wg"] == {"public": UNIT_TUNNEL_KEY, "state": "up", "applied": 1792000000123, "epoch": 1,
                                      "address": "10.77.1.2", "reason": "", "proven": True, "handshake": 1792000123}
    assert list(report["health"])[-2:] == ["os", "wg"]  # after the update fields
    assert dev.updater.health() == {"os": 2}  # the update client's own fields stay its own
    for junk in ("{", "[]", "", "null"):
        status.write_text(junk)
        assert "wg" not in dev.report({})["health"], junk
    helper_says(status, state="clock")  # read anew on every report
    assert dev.report({})["health"]["wg"]["state"] == "clock"
    # a district reports its own fields
    dist = district(tmp_path, inbox, status)
    helper_says(status, role="district", state="up", applied=7, epoch=1, address="10.77.1.1", server="10.77.1.1",
                peers=3, peers_up=2, mode="only")
    assert dist.health()["wg"] == {"public": UNIT_TUNNEL_KEY, "state": "up", "applied": 7, "epoch": 1,
                                   "address": "10.77.1.1", "reason": "", "peers": 3, "peers_up": 2, "mode": "only"}
    assert set(dist.make_report({}, 1)) == {"type", "t", "queues", "events", "epoch", "health"}
    status.unlink()
    assert "wg" not in dist.health()
    # the demo and the tests without a tunnel: as before
    assert "wg" not in DistrictDevice(DISTRICT, crypto.new_key(), gateway_public=GATEWAY_PUBLIC).health()


# ---------------------------------------------------------------- where a unit dials the centre

def test_a_unit_dials_the_tunnel_only_once_the_helper_says_the_link_is_proven(tmp_path, inbox, status):
    tunnel = Tunnel("unit", UNIT, inbox, status)
    assert Tunnel("unit", UNIT, tmp_path / "absent", status).platform(PLATFORM) == PLATFORM  # no helper: as ever
    helper_says(status, proven=True, server="10.77.1.1")
    assert Tunnel("unit", UNIT, tmp_path / "absent", status).platform(PLATFORM) == PLATFORM
    status.unlink()
    assert tunnel.platform(PLATFORM) is None  # the helper exists but has not said: wait
    for junk in ("{", "[]", "", "null"):
        status.write_text(junk)
        assert tunnel.platform(PLATFORM) is None, junk
    for fields, want in (({"proven": True, "server": "10.77.1.1", "state": "up"}, "10.77.1.1:8443"),
                         ({"proven": True, "server": "10.77.9.1", "state": "down"}, "10.77.9.1:8443"),  # down: still the tunnel
                         ({"proven": True, "server": "10.77.1.1", "state": "clock"}, "10.77.1.1:8443"),
                         ({"proven": False, "server": "10.77.1.1", "state": "up"}, PLATFORM),
                         ({"proven": "true", "server": "10.77.1.1"}, PLATFORM),
                         ({"proven": 1, "server": "10.77.1.1"}, PLATFORM),
                         ({"proven": True, "server": "192.168.77.1"}, PLATFORM),   # not a tunnel address
                         ({"proven": True, "server": "10.78.1.1"}, PLATFORM),
                         ({"proven": True, "server": "10.77.1.1:8443"}, PLATFORM),
                         ({"proven": True, "server": " 10.77.1.1"}, PLATFORM),
                         ({"proven": True, "server": None}, PLATFORM),
                         ({"proven": True, "server": 10}, PLATFORM),
                         ({"state": "none"}, PLATFORM)):
        helper_says(status, **fields)
        assert tunnel.platform(PLATFORM) == want, fields
    helper_says(status, role="district", proven=True, server="10.77.1.1")
    assert Tunnel("district", DISTRICT, inbox, status).platform(PLATFORM) == PLATFORM  # a district dials its centre


def test_an_update_is_downloaded_over_the_path_of_the_moment(tmp_path):
    where = [PLATFORM]
    context = ssl.create_default_context()
    up = make_updater(lambda: where[-1], "urban-center", context)
    conn = up.connect()
    assert (conn.host, conn.port, conn.server_name) == ("192.168.77.1", 8443, "urban-center")
    where.append("10.77.1.1:8443")
    conn = up.connect()
    assert (conn.host, conn.port, conn.server_name) == ("10.77.1.1", 8443, "urban-center")  # the centre's name still
    where.append(None)  # no path known: that download is not tried
    up.version, up.inbox, up.status = 2, tmp_path / "inbox", tmp_path / "update.json"
    spec = {"version": 3, "sha256": "ab" * 32, "size": 10, "path": "/update/" + "a1" * 32}
    assert up.offer(spec)
    up.drain()
    assert up.state["state"] == "failed" and "tunnel status" in up.state["reason"] and len(up.state["reason"]) <= 120
    assert not (tmp_path / "inbox" / "urban-3.raucb").exists()
    assert up.offer(spec) is True  # taken again on the centre's next offer
    fixed = make_updater("127.0.0.1:8443", "localhost", context).connect()  # a plain host:port as before
    assert (fixed.host, fixed.port, fixed.server_name) == ("127.0.0.1", 8443, "localhost")


class Socket:
    """The centre's side of one websocket session: its challenge and welcome, then whatever the unit sends."""

    def __init__(self):
        self.frames = [json.dumps({"nonce": "n-1"}), json.dumps({"type": "welcome", "tls": "T1", "plans": [1, 2]})]
        self.sent = []
        self.closed = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed.set()

    async def recv(self):
        return self.frames.pop(0)

    async def send(self, text):
        self.sent.append(json.loads(text))

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.closed.wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed.set()


class Dialler:
    """websockets.connect as the unit sees it: records where it dials and with which TLS name."""

    def __init__(self):
        self.dialled, self.sockets = [], []

    def __call__(self, url, ssl=None, server_hostname=None):
        self.dialled.append((url, server_hostname))
        self.sockets.append(Socket())
        return self.sockets[-1]


@pytest.fixture
def real_unit(tmp_path, monkeypatch):
    """A Unit as urban-edge@ starts it, with the tunnel paths of a test and a websocket that goes nowhere."""
    provision([{"id": "J-01", "tls": "T1", "name": "First"}], tmp_path / "pki")
    monkeypatch.setenv("URBAN_WG_INBOX", str(tmp_path / "wg-inbox"))
    monkeypatch.setenv("URBAN_WG_STATUS", str(tmp_path / "wg.json"))
    monkeypatch.setattr(edge_main, "RETRY", 0.01)
    dialler = Dialler()
    monkeypatch.setattr(edge_main.websockets, "connect", dialler)
    args = argparse.Namespace(pki=str(tmp_path / "pki"), unit=UNIT, key=None, gateway_pub=None, root=None, ca=None,
                              adapter="none", controller=None, temp=None, door=None, door_open="1", junction="T1",
                              state=str(tmp_path / "unit.seq"), sensors=None, platform=PLATFORM, server_name="urban-center")
    return edge_main.Unit(args), dialler


async def until(check, timeout=10.0):
    end = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.02)


def test_the_unit_moves_its_session_into_the_tunnel_without_restarting(real_unit, tmp_path, capsys):
    u, dialler = real_unit
    (tmp_path / "wg-inbox").mkdir()
    helper_says(tmp_path / "wg.json", state="down")
    dev = u.dev

    async def go():
        tasks = [asyncio.create_task(u.ticker()), asyncio.create_task(u.session())]
        await until(lambda: u.ws is not None)
        first = dialler.sockets[0]
        await until(lambda: any(m["type"] == "report" for m in first.sent))
        helper_says(tmp_path / "wg.json", state="up", proven=True, server="10.77.1.1", address="10.77.1.2")
        assert u.dev.updater.connect().host == "10.77.1.1"  # downloads take the tunnel at once
        await until(lambda: len(dialler.sockets) == 2 and u.ws is dialler.sockets[1])
        second = dialler.sockets[1]
        await until(lambda: any(m["type"] == "report" for m in second.sent))
        assert first.closed.is_set() and not any(t.done() for t in tasks)  # the same process, still running
        # a new kit clears proven (urban-wg pin --replace): back to the kit's address the same way
        helper_says(tmp_path / "wg.json", state="up", proven=False, server="10.77.1.1", address="10.77.1.2")
        await until(lambda: len(dialler.sockets) == 3 and u.ws is dialler.sockets[2])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return first, second

    first, second = asyncio.run(go())
    assert u.dev is dev
    assert dialler.dialled == [(f"wss://{PLATFORM}/device", "urban-center"), ("wss://10.77.1.1:8443/device", "urban-center"),
                               (f"wss://{PLATFORM}/device", "urban-center")]
    assert second.sent[0]["type"] == "hello" and second.sent[0]["device"] == UNIT
    out = capsys.readouterr().out
    assert out.count("edge: switching to the tunnel: platform 10.77.1.1:8443") == 1
    assert out.count(f"edge: back to the plain path: platform {PLATFORM}") == 1
    online = [line for line in out.splitlines() if " online at " in line]
    assert online == [f"edge: {UNIT} online at T1, approved plans [1, 2], gateway key epoch 0, platform {PLATFORM}",
                      f"edge: {UNIT} online at T1, approved plans [1, 2], gateway key epoch 0, platform 10.77.1.1:8443",
                      f"edge: {UNIT} online at T1, approved plans [1, 2], gateway key epoch 0, platform {PLATFORM}"]
    assert all(re.search(r"edge: \S+ online at", line) for line in online)  # what the QEMU checks wait for
    assert re.search(r"edge: \S+ online at .*platform 10\.77\.1\.1:8443", out)


def test_the_unit_waits_without_dialling_while_the_helper_has_not_said(real_unit, tmp_path, capsys):
    u, dialler = real_unit
    (tmp_path / "wg-inbox").mkdir()  # the helper is on this machine, its status is not written yet

    async def go():
        task = asyncio.create_task(u.session())
        await asyncio.sleep(0.3)  # many retries
        assert dialler.dialled == []
        assert u.dev.updater.connect() is None  # nor is anything downloaded meanwhile
        helper_says(tmp_path / "wg.json", state="none")
        await until(lambda: u.ws is not None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert dialler.dialled == [(f"wss://{PLATFORM}/device", "urban-center")]
    assert capsys.readouterr().out.count("edge: waiting for the tunnel status") == 1


def test_a_unit_without_the_helper_dials_its_kit_as_before(real_unit, tmp_path):
    u, dialler = real_unit  # no wg-inbox: a server install or an OS before the tunnel

    async def go():
        task = asyncio.create_task(u.session())
        await until(lambda: u.ws is not None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert dialler.dialled == [(f"wss://{PLATFORM}/device", "urban-center")]
    assert u.dev.report({})["health"].get("wg") is None and u.dev.updater.connect().host == "192.168.77.1"


# ---------------------------------------------------------------- the district's vision

def test_rtsp_is_read_over_tcp_only(monkeypatch):
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;udp")
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS")
    seen = []
    stop = threading.Event()

    class Capture:
        def __init__(self, url):
            seen.append((url, os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")))
            stop.set()

        def isOpened(self):
            return False

    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(VideoCapture=Capture))
    assert list(VideoSource("missing.avi", realtime=False).frames(stop)) == []
    assert seen == [("missing.avi", None)]  # a file is a file
    stop.clear()
    monkeypatch.setenv("CAM1_PASS", "s3cret")
    assert list(VideoSource("rtsp://admin:${CAM1_PASS}@10.77.1.2:8551/stream1").frames(stop)) == []
    assert seen[-1] == ("rtsp://admin:s3cret@10.77.1.2:8551/stream1", "rtsp_transport;tcp")
