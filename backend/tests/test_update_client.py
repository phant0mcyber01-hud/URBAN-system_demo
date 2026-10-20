"""Over-the-air updates on the device: a unit's and a district's client report their system version, ignore an offer
that is not newer, download an offered package, refuse it (and say why) when its size or hash differs from the signed
offer, and hand a verified package to the root helper through the update inbox only."""
import asyncio
import contextlib
import hashlib
import http.client
import io
import json
import socket
import ssl
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI

from urban.devices import crypto
from urban.devices.health import clean
from urban.devices.hub import DeviceHub
from urban.devices.registry import Registry, provision
from urban.edge import update as upd
from urban.edge.device import EdgeDevice
from urban.edge.update import Updater, make_updater, os_version, valid_offer
from urban.equipment.adapter import NoController
from urban.node.district import DistrictDevice
from urban.release import ReleaseStore

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}]
WALL = 1_800_000_000
DATA = bytes(range(251)) * 700  # a package of some 175 kB


def offer_of(data=DATA, version=3, **change):
    spec = {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "path": "/update/" + "a1" * 32}
    return {**spec, **change}


class Resp:
    def __init__(self, body=b"", status=200, length=len(DATA)):
        """length: what the Content-Length header says (the offered size, unless the centre lies about it)."""
        self.status, self.io, self.length, self.served = status, io.BytesIO(body), length, 0

    def getheader(self, name, default=None):
        return str(self.length) if name == "Content-Length" and self.length is not None else default

    def read(self, n):
        chunk = self.io.read(n)
        self.served += len(chunk)
        return chunk


class Endless(Resp):
    """A lying centre that never stops sending."""

    def __init__(self):
        super().__init__(length=None)

    def read(self, n):
        self.served += n
        return b"x" * n


class Conn:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.requests, self.closed = resp, error, [], False

    def request(self, method, path, headers=None):
        self.requests.append((method, path))
        if self.error:
            raise self.error

    def getresponse(self):
        return self.resp

    def close(self):
        self.closed = True


@pytest.fixture
def inbox(tmp_path):
    return tmp_path / "inbox"


def updater(tmp_path, inbox, conn=None, version=2, **kw):
    return Updater(lambda: conn, version=version, inbox=inbox, status=tmp_path / "update.json", **kw)


def files(inbox):
    return sorted(p.name for p in inbox.iterdir()) if inbox.exists() else []


# ---------------------------------------------------------------- the running version

def test_the_os_version_is_the_integer_in_the_release_file(tmp_path, monkeypatch):
    release = tmp_path / "urban-os-release"
    monkeypatch.setenv("URBAN_OS_RELEASE", str(release))
    assert os_version() is None  # no file: unknown, and an unknown device is never offered an update
    for text, want in (("NAME=URBAN\nVERSION=4\nVERSION_ID=9\n", 4), ('VERSION="12"\n', 12), ("VERSION=\n", None),
                       ("VERSION=2.1\n", None), ("VERSION=-3\n", None), ("VERSION_ID=7\n", None), ("junk", None)):
        release.write_text(text)
        assert os_version() == want, text
    assert os_version(tmp_path / "elsewhere") is None
    assert Updater(lambda: None).version is None and Updater(lambda: None, version=3).version == 3
    release.write_text("VERSION=5\n")
    assert Updater(lambda: None).version == 5


def test_the_reported_health_fields_are_cleaned_by_the_centre():
    ok = {"os": 3, "update": {"state": "installing", "version": 4, "reason": ""}}
    assert clean(ok) == ok
    assert clean({"os": True}) == {} and clean({"os": "3"}) == {} and clean({"os": -1}) == {}
    assert clean({"update": {"state": "exploded", "version": 4, "reason": ""}}) == {}
    assert clean({"update": {"state": "failed", "version": "4", "reason": ""}}) == {}
    assert clean({"update": {"state": "failed", "version": True, "reason": ""}}) == {}
    assert clean({"update": "failed"}) == {}
    assert clean({"update": {"state": "failed", "version": 4}})["update"]["reason"] == ""
    odd = {"state": "refused", "version": 4, "reason": "<b>bad</b>\x07 ‮signed‬ подпись " + "x" * 300}
    reason = clean({"update": odd})["update"]["reason"]
    assert len(reason) <= 120 and "\x07" not in reason and "‮" not in reason and "подпись" in reason
    for state in ("downloading", "ready", "failed", "installing", "refused", "installed"):
        assert clean({"update": {"state": state, "version": 1, "reason": ""}})["update"]["state"] == state


