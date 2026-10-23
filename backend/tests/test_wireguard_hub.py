"""The centre's end of the WireGuard tunnel between a junction unit and its district (G2, docs/wireguard-design.md).
The hub numbers a district and a unit's tunnel address once and for ever (an address left behind is retired and never
given again), takes a device's first tunnel key from its authenticated health report and holds a changed key until an
admin confirms it, sends the district its signed peer list and the unit its signed link after the first report of each
session and on every change (and again, backing off, while the device has not applied it), drops a revoked unit from
the next list, switches a district to mode only on an admin's request, and sends nothing at all to a device that never
reported a tunnel key. (The device side is in test_wireguard_devices.py, the root helper in test_wireguard_helper.py.)"""
import asyncio
import dataclasses
import json

import pytest
from fastapi import FastAPI, Header, HTTPException, WebSocketDisconnect

from urban.devices import crypto
from urban.devices.health import clean
from urban.devices.hub import WG_RESEND, WG_RESEND_MAX, DeviceHub
from urban.devices.registry import Registry, provision
from urban.net import wireguard as wg
from urban.platform import Platform
from urban.release import ReleaseStore

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"},
             {"id": "J-03", "tls": "T3", "name": "Third"}]
PLAN = wg.Plan.of()
WALL = 1_800_000_000
UNIT_ROW = {"address", "fingerprint", "pending", "state", "proven", "handshake_age", "applied", "epoch", "district",
            "reason", "behind", "problem", "ignored_keys"}
DISTRICT_ROW = {"index", "address", "fingerprint", "pending", "mode", "state", "peers", "peers_up", "unproven", "applied",
                "epoch", "reason", "behind", "ignored_keys"}


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path / "pki")
    return tmp_path / "pki"


class Rig:
    """A centre with two district servers (district-1 at `address`, district-2 at 10.2.0.2) and three junction units:
    edge-J-01 and edge-J-02 through district-1, edge-J-03 straight to the centre. Each has its own tunnel key to report."""
    def __init__(self, pki, address="10.1.0.2", releases=None):
        self.pki, self.now, self.audited = pki, WALL, []
        registry = Registry(pki / "devices.json")
        self.keys = {f"edge-{j['id']}": crypto.load_private((pki / "edge" / f"edge-{j['id']}.key").read_text())
                     for j in JUNCTIONS}
        for district, where in (("district-1", address), ("district-2", "10.2.0.2")):
            self.keys[district] = crypto.new_key()
            registry.enroll_district(district, where, crypto.public_hex(self.keys[district]))
        registry.assign("edge-J-01", "district-1")
        registry.assign("edge-J-02", "district-1")
        self.tunnel = {device: wg.new_keypair()[1] for device in self.keys}
        self.gateway_public = crypto.load_public((pki / "gateway.pub").read_text())
        self.releases = releases
        self.hub = self.new_hub()

    @property
    def registry(self) -> Registry:
        """The centre's own registry (a change made by another process is written with a Registry of its own)."""
        return self.hub.registry

    def new_hub(self) -> DeviceHub:
        """The centre (again, after a restart: what it kept in memory is gone, the registry is on disk)."""
        return DeviceHub(Registry(self.pki / "devices.json"), crypto.load_private((self.pki / "gateway.key").read_text()),
                         on_report=lambda rec, rep: None, wall=lambda: self.now, releases=self.releases,
                         audit=lambda event, target, note="", actor="platform":
                             self.audited.append((event, target, note, actor)))

    def health(self, device, os_version=9, **fields):
        """A report's health with the tunnel part its service copies from the helper's status (nothing applied yet)."""
        return {"os": os_version, "wg": {"public": self.tunnel[device], "state": "none", "applied": 0, "epoch": 0,
                                         "reason": "", **fields}}

    def events(self, name):
        return [a[1:] for a in self.audited if a[0] == name]

    def on_disk(self, device):
        return Registry(self.pki / "devices.json").devices[device]

    def serve(self, device, *script):
        """One whole session of a device; returns it."""
        session = Session(self, device, script)
        asyncio.run(self.hub.serve(session))
        return session


@pytest.fixture
def rig(pki):
    return Rig(pki)


@dataclasses.dataclass
class At:
    """A report sent `seconds` after WALL; health may be a function of the session, worked out when it is sent."""
    seconds: int
    health: object


class Session:
    """A device's connection to the hub: it signs the challenge, then for each item of its script sends a report (a
    health dict, or At) or lets something happen at the centre meanwhile (a callable; a coroutine is awaited). At the
    end it hangs up, or with stay=True it keeps the session open until leave() or until the hub closes it."""
    def __init__(self, rig, device, script, stay=False):
        self.rig, self.device, self.script, self.stay = rig, device, list(script), stay
        self.sent, self.step, self.closed = [], 0, None
        self.idle, self.gone = asyncio.Event(), asyncio.Event()

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        self.closed = code
        self.gone.set()

    def leave(self):
        self.gone.set()

    async def receive_text(self):
        self.step += 1
        if self.step == 1:
            nonce = self.sent[0]["nonce"]
            return json.dumps({"type": "hello", "device": self.device, "version": "0.1.0", "epoch": 1,
                               "sig": crypto.sign(self.rig.keys[self.device], {"device": self.device, "nonce": nonce})})
        while self.script:
            item = self.script.pop(0)
            if isinstance(item, At):
                self.rig.now = WALL + item.seconds
                item = item.health(self) if callable(item.health) else item.health
            if isinstance(item, dict):
                return json.dumps({"type": "report", "t": 1, "queues": {}, "events": [], "health": item})
            done = item()
            if asyncio.iscoroutine(done):
                await done
        if self.stay:
            self.idle.set()
            await self.gone.wait()
        raise WebSocketDisconnect()

    def frames(self):
        return [f for f in self.sent if f["type"] == "cmd" and "wg" in f["body"]]

    def wg(self):
        """The tunnel messages it received, in order."""
        return [f["body"]["wg"] for f in self.frames()]


