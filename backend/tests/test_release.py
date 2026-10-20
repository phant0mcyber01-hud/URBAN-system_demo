"""The centre's releases and what the hub does with them: a package is kept with its version, size and hash, offered
in a gateway-signed message only to a targeted unit or district whose reported system version is older, and served
only through a valid link bound to that device. (The device side is in test_update_client.py.)"""
import asyncio
import json
import re
import types

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from urban import release
from urban.devices import crypto
from urban.devices.hub import LINK_TTL, REOFFER, DeviceHub
from urban.devices.registry import Registry, provision
from urban.release import ReleaseError, ReleaseStore

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "CAM-02", "tls": "T2", "name": "Second"}]
WALL = {"now": 1_800_000_000}


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path / "pki")
    return tmp_path / "pki"


@pytest.fixture
def store(tmp_path):
    return ReleaseStore(tmp_path / "releases")


def package(tmp_path, name="urban-edge-2.raucb", data=b"not really a bundle, but bytes"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def make_hub(pki, store):
    """A hub with two junction units and a district server; every one of them has a session (a recording socket)."""
    registry = Registry(pki / "devices.json")
    registry.enroll_district("district-1", "10.1.0.2", crypto.public_hex(crypto.new_key()))
    hub = DeviceHub(registry, crypto.load_private((pki / "gateway.key").read_text()), on_report=lambda rec, rep: None,
                    wall=lambda: WALL["now"], releases=store)
    sockets = {}
    for device in ("edge-J-01", "edge-CAM-02", "district-1"):
        sockets[device] = hub.sessions[device] = Recorder()
    return hub, sockets


class Recorder:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def seen(hub, device, os_version):
    hub.seen[device] = {"at": WALL["now"], "health": {} if os_version is None else {"os": os_version}}


def offer(hub, device):
    asyncio.run(hub._offer(device, hub.registry.devices[device]))


# ---------------------------------------------------------------- the store and the CLI

def test_the_store_keeps_a_release_with_its_size_and_hash_and_lists_and_withdraws_it(store, tmp_path):
    import hashlib
    src = package(tmp_path)
    rel = store.add(src, 2)
    assert rel["version"] == 2 and rel["file"] == "urban-2.raucb" and rel["devices"] is None and rel["withdrawn"] is False
    assert rel["size"] == src.stat().st_size and rel["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert (store.dir / "urban-2.raucb").read_bytes() == src.read_bytes()
    assert not list(store.dir.glob("*.tmp"))  # written whole or not at all
    assert json.loads(store.index.read_text())[0]["sha256"] == rel["sha256"]
    assert [r["version"] for r in ReleaseStore(store.dir).releases()] == [2]  # another process sees it
    assert store.newest_for("edge-J-01")["version"] == 2
    store.withdraw(2)
    assert store.newest_for("edge-J-01") is None and store.get(2)["withdrawn"] is True
    assert (store.dir / "urban-2.raucb").exists()  # the file stays for the record
    with pytest.raises(ReleaseError):
        store.withdraw(2)
    with pytest.raises(ReleaseError):
        store.withdraw(9)


def test_the_store_refuses_a_version_that_is_not_newer_and_what_is_not_a_package(store, tmp_path):
    store.add(package(tmp_path), 3)
    with pytest.raises(ReleaseError, match="not newer"):
        store.add(package(tmp_path, data=b"other"), 2)
    with pytest.raises(ReleaseError, match="not newer"):
        store.add(package(tmp_path, data=b"other"), 3)
    with pytest.raises(ReleaseError):
        store.add(tmp_path / "missing.raucb", 4)
    with pytest.raises(ReleaseError):
        store.add(package(tmp_path, "empty.raucb", b""), 4)
    for bad in (0, -1, True, "4"):
        with pytest.raises(ReleaseError):
            store.add(package(tmp_path), bad)
    for devices in ([], ["../x"], ["a b"]):
        with pytest.raises(ReleaseError):
            store.add(package(tmp_path), 4, devices)
    assert [r["version"] for r in store.releases()] == [3]
    # a withdrawn release does not hold the number back: 2 can be released again, replacing the withdrawn entry
    store.withdraw(3)
    store.add(package(tmp_path, data=b"fixed"), 3)
    assert [(r["version"], r["withdrawn"]) for r in store.releases()] == [(3, False)]


def test_a_release_for_listed_devices_is_for_those_devices_only(store, tmp_path):
    store.add(package(tmp_path), 2, ["edge-J-01", "district-1"])
    store.add(package(tmp_path, data=b"3"), 3, ["district-1"])
    assert store.newest_for("edge-J-01")["version"] == 2
    assert store.newest_for("district-1")["version"] == 3
    assert store.newest_for("edge-CAM-02") is None


def test_the_cli_adds_lists_withdraws_and_refuses_with_a_clear_line(tmp_path, capsys):
    folder = str(tmp_path / "r")
    src = package(tmp_path)
    release.main(["--dir", folder, "add", str(src), "--version", "2", "--devices", "edge-J-01,district-1"])
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "added release 2" in out and "edge-J-01, district-1" in out
    release.main(["--dir", folder, "list"])
    assert "offered" in capsys.readouterr().out
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", folder, "add", str(src), "--version", "2"])
    assert str(stop.value).startswith("refused: version 2 is not newer")
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", folder, "add", str(src), "--version", "5", "--devices", ","])
    assert "--devices" in str(stop.value)
    release.main(["--dir", folder, "withdraw", "2"])
    assert "withdrawn release 2" in capsys.readouterr().out
    release.main(["--dir", folder, "list"])
    assert "WITHDRAWN" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        release.main(["--dir", folder, "withdraw", "7"])


def test_rauc_is_asked_about_the_bundle_when_it_is_there(tmp_path):
    src = package(tmp_path)
    assert "not cross-checked" in release.check_bundle(src, 2, which=lambda name: None)
    keyring = tmp_path / "keyring.pem"
    keyring.write_text("x")
    calls = []

    def rauc(version, code=0):
        def run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=code, stdout=f"RAUC_MF_COMPATIBLE='urban'\nRAUC_MF_VERSION='{version}'\n",
                                         stderr="signature check failed" if code else "")
        return run

    ok = release.check_bundle(src, 2, keyring, which=lambda name: "/usr/bin/rauc", run=rauc(2))
    assert "verified" in ok and calls[0][:3] == ["/usr/bin/rauc", "info", "--output-format=shell"]
    assert f"--keyring={keyring}" in calls[0] and calls[0][-1] == str(src)
    with pytest.raises(ReleaseError, match="version"):
        release.check_bundle(src, 2, keyring, which=lambda name: "/usr/bin/rauc", run=rauc(3))
    with pytest.raises(ReleaseError, match="signature"):
        release.check_bundle(src, 2, keyring, which=lambda name: "/usr/bin/rauc", run=rauc(2, code=1))


# ---------------------------------------------------------------- offers

def test_a_signed_offer_goes_to_a_device_on_an_older_system(pki, store, tmp_path):
    rel = store.add(package(tmp_path), 2)
    hub, sockets = make_hub(pki, store)
    seen(hub, "edge-J-01", 1)
    offer(hub, "edge-J-01")
    [frame] = sockets["edge-J-01"].sent
    assert frame["type"] == "cmd" and frame["device"] == "edge-J-01"
    signed = {k: frame[k] for k in ("device", "seq", "t", "body")}
    assert crypto.verify(crypto.load_public((pki / "gateway.pub").read_text()), signed, frame["sig"])
    assert frame["t"] == WALL["now"]
    body = frame["body"]["update"]
    assert set(body) == {"version", "sha256", "size", "path"} and body["version"] == 2
    assert body["sha256"] == rel["sha256"] and body["size"] == rel["size"]
    assert re.fullmatch(r"/update/[0-9a-f]{64}", body["path"])
    link = hub.links[body["path"].rsplit("/", 1)[1]]
    assert link["device"] == "edge-J-01" and link["version"] == 2 and link["until"] == WALL["now"] + LINK_TTL
    # a tampered offer does not verify
    forged = {**signed, "body": {"update": {**body, "sha256": "0" * 64}}}
    assert not crypto.verify(crypto.load_public((pki / "gateway.pub").read_text()), forged, frame["sig"])


def test_no_offer_to_a_device_on_that_version_or_newer_or_of_unknown_version(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    hub, sockets = make_hub(pki, store)
    for device, have in (("edge-J-01", 2), ("edge-CAM-02", 3), ("district-1", None)):
        seen(hub, device, have)
        offer(hub, device)
    hub.seen["edge-J-01"]["health"]["os"] = True  # not an integer
    hub._offered.clear()
    offer(hub, "edge-J-01")
    hub.seen["edge-J-01"]["health"]["os"] = "1"
    offer(hub, "edge-J-01")
    del hub.seen["edge-CAM-02"]  # never reported at all
    offer(hub, "edge-CAM-02")
    assert all(not s.sent for s in sockets.values()) and not hub.links


def test_offers_only_to_targeted_devices_and_only_the_newest_that_targets_them(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    store.add(package(tmp_path, data=b"3"), 3, ["district-1"])
    hub, sockets = make_hub(pki, store)
    for device in sockets:
        seen(hub, device, 1)
        offer(hub, device)
    versions = {d: [f["body"]["update"]["version"] for f in s.sent] for d, s in sockets.items()}
    assert versions == {"edge-J-01": [2], "edge-CAM-02": [2], "district-1": [3]}


def test_nothing_is_offered_after_a_release_is_withdrawn_and_the_link_dies_with_it(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    hub, sockets = make_hub(pki, store)
    seen(hub, "edge-J-01", 1)
    offer(hub, "edge-J-01")
    token = sockets["edge-J-01"].sent[0]["body"]["update"]["path"].rsplit("/", 1)[1]
    assert hub.update_file(token) is not None
    ReleaseStore(store.dir).withdraw(2)  # the CLI, in another process: the hub notices by itself
    assert hub.update_file(token) is None
    WALL["now"] += REOFFER + 1
    try:
        offer(hub, "edge-J-01")
    finally:
        WALL["now"] -= REOFFER + 1
    assert len(sockets["edge-J-01"].sent) == 1
    ReleaseStore(store.dir).add(package(tmp_path, data=b"3"), 3)  # a later release is picked up without a restart
    WALL["now"] += REOFFER + 1
    try:
        offer(hub, "edge-J-01")
    finally:
        WALL["now"] -= REOFFER + 1
    assert sockets["edge-J-01"].sent[1]["body"]["update"]["version"] == 3


def test_the_same_release_is_not_offered_again_within_ten_minutes(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    hub, sockets = make_hub(pki, store)
    seen(hub, "district-1", 1)
    offer(hub, "district-1")
    offer(hub, "district-1")
    assert len(sockets["district-1"].sent) == 1
    WALL["now"] += REOFFER - 1
    try:
        offer(hub, "district-1")
        assert len(sockets["district-1"].sent) == 1
        WALL["now"] += 2
        offer(hub, "district-1")
        assert len(sockets["district-1"].sent) == 2
    finally:
        WALL["now"] = 1_800_000_000
    a, b = (s["body"]["update"]["path"] for s in sockets["district-1"].sent)
    assert a != b and len(hub.links) == 1  # a fresh link replaces the old one


def test_an_offer_is_made_at_session_start_and_on_reports_and_a_new_session_resets_the_pause(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    hub, _ = make_hub(pki, store)
    hub.sessions.clear()
    key = crypto.load_private((pki / "edge" / "edge-J-01.key").read_text())

    class Session:
        def __init__(self, reports):
            self.sent, self.reports, self.step = [], list(reports), 0

        async def accept(self):
            pass

        async def send_text(self, text):
            self.sent.append(json.loads(text))

        async def receive_text(self):
            self.step += 1
            if self.step == 1:
                return json.dumps({"type": "hello", "device": "edge-J-01", "version": "0.1.0", "epoch": 1,
                                   "sig": crypto.sign(key, {"device": "edge-J-01", "nonce": self.sent[0]["nonce"]})})
            if self.reports:
                return json.dumps({"type": "report", "t": 1, "queues": {}, "events": [], "health": self.reports.pop(0)})
            raise WebSocketDisconnect()

        async def close(self, code=1000):
            pass

    def kinds(ws):
        return [f["type"] + (":" + str(f["body"]["update"]["version"]) if f["type"] == "cmd" else "") for f in ws.sent]

    first = Session([{"os": 1}, {"os": 1}])
    asyncio.run(hub.serve(first))
    assert kinds(first) == ["challenge", "welcome", "cmd:2"]  # the first report told its version; the second was inside the pause
    second = Session([])  # it connected again: it may have lost the download, so no pause, and its last os is known
    asyncio.run(hub.serve(second))
    assert kinds(second) == ["challenge", "welcome", "cmd:2"]
    third = Session([{"os": 2}])  # it installed the release
    asyncio.run(hub.serve(third))
    assert kinds(third) == ["challenge", "welcome", "cmd:2"]  # stale os 1 at session start: it ignores that offer itself
    fourth = Session([])
    asyncio.run(hub.serve(fourth))
    assert kinds(fourth) == ["challenge", "welcome"]  # now the hub knows it is on 2


def test_a_device_of_another_kind_and_a_hub_without_releases_offer_nothing(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    hub, sockets = make_hub(pki, store)
    seen(hub, "edge-J-01", 1)
    rec = {**hub.registry.devices["edge-J-01"], "kind": "gateway"}
    asyncio.run(hub._offer("edge-J-01", rec))
    assert not sockets["edge-J-01"].sent
    bare = DeviceHub(hub.registry, hub.key, lambda *a: None)  # a hub without releases offers nothing
    bare.sessions["edge-J-01"] = sockets["edge-J-01"]
    bare.seen["edge-J-01"] = hub.seen["edge-J-01"]
    asyncio.run(bare._offer("edge-J-01", hub.registry.devices["edge-J-01"]))
    assert not sockets["edge-J-01"].sent


# ---------------------------------------------------------------- the download link

def route(hub):
    return next(r for r in hub.update_routes().routes if r.path == "/update/{token}")


def minted(hub, sockets, device="edge-J-01"):
    seen(hub, device, 1)
    hub._offered.clear()
    offer(hub, device)
    return sockets[device].sent[-1]["body"]["update"]["path"].rsplit("/", 1)[1]


def test_a_valid_link_serves_the_package_bytes_and_only_to_its_own_release(pki, store, tmp_path):
    data = bytes(range(256)) * 1000
    store.add(package(tmp_path, data=data), 2)
    hub, sockets = make_hub(pki, store)
    token = minted(hub, sockets)
    reply = route(hub).endpoint(token)
    assert open(reply.path, "rb").read() == data and reply.status_code == 200
    assert reply.media_type == "application/octet-stream"
    for wrong in ("0" * 64, "", token[:-1] + ("0" if token[-1] != "0" else "1"), "../urban-2.raucb"):
        with pytest.raises(HTTPException) as err:
            route(hub).endpoint(wrong)
        assert err.value.status_code == 404


def test_a_link_expires_after_an_hour_and_is_for_one_device_and_one_release(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    hub, sockets = make_hub(pki, store)
    token = minted(hub, sockets)
    other = minted(hub, sockets, "district-1")
    assert token != other and hub.links[token]["device"] == "edge-J-01" and hub.links[other]["device"] == "district-1"
    store.add(package(tmp_path, data=b"3"), 3)
    assert hub.update_file(token)[1] == "urban-2.raucb"  # the link's own release, not the newest
    WALL["now"] += LINK_TTL - 1
    try:
        assert hub.update_file(token) is not None
        WALL["now"] += 1
        assert hub.update_file(token) is None and token not in hub.links
        assert hub.update_file(other) is None
    finally:
        WALL["now"] = 1_800_000_000


def test_a_link_is_refused_when_its_release_no_longer_targets_the_device_or_the_device_is_revoked(pki, store, tmp_path):
    store.add(package(tmp_path), 2)
    hub, sockets = make_hub(pki, store)
    token = minted(hub, sockets)
    assert hub.update_file(token) is not None
    hub.registry.set_status("edge-J-01", "revoked")
    assert hub.update_file(token) is None
    hub.registry.set_status("edge-J-01", "active")
    assert hub.update_file(token) is not None
    (store.dir / "urban-2.raucb").unlink()  # the file went missing: nothing to serve
    assert hub.update_file(token) is None