# ---------------------------------------------------------------- which offers are taken up

def test_an_offer_that_is_not_newer_or_not_well_formed_is_ignored_and_nothing_is_written(tmp_path, inbox):
    conn = Conn(Resp(DATA))
    up = updater(tmp_path, inbox, conn, version=3)
    bad = [offer_of(version=3), offer_of(version=2), offer_of(version=True), offer_of(sha256="0" * 63),
           offer_of(sha256="G" * 64), offer_of(size=0), offer_of(size=True), offer_of(size=10 ** 12),
           offer_of(path="/update/../../etc/passwd"), offer_of(path="http://evil.example/update/" + "a" * 64),
           offer_of(path="/update/" + "a1" * 32 + "?x=1"), "update", None, {}]
    assert [up.offer(spec) for spec in bad] == [False] * len(bad)
    up.drain()
    assert conn.requests == [] and not inbox.exists() and up.state is None
    assert Updater(lambda: conn, version=None, inbox=inbox).offer(offer_of(version=9)) is False  # unknown running system
    assert valid_offer(offer_of()) == offer_of()


def test_an_offer_already_handled_or_overtaken_by_the_root_helper_is_ignored(tmp_path, inbox):
    status = tmp_path / "update.json"
    up = updater(tmp_path, inbox, Conn(Resp(DATA)))
    assert up.offer(offer_of()) is True
    assert up.offer(offer_of()) is False  # downloading: not twice
    up.drain()
    assert up.state["state"] == "ready" and up.offer(offer_of()) is False  # handed to the helper: not again
    for state in ("installing", "refused", "installed"):
        status.write_text(json.dumps({"state": state, "version": 4, "reason": "x"}))
        assert up.offer(offer_of(version=4)) is False, state  # the helper has taken or turned down that very one
    status.write_text(json.dumps({"state": "installing", "version": 4, "reason": ""}))
    assert up.offer(offer_of(version=5)) is False  # a reboot is coming: nothing else is fetched meanwhile
    status.write_text(json.dumps({"state": "refused", "version": 4, "reason": "x"}))
    assert up.offer(offer_of(version=5)) is True  # but a refusal of an older one does not stand in the way of a newer


# ---------------------------------------------------------------- download and hand-over

def test_a_verified_package_lands_in_the_inbox_with_a_request_and_nothing_else(tmp_path, inbox):
    conn = Conn(Resp(DATA))
    up = updater(tmp_path, inbox, conn)
    assert up.offer(offer_of())
    up.drain()
    assert conn.requests == [("GET", "/update/" + "a1" * 32)] and conn.closed
    assert files(inbox) == ["request.json", "urban-3.raucb"]  # no .part, no .tmp
    assert (inbox / "urban-3.raucb").read_bytes() == DATA
    assert json.loads((inbox / "request.json").read_text()) == {
        "version": 3, "file": "urban-3.raucb", "sha256": hashlib.sha256(DATA).hexdigest(), "size": len(DATA)}
    assert up.state == {"state": "ready", "version": 3, "reason": ""}
    assert list(tmp_path.iterdir()) == [inbox]  # nothing was written anywhere else


@pytest.mark.parametrize("what, resp, reason", [
    ("hash", Resp(bytes([DATA[0] ^ 1]) + DATA[1:]), "hash"),
    ("short", Resp(DATA[:-1]), "shorter"),
    ("long", Resp(DATA + b"!"), "larger"),
    ("not found", Resp(b"", status=404), "404"),
    ("length header", Resp(DATA, length=len(DATA) + 2), "length"),
])
def test_a_package_that_differs_from_the_offer_is_refused_with_its_reason_and_leaves_no_file(tmp_path, inbox, what, resp, reason):
    (inbox).mkdir()
    (inbox / "request.json").write_text('{"version": 2}')  # an earlier request, which must stay as it was
    up = updater(tmp_path, inbox, Conn(resp))
    up.offer(offer_of())
    up.drain()
    assert up.state["state"] == "failed" and up.state["version"] == 3 and reason in up.state["reason"], up.state
    assert len(up.state["reason"]) <= 120 and up.state["reason"].isascii()
    assert files(inbox) == ["request.json"] and (inbox / "request.json").read_text() == '{"version": 2}'
    assert up.update_state()["state"] == "failed"
    assert up.offer(offer_of()) is True  # the centre offers it again after a while: a failure is not final