def applied(session) -> int:
    """The version of the last tunnel message the session received: what a device that applied it reports."""
    msgs = session.wg()
    return msgs[-1]["version"] if msgs else 0


def peers_of(msg, rig, district="district-1"):
    lst = wg.parse_peer_list(msg, rig.gateway_public, PLAN, district)
    return {p.unit: (p.public, p.ip) for p in lst.peers}


def held_open(rig, first, then):
    """`first` connects and stays connected (its script done) while the coroutine `then()` runs; then it leaves."""
    async def go():
        task = asyncio.create_task(rig.hub.serve(first))
        await first.idle.wait()
        out = await then()
        first.leave()
        await task
        return out
    return asyncio.run(go())


# ---------------------------------------------------------------- the admin's routes, straight into the ASGI app

def role(*allowed):
    def check(x_user: str | None = Header(None)):
        kind, _, name = (x_user or "").partition(":")
        if kind not in allowed:
            raise HTTPException(403, "no role for this")
        return name
    return check


viewer, admin = role("viewer", "operator", "admin"), role("admin")


def routes(rig):
    app = FastAPI()
    app.include_router(rig.hub.wg_routes(viewer, admin))
    return app


async def acall(app, path, body=None, user="admin:ann"):
    """One POST straight into the ASGI app (the test environment has no HTTP client library)."""
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    if user:
        headers.append((b"x-user", user.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 1), "server": ("test", 80)}
    out, sent_body = {"status": None, "body": b""}, [False]

    async def receive():
        if sent_body[0]:
            await asyncio.sleep(3600)
        sent_body[0] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return out["status"], (json.loads(out["body"]) if out["body"] else None)


def call(app, path, body=None, user="admin:ann"):
    return asyncio.run(acall(app, path, body, user))


# ---------------------------------------------------------------- what a report may say about the tunnel

def test_the_tunnel_part_of_a_report_keeps_only_what_the_helper_writes_and_needs_a_real_key():
    key = wg.new_keypair()[1]
    unit = {"public": key, "state": "up", "applied": 1792000000123, "epoch": 1, "address": "10.77.1.2", "reason": "",
            "proven": True, "handshake": 1792000123}
    district = {"public": key, "state": "down", "applied": 5, "epoch": 0, "address": "10.77.1.1",
                "reason": "older version", "peers": 254, "peers_up": 0, "mode": "only"}
    assert clean({"wg": unit})["wg"] == unit and clean({"wg": district})["wg"] == district
    assert clean({"wg": {**unit, "role": "unit", "t": 5, "server": "10.77.1.1", "extra": "<b>"}})["wg"] == unit
    for bad_key in (None, "", "short", key[:-2] + "==", "A" * 43 + "=", 5, ["x"]):
        assert "wg" not in clean({"wg": {**unit, "public": bad_key}, "os": 3})  # no key: no tunnel part at all
    assert "wg" not in clean({"wg": "up"}) and "wg" not in clean({"wg": [unit]})
    junk = {"state": ["up"], "applied": 10 ** 15, "epoch": True, "address": "10.78.1.2", "reason": "because",
            "proven": 1, "handshake": -1, "peers": 255, "peers_up": 2.0, "mode": {"only": 1}}
    assert clean({"wg": {"public": key, **junk}})["wg"] == {"public": key}  # each bad field is dropped alone
    for address in ("010.77.1.2", "10.77.1.2 ", "10.77.1.256", "10.77.001.2", "::ffff:10.77.1.2"):
        assert "address" not in clean({"wg": {"public": key, "address": address}})["wg"]
    edge = {"public": key, "applied": 10 ** 15 - 1, "epoch": 10 ** 6 - 1, "handshake": 10 ** 11 - 1, "peers": 0,
            "proven": False, "state": "clock", "mode": "off", "reason": "bad gateway certificate"}
    assert clean({"wg": edge})["wg"] == edge


# ---------------------------------------------------------------- numbers: given once and for ever

def test_a_district_and_its_units_are_numbered_once_and_the_numbers_never_move(rig):
    rig.serve("district-1", rig.health("district-1"))
    rec = rig.on_disk("district-1")
    assert rec["wg_index"] == 1 and rec["wg_public"] == rig.tunnel["district-1"]  # numbered when its key was accepted
    rig.serve("edge-J-02", rig.health("edge-J-02"))
    rig.serve("edge-J-01", rig.health("edge-J-01"))
    assert rig.on_disk("edge-J-02")["wg_ip"] == "10.77.1.2" and rig.on_disk("edge-J-01")["wg_ip"] == "10.77.1.3"
    assert rig.events("wg-address-assigned") == [("district-1", "district-1 -> 1", "platform"),
                                                 ("edge-J-02", "edge-J-02 -> 10.77.1.2", "platform"),
                                                 ("edge-J-01", "edge-J-01 -> 10.77.1.3", "platform")]
    rig.hub = rig.new_hub()  # the centre restarts: the numbers are on disk, in whatever order the devices come back
    for device in ("edge-J-01", "edge-J-02", "district-1", "edge-J-01"):
        rig.serve(device, rig.health(device), rig.health(device))
    assert [rig.on_disk(d).get("wg_ip") for d in ("edge-J-01", "edge-J-02")] == ["10.77.1.3", "10.77.1.2"]
    assert rig.on_disk("district-1")["wg_index"] == 1 and len(rig.events("wg-address-assigned")) == 3
    rig.serve("district-2", rig.health("district-2"))
    assert rig.on_disk("district-2")["wg_index"] == 2 and rig.events("wg-address-assigned")[-1][1] == "district-2 -> 2"
    rig.serve("edge-J-03", rig.health("edge-J-03"))  # routed straight to the centre: no district, no address
    assert "wg_ip" not in rig.on_disk("edge-J-03") and rig.on_disk("edge-J-03")["wg_public"] == rig.tunnel["edge-J-03"]


def test_a_unit_gets_its_address_before_its_district_has_a_key_and_the_district_its_number_then(rig):
    session = rig.serve("edge-J-01", rig.health("edge-J-01"))
    assert rig.on_disk("district-1")["wg_index"] == 1 and "wg_public" not in rig.on_disk("district-1")
    assert rig.on_disk("edge-J-01")["wg_ip"] == "10.77.1.2"
    assert session.wg() == []  # but no link: the district has no tunnel key to put in it
    [row] = [r for r in rig.hub.status() if r["id"] == "edge-J-01"]
    assert row["wg"]["problem"] == "district has no tunnel key yet" and row["wg"]["address"] == "10.77.1.2"
    rig.serve("district-1", rig.health("district-1"))  # its key now: it keeps the number it got
    assert rig.on_disk("district-1")["wg_index"] == 1 and len(rig.events("wg-address-assigned")) == 2
    assert wg.parse_link(rig.serve("edge-J-01", rig.health("edge-J-01")).wg()[0], rig.gateway_public, PLAN,
                         "edge-J-01").address == "10.77.1.2"


def test_an_address_left_behind_is_retired_and_never_given_again(rig):
    for device in ("district-1", "district-2", "edge-J-01", "edge-J-02"):
        rig.serve(device, rig.health(device))
    assert [rig.on_disk(d)["wg_ip"] for d in ("edge-J-01", "edge-J-02")] == ["10.77.1.2", "10.77.1.3"]
    rig.registry.assign("edge-J-01", "district-1")  # the same district: nothing moves
    assert rig.on_disk("edge-J-01")["wg_ip"] == "10.77.1.2" and "wg_retired" not in rig.on_disk("district-1")
    rig.registry.assign("edge-J-01", "district-2")  # moved
    assert rig.on_disk("district-1")["wg_retired"] == ["10.77.1.2"] and "wg_ip" not in rig.on_disk("edge-J-01")
    rig.serve("edge-J-01", rig.health("edge-J-01"))
    assert rig.on_disk("edge-J-01")["wg_ip"] == "10.77.2.2"
    rig.registry.assign("edge-J-01", "district-1")  # and back: its old address stays retired
    rig.serve("edge-J-01", rig.health("edge-J-01"))
    assert rig.on_disk("edge-J-01")["wg_ip"] == "10.77.1.4" and rig.on_disk("district-2")["wg_retired"] == ["10.77.2.2"]
    rig.registry.assign("edge-J-01", None)  # straight to the centre (provision assign --direct)
    assert rig.on_disk("district-1")["wg_retired"] == ["10.77.1.2", "10.77.1.4"] and "wg_ip" not in rig.on_disk("edge-J-01")
    junction = JUNCTIONS[1]  # re-enrolled with another district (the same key): the old address is retired too
    rig.registry.enroll("edge-J-02", junction, crypto.public_hex(rig.keys["edge-J-02"]), district="district-2")
    assert rig.on_disk("district-1")["wg_retired"] == ["10.77.1.2", "10.77.1.4", "10.77.1.3"]
    rig.serve("edge-J-02", rig.health("edge-J-02"))
    assert rig.on_disk("edge-J-02")["wg_ip"] == "10.77.2.3"  # .2 of district-2 is retired
    rig.registry.assign("edge-J-03", "district-1")
    rig.serve("edge-J-03", rig.health("edge-J-03"))
    assert rig.on_disk("edge-J-03")["wg_ip"] == "10.77.1.5"  # the lowest address never given to anyone
    rig.registry.set_status("edge-J-03", "revoked")
    assert rig.on_disk("edge-J-03")["wg_ip"] == "10.77.1.5"  # a revoked unit keeps its address
    asyncio.run(rig.hub.remove("edge-J-03"))  # removed for good: its address is retired with it
    assert rig.on_disk("district-1")["wg_retired"][-1] == "10.77.1.5"
    rig.registry.set_status("district-2", "revoked")
    asyncio.run(rig.hub.remove("district-2"))  # a removed district: its units lose the district and the address
    assert "district" not in rig.on_disk("edge-J-02") and "wg_ip" not in rig.on_disk("edge-J-02")
    assert rig.on_disk("edge-J-02")["wg_public"] == rig.tunnel["edge-J-02"]  # its key stays its key


# ---------------------------------------------------------------- keys: the first one taken, a changed one waits