def test_a_lying_centre_cannot_make_the_device_write_more_than_the_offered_size(tmp_path, inbox):
    resp = Endless()
    up = updater(tmp_path, inbox, Conn(resp))
    up.offer(offer_of())
    up.drain()
    assert up.state["state"] == "failed" and "larger" in up.state["reason"]
    assert resp.served <= len(DATA) + upd.CHUNK + 1  # it stopped reading as soon as the size was exceeded
    assert files(inbox) == []


def test_a_broken_link_is_reported_as_a_failure_not_a_crash(tmp_path, inbox):
    for err in (ConnectionRefusedError("down"), http.client.RemoteDisconnected("gone"), ssl.SSLError("bad certificate"),
                TimeoutError("slow")):
        up = updater(tmp_path, inbox, Conn(error=err))
        up.offer(offer_of())
        up.drain()
        assert up.state["state"] == "failed" and err.__class__.__name__ in up.state["reason"], up.state
        assert files(inbox) == []


def test_the_device_checks_it_has_room_for_the_package(tmp_path, inbox, monkeypatch):
    monkeypatch.setattr(upd.shutil, "disk_usage", lambda path: type("U", (), {"free": len(DATA)})())
    conn = Conn(Resp(DATA))
    up = updater(tmp_path, inbox, conn)
    up.offer(offer_of())
    up.drain()
    assert up.state["state"] == "failed" and "space" in up.state["reason"] and conn.requests == [] and files(inbox) == []


def test_the_download_runs_beside_the_session_in_a_thread(tmp_path, inbox):
    async def go():
        up = updater(tmp_path, inbox, Conn(Resp(DATA)))
        assert up.offer(offer_of())  # inside the event loop: the work starts by itself
        task = up._task
        assert task is not None
        await asyncio.sleep(0)  # the session's own loop is free meanwhile
        await task
        return up

    up = asyncio.run(go())
    assert up.state["state"] == "ready" and files(inbox) == ["request.json", "urban-3.raucb"]


# ---------------------------------------------------------------- what is reported

def test_the_client_reports_its_os_and_merges_the_status_of_the_root_helper(tmp_path, inbox):
    status = tmp_path / "update.json"
    assert updater(tmp_path, inbox, version=None).health() == {}
    up = updater(tmp_path, inbox, Conn(Resp(DATA)))
    assert up.health() == {"os": 2}
    up.offer(offer_of())
    up.state = {"state": "downloading", "version": 3, "reason": ""}
    assert up.health()["update"]["state"] == "downloading"
    up.drain()
    assert up.health() == {"os": 2, "update": {"state": "ready", "version": 3, "reason": ""}}
    status.write_text(json.dumps({"state": "installing", "version": 3, "reason": ""}))  # the helper took it
    assert up.health()["update"] == {"state": "installing", "version": 3, "reason": ""}
    status.write_text(json.dumps({"state": "refused", "version": 3, "reason": "signature check failed"}))
    assert up.health()["update"] == {"state": "refused", "version": 3, "reason": "signature check failed"}
    status.write_text(json.dumps({"state": "installed", "version": 4, "reason": ""}))  # newer than ours
    assert up.health()["update"]["version"] == 4
    status.write_text(json.dumps({"state": "refused", "version": 2, "reason": "old news"}))  # older than ours
    assert up.health()["update"] == {"state": "ready", "version": 3, "reason": ""}
    for junk in ("{", "[]", json.dumps({"state": "weird", "version": 5}), json.dumps({"state": "installed", "version": "5"})):
        status.write_text(junk)
        assert up.health()["update"]["state"] == "ready"
    assert clean(up.health()) == up.health()  # and the centre keeps all of it


# ---------------------------------------------------------------- the unit's and the district's client

@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path / "pki")
    return tmp_path / "pki"


def gateway(pki):
    return DeviceHub(Registry(pki / "devices.json"), crypto.load_private((pki / "gateway.key").read_text()),
                     lambda *a: None, wall=lambda: WALL)


def test_a_unit_obeys_a_signed_offer_for_itself_and_never_hands_it_to_the_controller(pki, tmp_path, inbox):
    hub = gateway(pki)
    ctl = NoController()
    up = updater(tmp_path, inbox, Conn(Resp(DATA)))
    dev = EdgeDevice("edge-J-01", "T1", crypto.load_private((pki / "edge" / "edge-J-01.key").read_text()),
                     crypto.load_public((pki / "gateway.pub").read_text()), ctl,
                     state_path=tmp_path / "seq", wall=lambda: WALL, updater=up)
    dev.tick(100)
    env = hub.envelope("edge-J-01", {"update": offer_of()})  # no "tls" in it: it is about the unit, not a junction
    assert dev.on_command(env) is True
    assert dev.on_command(env) is False  # replayed
    assert dev.report({})["events"][-1]["reason"] == "replayed or out of order"
    up.drain()
    assert files(inbox) == ["request.json", "urban-3.raucb"] and dev.last_seq == env["seq"]
    assert (tmp_path / "seq").read_text() == str(env["seq"])
    health = dev.report({})["health"]
    assert health["os"] == 2 and health["update"]["state"] == "ready"
    assert clean(health)["update"]["version"] == 3
    # forged or misaddressed offers are refused like any command, and start nothing
    up2 = updater(tmp_path, tmp_path / "inbox2", Conn(Resp(DATA)))
    dev.updater = up2
    stranger = DeviceHub(Registry(pki / "devices.json"), crypto.new_key(), lambda *a: None, wall=lambda: WALL)
    assert dev.on_command(stranger.envelope("edge-J-01", {"update": offer_of(version=4)})) is False
    assert dev.report({})["events"][-1]["reason"] == "bad signature"
    assert dev.on_command(hub.envelope("edge-OTHER", {"update": offer_of(version=4)})) is False
    assert dev.report({})["events"][-1]["reason"] == "addressed to another device"
    tampered = hub.envelope("edge-J-01", {"update": offer_of(version=4)})
    tampered["body"]["update"]["sha256"] = "0" * 64
    assert dev.on_command(tampered) is False
    stale = DeviceHub(Registry(pki / "devices.json"), crypto.load_private((pki / "gateway.key").read_text()),
                      lambda *a: None, wall=lambda: WALL - 600).envelope("edge-J-01", {"update": offer_of(version=4)})
    assert dev.on_command(stale) is False
    assert dev.report({})["events"][-1]["reason"] == "stale command"
    up2.drain()
    assert up2.state is None and not (tmp_path / "inbox2").exists()


def test_a_unit_still_refuses_a_command_for_another_junction(pki, tmp_path):
    hub = gateway(pki)
    dev = EdgeDevice("edge-J-01", "T1", crypto.load_private((pki / "edge" / "edge-J-01.key").read_text()),
                     crypto.load_public((pki / "gateway.pub").read_text()), NoController(), wall=lambda: WALL)
    assert dev.on_command(hub.envelope("edge-J-01", {"op": "lease", "id": "c", "tls": "T9", "plan": 1, "ttl": 60})) is False
    assert dev.report({})["events"][-1]["reason"] == "not my junction"
    assert dev.on_command(hub.envelope("edge-J-01", {"update": offer_of()})) is True  # no updater here: taken and ignored


def test_a_district_handles_an_update_offer_but_still_no_junction_command(pki, tmp_path, inbox):
    hub = gateway(pki)
    hub.registry.enroll_district("district-1", "10.1.0.2", crypto.public_hex(crypto.new_key()))
    up = updater(tmp_path, inbox, Conn(Resp(DATA)))
    dev = DistrictDevice("district-1", crypto.new_key(), gateway_public=crypto.load_public((pki / "gateway.pub").read_text()),
                         updater=up, state_path=tmp_path / "district.seq", wall=lambda: WALL)
    env = hub.envelope("district-1", {"update": offer_of()})
    dev.on_command({"type": "cmd", **env})
    up.drain()
    assert files(inbox) == ["request.json", "urban-3.raucb"] and (tmp_path / "district.seq").read_text() == str(env["seq"])
    health = dev.health()
    assert health["os"] == 2 and health["update"] == {"state": "ready", "version": 3, "reason": ""}
    assert clean(health)["update"]["state"] == "ready"
    assert dev.make_report({}, 1)["events"] == []  # nothing was refused
    dev.on_command({"type": "cmd", **env})  # a replay
    dev.on_command({"type": "cmd", **hub.envelope("district-1", {"op": "lease", "tls": "T1"})})  # a junction command
    forged = DeviceHub(hub.registry, crypto.new_key(), lambda *a: None, wall=lambda: WALL).envelope("district-1", {"update": offer_of(version=4)})
    dev.on_command({"type": "cmd", **forged})
    dev.on_command({"type": "cmd", **hub.envelope("edge-J-01", {"update": offer_of(version=4)})})  # meant for someone else
    reasons = [e["reason"] for e in dev.make_report({}, 2)["events"]]
    assert reasons == ["replayed or out of order", "a district server takes no commands", "bad signature",
                       "addressed to another device"]
    assert up.queue == [] and up.state["version"] == 3
    # its seq survives a restart: the replay is still refused by a fresh client
    again = DistrictDevice("district-1", crypto.new_key(), gateway_public=crypto.load_public((pki / "gateway.pub").read_text()),
                           updater=updater(tmp_path, tmp_path / "i2", Conn(Resp(DATA))), state_path=tmp_path / "district.seq",
                           wall=lambda: WALL)
    again.on_command({"type": "cmd", **env})
    assert again.make_report({}, 3)["events"][0]["reason"] == "replayed or out of order"