def test_the_first_tunnel_key_is_taken_from_the_report_and_a_changed_one_waits_for_an_admin(rig):
    first, second, third = rig.tunnel["edge-J-01"], wg.new_keypair()[1], wg.new_keypair()[1]
    rig.serve("district-1", rig.health("district-1"))
    report = lambda key: {"os": 9, "wg": {"public": key}}  # noqa: E731
    rig.serve("edge-J-01", report(first), report(second), report(third), report(second), report(third), report(third))
    rec = rig.on_disk("edge-J-01")
    assert rec["wg_public"] == first and rec["wg_pending"] == second  # the first stays the one in use
    assert rig.events("wg-key-accepted") == [("district-1", wg.fingerprint(rig.tunnel["district-1"]), "platform"),
                                             ("edge-J-01", wg.fingerprint(first), "platform")]
    assert rig.events("wg-key-pending") == [("edge-J-01", f"{wg.fingerprint(first)} -> {wg.fingerprint(second)}",
                                             "platform")]  # once: the third key in that session is not even audited
    row = {r["id"]: r for r in rig.hub.status()}["edge-J-01"]["wg"]
    assert (row["fingerprint"], row["pending"], row["ignored_keys"]) == (wg.fingerprint(first), wg.fingerprint(second), 1)
    district = rig.serve("district-1", rig.health("district-1"))
    assert peers_of(district.wg()[0], rig)["edge-J-01"] == (first, "10.77.1.2")  # a waiting key never replaces it
    rig.serve("edge-J-01", report(third))  # a new session may set another key waiting (audited again)
    assert rig.on_disk("edge-J-01")["wg_pending"] == third
    assert rig.events("wg-key-pending")[-1][1] == f"{wg.fingerprint(first)} -> {wg.fingerprint(third)}"
    rig.serve("edge-J-01", report(first))  # it reports its accepted key again: the waiting one is dropped
    assert "wg_pending" not in rig.on_disk("edge-J-01") and rig.on_disk("edge-J-01")["wg_public"] == first
    assert len(rig.events("wg-key-pending")) == 2 and len(rig.events("wg-key-accepted")) == 2


def test_the_admin_confirms_a_waiting_key_and_the_district_gets_it_at_its_next_report(rig):
    app = routes(rig)
    first, second = rig.tunnel["edge-J-01"], wg.new_keypair()[1]
    rig.serve("district-1", rig.health("district-1"))
    rig.serve("edge-J-01", {"os": 9, "wg": {"public": first}}, {"os": 9, "wg": {"public": second}})
    assert call(app, "/api/devices/edge-J-01/wg-accept", user="operator:olga")[0] == 403
    assert call(app, "/api/devices/edge-J-01/wg-accept", user="viewer:vic")[0] == 403
    assert rig.on_disk("edge-J-01")["wg_public"] == first and rig.events("wg-key-confirmed") == []
    status, body = call(app, "/api/devices/edge-J-01/wg-accept")
    assert status == 200 and body == {"id": "edge-J-01", "fingerprint": wg.fingerprint(second)}
    rec = rig.on_disk("edge-J-01")
    assert rec["wg_public"] == second and "wg_pending" not in rec and rec["wg_ip"] == "10.77.1.2"
    assert rig.events("wg-key-confirmed") == [("edge-J-01", f"{wg.fingerprint(first)} -> {wg.fingerprint(second)}", "ann")]
    assert call(app, "/api/devices/edge-J-01/wg-accept") == (409, {"detail": "no tunnel key waits for confirmation"})
    assert call(app, "/api/devices/edge-J-02/wg-accept")[0] == 409  # never reported one
    assert call(app, "/api/devices/edge-J-99/wg-accept") == (404, {"detail": "unknown device"})
    district = rig.serve("district-1", rig.health("district-1"))
    assert peers_of(district.wg()[0], rig)["edge-J-01"] == (second, "10.77.1.2")


def test_a_district_key_confirmed_changes_the_links_of_its_units(rig):
    app = routes(rig)
    old, new = rig.tunnel["district-1"], wg.new_keypair()[1]
    rig.serve("district-1", rig.health("district-1"))
    unit = rig.serve("edge-J-01", rig.health("edge-J-01"))
    assert wg.parse_link(unit.wg()[0], rig.gateway_public, PLAN, "edge-J-01").server_public == old
    rig.tunnel["district-1"] = new
    rig.serve("district-1", rig.health("district-1"))
    assert rig.on_disk("district-1")["wg_pending"] == new
    assert wg.parse_link(rig.serve("edge-J-01", rig.health("edge-J-01")).wg()[0], rig.gateway_public, PLAN,
                         "edge-J-01").server_public == old  # still the accepted key
    assert call(app, "/api/devices/district-1/wg-accept")[0] == 200
    link = wg.parse_link(rig.serve("edge-J-01", rig.health("edge-J-01")).wg()[0], rig.gateway_public, PLAN, "edge-J-01")
    assert link.server_public == new and link.index == 1 and link.address == "10.77.1.2"


def test_one_tunnel_key_belongs_to_one_record(rig):
    rig.serve("edge-J-01", rig.health("edge-J-01"))
    taken = rig.tunnel["edge-J-01"]
    rig.serve("edge-J-02", {"os": 9, "wg": {"public": taken}}, {"os": 9, "wg": {"public": taken}})  # a copied key
    assert "wg_public" not in rig.on_disk("edge-J-02")
    assert rig.events("wg-key-refused") == [("edge-J-02", "in use by edge-J-01", "platform")]  # once per session
    rig.serve("edge-J-02", rig.health("edge-J-02"))  # its own key is fine
    rig.serve("edge-J-01", {"os": 9, "wg": {"public": wg.new_keypair()[1]}})  # J-01 now has a key waiting
    waiting = rig.on_disk("edge-J-01")["wg_pending"]
    rig.serve("district-1", {"os": 9, "wg": {"public": waiting}}, {"os": 9, "wg": {"public": taken}})
    assert "wg_public" not in rig.on_disk("district-1")  # neither an accepted nor a waiting key of another record
    assert rig.events("wg-key-refused")[1:] == [("district-1", "in use by edge-J-01", "platform")]
    rig.serve("district-1", {"os": 9, "wg": {"public": waiting}})  # a new session: audited again
    assert len(rig.events("wg-key-refused")) == 3


def test_a_replaced_box_starts_over_with_its_own_first_tunnel_key_and_keeps_the_address(rig):
    rig.serve("district-1", rig.health("district-1"))
    rig.serve("edge-J-01", rig.health("edge-J-01"), {"os": 9, "wg": {"public": wg.new_keypair()[1]}})
    assert rig.on_disk("edge-J-01").get("wg_pending")
    rig.keys["edge-J-01"] = crypto.new_key()  # a new box at the junction: a new device key, enrolled by the admin
    asyncio.run(rig.hub.enroll("edge-J-01", JUNCTIONS[0], crypto.public_hex(rig.keys["edge-J-01"]), district="district-1"))
    rec = rig.on_disk("edge-J-01")
    assert "wg_public" not in rec and "wg_pending" not in rec and rec["wg_ip"] == "10.77.1.2"
    rig.tunnel["edge-J-01"] = wg.new_keypair()[1]
    session = rig.serve("edge-J-01", rig.health("edge-J-01"))
    assert rig.on_disk("edge-J-01")["wg_public"] == rig.tunnel["edge-J-01"]  # accepted by itself, no admin
    assert wg.parse_link(session.wg()[0], rig.gateway_public, PLAN, "edge-J-01").address == "10.77.1.2"
    rig.keys["district-1"] = crypto.new_key()  # a replaced district server too
    asyncio.run(rig.hub.enroll_district("district-1", "10.1.0.2", crypto.public_hex(rig.keys["district-1"])))
    assert "wg_public" not in rig.on_disk("district-1") and rig.on_disk("district-1")["wg_index"] == 1


def test_the_old_box_still_connected_when_its_unit_is_re_enrolled_cannot_give_the_new_box_its_tunnel_key(rig):
    rig.serve("district-1", rig.health("district-1"))
    new_box = crypto.public_hex(crypto.new_key())

    def re_enrol():  # the admin enrols the new box with the CLI, in another process, while the old one is connected
        Registry(rig.pki / "devices.json").enroll("edge-J-01", JUNCTIONS[0], new_box, district="district-1")
    old = rig.serve("edge-J-01", rig.health("edge-J-01"), re_enrol, rig.health("edge-J-01"), rig.health("edge-J-01"))
    assert old.closed is not None  # its session ended at its next report
    rec = rig.on_disk("edge-J-01")
    assert "wg_public" not in rec and rec["wg_ip"] == "10.77.1.2"  # the old box's key is not the new box's first key
    assert len(rig.events("wg-key-accepted")) == 2 and len(old.wg()) == 1  # district-1 and the old box at first


def test_a_registry_that_cannot_be_written_does_not_end_the_session(rig, monkeypatch):
    def full_disk():
        raise OSError("no space left on device")
    rig.serve("district-1")  # seen once already (written at its first session)
    monkeypatch.setattr(rig.registry, "_save", full_disk)  # taking the key and numbering both fail to write
    reached = []
    rig.serve("district-1", rig.health("district-1"), rig.health("district-1"), lambda: reached.append(True))
    assert reached == [True] and "wg_public" not in rig.on_disk("district-1")  # both reports were handled


# ---------------------------------------------------------------- the messages: built, signed, sent

def test_the_district_gets_its_signed_peer_list_and_the_unit_its_signed_link(rig):
    rig.serve("district-1", rig.health("district-1"))
    unit = rig.serve("edge-J-01", rig.health("edge-J-01"))
    rig.serve("edge-J-02", rig.health("edge-J-02"))
    rig.now = WALL + 7
    district = rig.serve("district-1", rig.health("district-1"))
    [frame] = district.frames()
    envelope = {k: frame[k] for k in ("device", "seq", "t", "body")}
    assert crypto.verify(rig.gateway_public, envelope, frame["sig"])  # an ordinary signed command
    assert frame["device"] == "district-1" and set(frame["body"]) == {"wg"} and frame["t"] == WALL + 7
    lst = wg.parse_peer_list(frame["body"]["wg"], rig.gateway_public, PLAN, "district-1")  # signed by the gateway key
    assert (lst.district, lst.index, lst.plain_relay, lst.issued) == ("district-1", 1, True, WALL + 7)
    assert {p.unit: (p.public, p.ip) for p in lst.peers} == {"edge-J-01": (rig.tunnel["edge-J-01"], "10.77.1.2"),
                                                            "edge-J-02": (rig.tunnel["edge-J-02"], "10.77.1.3")}
    assert lst.version >= (WALL + 7) * 1000  # the centre's wall clock in ms
    [frame] = unit.frames()
    link = wg.parse_link(frame["body"]["wg"], rig.gateway_public, PLAN, "edge-J-01")
    assert (link.unit, link.district, link.index, link.address) == ("edge-J-01", "district-1", 1, "10.77.1.2")
    assert link.server_public == rig.tunnel["district-1"] and link.endpoint == "10.1.0.2:51820"
    assert link.version == WALL * 1000 and link.issued == WALL
    forged = {**frame["body"]["wg"], "address": "10.77.1.9"}
    with pytest.raises(wg.MessageRefused):
        wg.parse_link(forged, rig.gateway_public, PLAN, "edge-J-01")
    first = rig.serve("district-2", rig.health("district-2")).wg()[0]
    assert wg.parse_peer_list(first, rig.gateway_public, PLAN, "district-2").peers == ()  # sent with no peers too