# ---------------------------------------------------------------- through the real listener

@contextlib.contextmanager
def listener(app, certfile=None, keyfile=None):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", ssl_certfile=certfile, ssl_keyfile=keyfile))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "the test listener did not start"
    try:
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def offered(pki, tmp_path):
    """A hub with a release in its store and the signed offer it makes to edge-J-01, plus the app serving the link."""
    store = ReleaseStore(tmp_path / "releases")
    store.add(_write(tmp_path / "src.raucb", DATA), 3)
    hub = gateway(pki)
    hub.releases = store
    sent = []

    class Session:
        async def send_text(self, text):
            sent.append(json.loads(text))

    hub.sessions["edge-J-01"] = Session()
    hub.seen["edge-J-01"] = {"at": WALL, "health": {"os": 2}}
    asyncio.run(hub._offer("edge-J-01", hub.registry.devices["edge-J-01"]))
    app = FastAPI()
    app.include_router(hub.update_routes())
    return app, sent[0]["body"]["update"]


def _write(path, data):
    path.write_bytes(data)
    return path


def test_the_client_fetches_the_package_from_the_hubs_own_route(pki, tmp_path, inbox):
    app, spec = offered(pki, tmp_path)
    with listener(app) as port:
        up = updater(tmp_path, inbox, None)
        up.connect = lambda: http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        assert up.offer(spec)
        up.drain()
        assert up.state == {"state": "ready", "version": 3, "reason": ""}, up.state
        assert (inbox / "urban-3.raucb").read_bytes() == DATA and files(inbox) == ["request.json", "urban-3.raucb"]
        # an unknown link is a 404 and the client reports it
        up2 = updater(tmp_path, tmp_path / "inbox2", None)
        up2.connect = lambda: http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        up2.offer({**spec, "path": "/update/" + "0" * 64})
        up2.drain()
        assert up2.state["state"] == "failed" and "404" in up2.state["reason"] and files(tmp_path / "inbox2") == []


def test_the_download_uses_the_tls_name_of_the_websocket_while_connecting_to_an_address(pki, tmp_path, inbox):
    app, spec = offered(pki, tmp_path)
    context = ssl.create_default_context(cafile=str(pki / "ca.pem"))  # pin our CA, as the websocket does
    with listener(app, str(pki / "server.pem"), str(pki / "server.key")) as port:
        up = make_updater(f"127.0.0.1:{port}", "localhost", context)  # an IP address, the certificate says "localhost"
        up.version, up.inbox, up.status = 2, inbox, tmp_path / "update.json"
        assert up.offer(spec)
        up.drain()
        assert up.state == {"state": "ready", "version": 3, "reason": ""}, up.state
        assert (inbox / "urban-3.raucb").read_bytes() == DATA
        # the wrong name, or a CA that did not sign the certificate, and nothing is fetched
        for name, ctx in (("somewhere-else", context), ("localhost", ssl.create_default_context())):
            other = tmp_path / ("inbox-" + name)
            bad = make_updater(f"127.0.0.1:{port}", name, ctx)
            bad.version, bad.inbox, bad.status = 2, other, tmp_path / "update.json"
            bad.offer(spec)
            bad.drain()
            assert bad.state["state"] == "failed" and "SSL" in bad.state["reason"] and files(other) == [], bad.state