def test_a_district_address_with_a_port_gives_the_same_host_on_udp_51820(pki):
    rig = Rig(pki, address="10.1.0.2:18443")
    rig.serve("district-1", rig.health("district-1"))
    link = wg.parse_link(rig.serve("edge-J-01", rig.health("edge-J-01")).wg()[0], rig.gateway_public, PLAN, "edge-J-01")
    assert link.endpoint == "10.1.0.2:51820"


@pytest.mark.parametrize("address", ["district-1.city", "district-1.city:8443", "[fd00::2]:8443", "10.77.3.1"])
def test_a_district_known_only_by_a_name_or_inside_the_tunnel_network_gets_its_units_no_link(pki, address):
    rig = Rig(pki, address=address)
    rig.serve("district-1", rig.health("district-1"))
    unit = rig.serve("edge-J-01", rig.health("edge-J-01"), rig.health("edge-J-01"))
    assert unit.wg() == [] and rig.on_disk("edge-J-01")["wg_ip"] == "10.77.1.2"
    row = {r["id"]: r for r in rig.hub.status()}["edge-J-01"]["wg"]
    assert row["problem"] == "district address is not IPv4" and row["behind"] is False
    [(target, note, actor)] = rig.events("wg-link-refused")
    assert target == "edge-J-01" and address in note and actor == "platform"
    rig.serve("edge-J-01", rig.health("edge-J-01"))
    assert len(rig.events("wg-link-refused")) == 1  # once per unit and address in this process
    Registry(rig.pki / "devices.json").enroll_district("district-1", "10.1.0.9", crypto.public_hex(rig.keys["district-1"]))
    link = wg.parse_link(rig.serve("edge-J-01", rig.health("edge-J-01")).wg()[0], rig.gateway_public, PLAN, "edge-J-01")
    assert link.endpoint == "10.1.0.9:51820"  # the address was fixed (by another process, the CLI): the link goes


def test_the_message_goes_at_session_start_and_on_change_and_the_same_content_keeps_its_signed_message(rig):
    rig.serve("edge-J-01", rig.health("edge-J-01"))
    done = lambda s: rig.health("district-1", applied=applied(s))  # noqa: E731 (it applied what it got)

    def joins(device):
        return lambda: rig.hub.serve(Session(rig, device, [rig.health(device)]))

    district = Session(rig, "district-1", [At(0, rig.health("district-1")), At(0, done), joins("edge-J-02"),
                                           At(0, done), At(1, done), lambda: rig.hub.set_status("edge-J-02", "revoked"),
                                           At(5, done), At(9, done)])
    asyncio.run(rig.hub.serve(district))
    msgs = district.wg()
    versions = [m["version"] for m in msgs]
    assert len(msgs) == 3  # the first report, J-02's key accepted, J-02 revoked; nothing else in between
    assert versions[:2] == [WALL * 1000, WALL * 1000 + 1]  # max(last + 1, wall ms): the clock had not moved
    assert versions[2] == (WALL + 5) * 1000 and [m["t"] for m in msgs] == [WALL, WALL, WALL + 5]
    assert [sorted(peers_of(m, rig)) for m in msgs] == [["edge-J-01"], ["edge-J-01", "edge-J-02"], ["edge-J-01"]]
    again = Session(rig, "district-1", [At(30, rig.health("district-1", applied=versions[2])), At(31, done)])
    asyncio.run(rig.hub.serve(again))
    assert again.wg() == [msgs[2]]  # a new session gets it again, the very same signed message: nothing changed
    rig.hub = rig.new_hub()  # a restarted centre continues from its wall clock
    after = rig.serve("district-1", At(40, rig.health("district-1", applied=versions[2])))
    assert after.wg()[0]["version"] == (WALL + 40) * 1000 and peers_of(after.wg()[0], rig) == peers_of(msgs[2], rig)


def test_a_message_not_yet_applied_goes_again_after_30_60_120_s_and_then_every_16_minutes(rig):
    rig.serve("district-1", rig.health("district-1"))
    waits, t = [], 0
    pause = WG_RESEND
    while pause < WG_RESEND_MAX:
        waits.append(pause)
        pause *= 2
    assert waits == [30, 60, 120, 240, 480] and WG_RESEND_MAX == 960
    script, expected = [At(0, rig.health("edge-J-01"))], [0]
    for pause in [*waits, 960, 960]:
        script += [At(t + pause - 1, rig.health("edge-J-01")), At(t + pause, rig.health("edge-J-01"))]
        t += pause
        expected.append(t)
    sent_at = []
    script = [item for step in script for item in (step, lambda: sent_at.append(len(unit.wg())))]
    unit = Session(rig, "edge-J-01", script)
    asyncio.run(rig.hub.serve(unit))
    counts = sent_at  # after each report: how many messages it had got so far
    assert counts == [1] + [n for k in range(2, len(expected) + 1) for n in (k - 1, k)]
    assert len({json.dumps(m, sort_keys=True) for m in unit.wg()}) == 1  # always the same signed message
    v = unit.wg()[0]["version"]
    applied_now = Session(rig, "edge-J-01", [At(10_000, rig.health("edge-J-01")),
                                            At(10_001, rig.health("edge-J-01", applied=v)),
                                            At(20_000, rig.health("edge-J-01", applied=v))])
    asyncio.run(rig.hub.serve(applied_now))
    assert len(applied_now.wg()) == 1  # sent at session start; once applied, never again
    fresh = Session(rig, "edge-J-01", [At(30_000, rig.health("edge-J-01")), At(30_029, rig.health("edge-J-01")),
                                       At(30_030, rig.health("edge-J-01"))])
    asyncio.run(rig.hub.serve(fresh))
    assert len(fresh.wg()) == 2  # a new session starts the schedule anew: 30 s again


def test_a_revoked_unit_is_dropped_from_the_next_list_and_comes_back_with_its_address(rig):
    for device in ("district-1", "edge-J-01", "edge-J-02"):
        rig.serve(device, rig.health(device))
    district = Session(rig, "district-1", [rig.health("district-1"),
                                           lambda: rig.hub.set_status("edge-J-01", "revoked"),
                                           rig.health("district-1"),
                                           lambda: Registry(rig.pki / "devices.json").set_status("edge-J-02", "revoked"),
                                           rig.health("district-1"),  # the CLI, in another process
                                           lambda: rig.hub.set_status("edge-J-01", "active"),
                                           rig.health("district-1")])
    asyncio.run(rig.hub.serve(district))
    assert [sorted(peers_of(m, rig)) for m in district.wg()] == [["edge-J-01", "edge-J-02"], ["edge-J-02"], [],
                                                                 ["edge-J-01"]]
    assert peers_of(district.wg()[-1], rig)["edge-J-01"] == (rig.tunnel["edge-J-01"], "10.77.1.2")
    assert rig.on_disk("edge-J-02")["wg_ip"] == "10.77.1.3"  # kept while revoked
    versions = [m["version"] for m in district.wg()]
    assert versions == sorted(set(versions))


def test_a_unit_moved_to_another_district_leaves_the_old_list_at_once(rig):
    for device in ("district-1", "district-2", "edge-J-01", "edge-J-02"):
        rig.serve(device, rig.health(device))
    district = Session(rig, "district-1", [rig.health("district-1"), lambda: rig.hub.assign("edge-J-01", "district-2"),
                                           rig.health("district-1")])
    asyncio.run(rig.hub.serve(district))
    assert [sorted(peers_of(m, rig)) for m in district.wg()] == [["edge-J-01", "edge-J-02"], ["edge-J-02"]]
    link = wg.parse_link(rig.serve("edge-J-01", rig.health("edge-J-01")).wg()[0], rig.gateway_public, PLAN, "edge-J-01")
    assert (link.district, link.address, link.endpoint) == ("district-2", "10.77.2.2", "10.2.0.2:51820")
    assert sorted(peers_of(rig.serve("district-2", rig.health("district-2")).wg()[0], rig, "district-2")) == ["edge-J-01"]


# ---------------------------------------------------------------- mode only: by the admin, through the API

def test_the_admin_closes_and_opens_the_districts_relay_port_and_the_next_list_says_so(rig):
    app = routes(rig)
    for device in ("district-1", "edge-J-01", "edge-J-02"):
        rig.serve(device, rig.health(device))
    assert call(app, "/api/devices/district-1/wg-mode", {"mode": "only"}, user="operator:olga")[0] == 403
    assert "wg_mode" not in rig.on_disk("district-1")
    unit = Session(rig, "edge-J-01", [rig.health("edge-J-01", proven=True, state="up")], stay=True)
    status, body = held_open(rig, unit, lambda: acall(app, "/api/devices/district-1/wg-mode", {"mode": "only"}))
    assert status == 200 and body == {"id": "district-1", "mode": "only", "unproven": ["edge-J-02"]}  # J-02 is offline
    assert rig.on_disk("district-1")["wg_mode"] == "only"
    assert rig.events("wg-mode-set") == [("district-1", "only (was allow)", "ann")]
    lst = wg.parse_peer_list(rig.serve("district-1", rig.health("district-1")).wg()[0], rig.gateway_public, PLAN,
                             "district-1")
    assert lst.plain_relay is False and len(lst.peers) == 2
    status, body = call(app, "/api/devices/district-1/wg-mode", {"mode": "allow"})
    assert status == 200 and body["unproven"] == ["edge-J-01", "edge-J-02"]  # offline: not proven now
    assert rig.events("wg-mode-set")[-1] == ("district-1", "allow (was only)", "ann")
    assert wg.parse_peer_list(rig.serve("district-1", rig.health("district-1")).wg()[0], rig.gateway_public, PLAN,
                              "district-1").plain_relay is True
    assert call(app, "/api/devices/edge-J-01/wg-mode", {"mode": "only"}) == (400, {"detail": "not a district"})
    for bad in ("ONLY", "off", "", "only "):
        assert call(app, "/api/devices/district-1/wg-mode", {"mode": bad}) == (400, {"detail": "mode must be allow or only"})
    assert call(app, "/api/devices/district-9/wg-mode", {"mode": "only"})[0] == 404
    for bad in ({}, {"mode": None}, {"mode": 1}):
        assert call(app, "/api/devices/district-1/wg-mode", bad)[0] == 422
    assert rig.on_disk("district-1")["wg_mode"] == "allow" and len(rig.events("wg-mode-set")) == 2


# ---------------------------------------------------------------- nothing for devices without a tunnel

def test_a_device_that_never_reported_a_tunnel_key_gets_no_tunnel_message(rig):
    for device in ("district-1", "edge-J-01"):
        rig.serve(device, rig.health(device))  # both have keys and numbers from an earlier session
    for device in ("district-1", "edge-J-01"):
        silent = rig.serve(device)  # connected, no report yet: the health kept from last time does not count
        older = rig.serve(device, {"os": 9}, {"os": 9, "temp": 30.0})  # this session reports no tunnel key
        assert silent.wg() == [] and older.wg() == [] and [f["type"] for f in older.sent] == ["challenge", "welcome"]
    rig.registry.enroll_lighting("light-C-01", "C-01", crypto.public_hex(key := crypto.new_key()))
    rig.keys["light-C-01"], rig.tunnel["light-C-01"] = key, wg.new_keypair()[1]
    lighting = rig.serve("light-C-01", rig.health("light-C-01"), rig.health("light-C-01"))
    assert lighting.wg() == [] and "wg_public" not in rig.on_disk("light-C-01")  # a lighting cabinet has no tunnel
    assert {r["id"]: r for r in rig.hub.status()}["light-C-01"]["wg"] is None


def test_a_district_below_the_version_floor_still_gets_its_list(pki, tmp_path):
    store = ReleaseStore(tmp_path / "releases")
    rig = Rig(pki, releases=store)
    store.set_floor(5)
    rig.serve("edge-J-01", rig.health("edge-J-01", os_version=3))
    district = rig.serve("district-1", rig.health("district-1", os_version=3))
    assert rig.hub.floor_verdict("district-1") == (5, 3)  # it gets no commands
    assert sorted(peers_of(district.wg()[0], rig)) == ["edge-J-01"]  # but its peer list goes: a revocation must reach it
    assert rig.events("command-refused-below-floor") == []


# ---------------------------------------------------------------- the Devices rows

def test_the_devices_rows_show_the_tunnel_of_units_and_districts(rig):
    rows = {r["id"]: r for r in rig.hub.status()}
    assert set(rows["edge-J-01"]["wg"]) == UNIT_ROW and set(rows["district-1"]["wg"]) == DISTRICT_ROW
    assert rows["edge-J-01"]["wg"] == {"address": None, "fingerprint": None, "pending": None, "state": None,
                                       "proven": None, "handshake_age": None, "applied": None, "epoch": None,
                                       "district": "district-1", "reason": None, "behind": False,
                                       "problem": "district has no tunnel key yet", "ignored_keys": 0}
    assert rows["district-1"]["wg"] == {"index": None, "address": None, "fingerprint": None, "pending": None,
                                        "mode": "allow", "state": None, "peers": None, "peers_up": None,
                                        "unproven": ["edge-J-01", "edge-J-02"], "applied": None, "epoch": None,
                                        "reason": None, "behind": False, "ignored_keys": 0}
    assert rows["edge-J-03"]["wg"]["problem"] == "no district"
    rig.serve("district-1", rig.health("district-1", state="up", peers=0, peers_up=0, mode="allow"))
    assert {r["id"]: r for r in rig.hub.status()}["edge-J-01"]["wg"]["problem"] == "no tunnel key yet"

    async def look():
        rig.now = WALL + 100
        return {r["id"]: r for r in rig.hub.status()}
    unit = Session(rig, "edge-J-01", [rig.health("edge-J-01", state="up", proven=True, handshake=WALL + 88,
                                                reason="older version")], stay=True)
    rows = held_open(rig, unit, look)
    row = rows["edge-J-01"]["wg"]
    version = unit.wg()[0]["version"]
    assert row == {"address": "10.77.1.2", "fingerprint": wg.fingerprint(rig.tunnel["edge-J-01"]), "pending": None,
                   "state": "up", "proven": True, "handshake_age": 12, "applied": 0, "epoch": 0, "district": "district-1",
                   "reason": "older version", "behind": True, "problem": None, "ignored_keys": 0}  # 0 < its version
    assert rows["district-1"]["wg"]["unproven"] == ["edge-J-02"]  # J-01 showed a proven tunnel in this session
    unit = Session(rig, "edge-J-01", [rig.health("edge-J-01", state="up", proven=True, applied=version, epoch=1)],
                   stay=True)
    rows = held_open(rig, unit, look)
    assert rows["edge-J-01"]["wg"]["behind"] is False and rows["edge-J-01"]["wg"]["epoch"] == 1
    district = rows["district-1"]["wg"]
    assert (district["index"], district["address"], district["fingerprint"], district["state"]) == (
        1, "10.77.1.1", wg.fingerprint(rig.tunnel["district-1"]), "up")
    assert district["behind"] is False  # offline: nothing is behind
    rig.registry.set_status("district-1", "revoked")
    assert {r["id"]: r for r in rig.hub.status()}["edge-J-01"]["wg"]["problem"] == "district not active"
    rig.registry.set_status("edge-J-01", "revoked")
    assert {r["id"]: r for r in rig.hub.status()}["edge-J-01"]["wg"]["problem"] == "revoked"


def test_a_district_that_has_not_applied_its_list_is_behind_and_the_platform_brief_carries_the_tunnel(rig):
    rig.serve("edge-J-01", rig.health("edge-J-01"))

    async def look():
        return {r["id"]: r for r in rig.hub.status()}
    district = Session(rig, "district-1", [rig.health("district-1")], stay=True)
    rows = held_open(rig, district, look)
    assert rows["district-1"]["wg"]["behind"] is True and rows["district-1"]["wg"]["index"] == 1
    platform = Platform([71.78, 40.38], 1000, [])
    platform.devices(list(rows.values()), 10)
    assert platform.dev["J-01"]["wg"]["address"] == "10.77.1.2" and platform.dev["district-1"]["wg"]["behind"] is True
    assert platform.dev["J-01"]["wg"] == rows["edge-J-01"]["wg"] and "wg" in platform.dev["district-2"]
    assert "wg" not in platform.dev["J-03"]  # straight to the centre and no tunnel key: nothing to show
