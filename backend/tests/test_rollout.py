"""Releases by architecture and the staged rollout (step 12, G1). A release package says which system it is for (RAUC's
compatible, read from the signed package by `release add`; anything but urban-edge-x86_64 and urban-edge-aarch64 is
refused), each system has its own numbering, and every device reports its own compatible in its health (none: x86). The
hub offers a device only the newest release for its own compatible, and only while it is in the release's current wave
(named devices or a share of the fleet by device id), which the admin widens with `promote`. A failure or a rollback
reported by a device of the wave halts the release by itself: no new offers (of any package of that version), audited
as rollout-halted with the device and the reason, shown in GET /api/releases; the admin resumes or withdraws it. The
version floor and the earlier release rules still hold. (test_release.py has the rules from before step 12.)"""
import asyncio
import json
import re
import types

import pytest
from fastapi import WebSocketDisconnect

from urban import release
from urban.devices import crypto
from urban.devices.health import clean
from urban.devices.hub import REOFFER, DeviceHub
from urban.devices.registry import Registry, provision
from urban.edge.update import Updater, os_compatible
from urban.release import ReleaseError, ReleaseStore, in_wave, wave_rank

X86, ARM = "urban-edge-x86_64", "urban-edge-aarch64"
JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"},
             {"id": "J-03", "tls": "T3", "name": "Third"}]
DEVICES = ("edge-J-01", "edge-J-02", "edge-J-03", "district-1")
WALL = {"now": 1_800_000_000}
CHECK_BUNDLE = release.check_bundle  # the real one: stand_in_rauc gives it a stand-in RAUC


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path / "pki")
    return tmp_path / "pki"


@pytest.fixture
def store(tmp_path):
    return ReleaseStore(tmp_path / "releases")


def package(tmp_path, name, data=None):
    path = tmp_path / name
    path.write_bytes(data if data is not None else name.encode() * 10)
    return path


def stand_in_rauc(monkeypatch, compatible):
    """`add` asks RAUC about the package (release.check_bundle): a RAUC that finds it signed, of the version asked
    for, made for `compatible`."""
    real = CHECK_BUNDLE

    def check(path, version):
        said = f"RAUC_MF_COMPATIBLE='{compatible}'\nRAUC_MF_VERSION='{version}'\n"
        return real(path, version, which=lambda name: "/usr/bin/rauc",
                    run=lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=said, stderr=""))
    monkeypatch.setattr(release, "check_bundle", check)


class Recorder:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        pass


class Rig:
    """A hub with three junction units and a district server, every one with a session (a recording socket). report()
    is one report in the device's current session as serve() handles it: heard, watched for the rollout, offered."""
    def __init__(self, pki, store):
        registry = Registry(pki / "devices.json")
        registry.enroll_district("district-1", "10.1.0.2", crypto.public_hex(crypto.new_key()))
        self.audited = []
        self.hub = DeviceHub(registry, crypto.load_private((pki / "gateway.key").read_text()),
                             on_report=lambda rec, rep: None, wall=lambda: WALL["now"], releases=store,
                             audit=lambda event, target, note="", actor="platform":
                                 self.audited.append((event, target, note, actor)))
        self.sockets = {}
        for device in DEVICES:
            self.sockets[device] = self.hub.sessions[device] = Recorder()
            self.hub.connects[device] = 1

    def report(self, device, **health):
        self.hub.heard(device, health)
        self.hub._watch_rollout(device)
        asyncio.run(self.hub._offer(device, self.hub.registry.devices[device]))

    def new_session(self, device):
        """The device connected again (serve(): a new session, the offer pause reset)."""
        self.hub.connects[device] += 1
        self.hub._offered.pop(device, None)

    def offers(self, device):
        return [f["body"]["update"]["version"] for f in self.sockets[device].sent if f["type"] == "cmd"]

    def tokens(self, device):
        return [f["body"]["update"]["path"].rsplit("/", 1)[1] for f in self.sockets[device].sent if f["type"] == "cmd"]

    def events(self, name):
        return [a for a in self.audited if a[0] == name]

    def later(self, seconds=REOFFER + 1):
        WALL["now"] += seconds


@pytest.fixture
def rig(pki, store):
    WALL["now"] = 1_800_000_000
    return Rig(pki, store)


# ---------------------------------------------------------------- the package says which system it is for

def test_add_reads_the_compatible_from_the_signed_package_and_refuses_one_that_is_not_ours(tmp_path, capsys, monkeypatch):
    folder = tmp_path / "r"
    stand_in_rauc(monkeypatch, ARM)
    release.main(["--dir", str(folder), "add", str(package(tmp_path, "urban-edge-arm64-2.raucb")), "--version", "2"])
    out = capsys.readouterr().out
    assert out.startswith("added release 2 for urban-edge-aarch64: ") and "to every unit and district; rauc verified" in out
    rel = ReleaseStore(folder).get(2, ARM)
    assert rel["compatible"] == ARM and rel["file"] == "urban-aarch64-2.raucb" and (folder / rel["file"]).is_file()
    assert ReleaseStore(folder).get(2) is None  # not an x86 release
    for foreign in ("urban-edge-riscv64", "urban", ""):
        stand_in_rauc(monkeypatch, foreign)
        with pytest.raises(SystemExit) as stop:
            release.main(["--dir", str(folder), "add", str(package(tmp_path, "x.raucb")), "--version", "3"])
        assert str(stop.value).startswith(f"refused: the bundle is for {foreign!r}, not for a system URBAN builds")
    assert [(r["version"], r["compatible"]) for r in ReleaseStore(folder).releases()] == [(2, ARM)]
    assert sorted(p.name for p in folder.iterdir()) == ["releases.json", "urban-aarch64-2.raucb"]  # nothing copied
    with pytest.raises(ReleaseError, match="not for a system URBAN builds"):  # the store refuses it too
        ReleaseStore(folder).add(package(tmp_path, "y.raucb"), 4, compatible="urban-edge-riscv64")


def test_each_system_has_its_own_numbering_and_list_shows_the_compatible(store, tmp_path, capsys):
    store.add(package(tmp_path, "x2"), 2)
    store.add(package(tmp_path, "a2"), 2, compatible=ARM)  # both may be "2"
    store.add(package(tmp_path, "x3"), 3)
    with pytest.raises(ReleaseError, match="not newer than release 2 for urban-edge-aarch64"):
        store.add(package(tmp_path, "a2b"), 2, compatible=ARM)
    with pytest.raises(ReleaseError, match="not newer than release 3 for urban-edge-x86_64"):
        store.add(package(tmp_path, "x3b"), 3)
    store.add(package(tmp_path, "a3"), 3, compatible=ARM)  # x86 3 does not hold arm64 3 back, nor the other way
    assert store.get(2)["file"] == "urban-2.raucb" and store.get(2, ARM)["file"] == "urban-aarch64-2.raucb"
    assert (store.dir / "urban-2.raucb").read_bytes() != (store.dir / "urban-aarch64-2.raucb").read_bytes()
    release.main(["--dir", str(store.dir), "list"])
    lines = capsys.readouterr().out.splitlines()
    assert [l.split()[:3] for l in lines[:4]] == [["2", "offered", ARM], ["2", "offered", X86],
                                                  ["3", "offered", ARM], ["3", "offered", X86]]
    # withdraw: one system's package with --arch, every package of the number without it
    release.main(["--dir", str(store.dir), "withdraw", "3", "--arch", "aarch64"])
    assert capsys.readouterr().out == "withdrawn release 3 for urban-edge-aarch64: it is no longer offered or served\n"
    assert store.get(3, ARM)["withdrawn"] and not store.get(3)["withdrawn"]
    release.main(["--dir", str(store.dir), "withdraw", "2"])
    assert capsys.readouterr().out.splitlines() == [
        "withdrawn release 2 for urban-edge-x86_64: it is no longer offered or served",
        "withdrawn release 2 for urban-edge-aarch64: it is no longer offered or served"]
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", str(store.dir), "withdraw", "3", "--arch", "mips"])
    assert str(stop.value) == "refused: --arch is x86_64 or aarch64, not 'mips'"


def test_an_entry_written_before_step_12_is_an_x86_release_for_every_device(store, tmp_path):
    store.dir.mkdir(parents=True)
    (store.dir / "urban-2.raucb").write_bytes(b"old")
    store.index.write_text(json.dumps([{"version": 2, "file": "urban-2.raucb", "sha256": "0" * 64, "size": 3,
                                        "devices": None, "added": "2026-10-19T10:00:00Z", "withdrawn": False}]))
    assert store.newest_for("edge-J-01")["version"] == 2 and store.newest_for("edge-J-01", X86)["version"] == 2
    assert store.newest_for("edge-J-01", ARM) is None
    assert store.get(2) is not None and store.get(2, ARM) is None


# ---------------------------------------------------------------- the device reports its compatible

def test_the_device_reports_its_compatible_from_raucs_system_conf_and_the_centre_keeps_it(tmp_path, monkeypatch):
    conf = tmp_path / "system.conf"
    monkeypatch.setenv("URBAN_RAUC_CONF", str(conf))
    assert os_compatible() is None  # no file: nothing reported, the centre takes it for x86
    for text, want in (("[system]\ncompatible=urban-edge-aarch64\nbootloader=grub\n", ARM),
                       ("# x\n[system]\n compatible = urban-edge-x86_64 \n", X86),
                       ("[keyring]\ncompatible=urban-edge-aarch64\n[system]\nbootloader=grub\n", None),  # not [system]
                       ("[system]\ncompatible=<b>x</b>\n", None), ("junk", None)):
        conf.write_text(text)
        assert os_compatible() == want, text
    conf.write_text("[system]\ncompatible=urban-edge-aarch64\n")
    up = Updater(lambda: None, version=2, status=tmp_path / "update.json")
    assert up.health() == {"os": 2, "compatible": ARM}  # next to the fields it reported before
    assert Updater(lambda: None, version=2, compatible=None, status=tmp_path / "update.json").health() == {"os": 2}
    assert clean(up.health()) == up.health()
    for junk in ("<b>x</b>", 7, "x" * 65, "URBAN-EDGE", "", None):
        assert "compatible" not in clean({"compatible": junk})


def test_a_device_is_offered_only_the_newest_release_for_its_own_compatible(rig, store, tmp_path):
    store.add(package(tmp_path, "x2"), 2)
    store.add(package(tmp_path, "x3"), 3)
    store.add(package(tmp_path, "a2"), 2, compatible=ARM)
    rig.report("edge-J-01", os=1, compatible=X86)
    rig.report("edge-J-02", os=1, compatible=ARM)
    rig.report("district-1", os=1)  # reports no compatible: a system before step 12, x86
    rig.report("edge-J-03", os=1, compatible="urban-edge-riscv64")  # a system no release is for: nothing
    assert {d: rig.offers(d) for d in DEVICES} == {"edge-J-01": [3], "edge-J-02": [2], "edge-J-03": [], "district-1": [3]}
    assert rig.hub.update_file(rig.tokens("edge-J-01")[0])[1] == "urban-3.raucb"
    assert rig.hub.update_file(rig.tokens("edge-J-02")[0])[1] == "urban-aarch64-2.raucb"  # its own package, never x86
    sent = rig.sockets["edge-J-02"].sent[0]["body"]["update"]
    assert sent["sha256"] == store.get(2, ARM)["sha256"] and sent["size"] == store.get(2, ARM)["size"]
    assert [(t, n) for _, t, n, _ in rig.events("update-offered")] == [
        ("edge-J-01", "release 3 for urban-edge-x86_64"), ("edge-J-02", "release 2 for urban-edge-aarch64"),
        ("district-1", "release 3 for urban-edge-x86_64")]
    rig.later()
    rig.report("edge-J-02", os=1, compatible=ARM)  # offered again after the pause, audited once
    assert rig.offers("edge-J-02") == [2, 2] and len(rig.events("update-offered")) == 3
    rows = {r["id"]: r for r in rig.hub.status()}
    assert (rows["edge-J-01"]["compatible"], rows["edge-J-02"]["compatible"], rows["district-1"]["compatible"]) == (X86, ARM, X86)


# ---------------------------------------------------------------- waves

def test_the_rank_of_a_device_is_stable_and_a_share_is_about_that_share_of_any_fleet():
    ids = [f"edge-J-{n:03}" for n in range(2000)]
    ranks = [wave_rank(d) for d in ids]
    assert ranks == [wave_rank(d) for d in ids] and min(ranks) == 0 and max(ranks) == 99
    share = {"version": 2, "devices": None, "wave": 10}
    inside = [d for d in ids if in_wave(share, d)]
    assert 140 <= len(inside) <= 260  # 10 % of 2000, give or take
    wider = {**share, "wave": 30}
    assert set(inside) <= {d for d in ids if in_wave(wider, d)}  # widening never drops a device
    assert all(in_wave({**share, "wave": 100}, d) for d in ids)
    assert all(in_wave({"devices": None, "wave": None}, d) for d in ids)  # neither: every device
    named = {"devices": ["edge-J-001"], "wave": 10}
    assert in_wave(named, "edge-J-001") and {d for d in ids if in_wave(named, d)} == set(inside) | {"edge-J-001"}


def test_only_the_devices_of_the_current_wave_are_offered_and_promotion_widens_it(rig, store, tmp_path, capsys):
    store.add(package(tmp_path, "x2"), 2)  # to every device
    ranked = sorted(DEVICES, key=wave_rank)
    first = wave_rank(ranked[0]) + 1  # a share that holds the lowest-ranked device only
    assert wave_rank(ranked[1]) >= first, "the test needs devices of different ranks"
    store.add(package(tmp_path, "x3"), 3, wave=first)
    for d in DEVICES:
        rig.report(d, os=1, compatible=X86)
    assert {d: rig.offers(d) for d in DEVICES} == {d: [3] if d == ranked[0] else [2] for d in DEVICES}  # the rest: 2
    for d in DEVICES:
        rig.report(d, os=2, compatible=X86)  # they installed what they were offered
    release.main(["--dir", str(store.dir), "promote", "3", "--wave", str(wave_rank(ranked[1]) + 1)])
    assert capsys.readouterr().out == f"promoted release 3 for urban-edge-x86_64: now to {wave_rank(ranked[1]) + 1}% of " \
                                      f"the fleet by device id\n"
    rig.later()
    for d in DEVICES:
        rig.report(d, os=2 if d != ranked[0] else 3, compatible=X86)
    assert rig.offers(ranked[1]) == [2, 3] and rig.offers(ranked[2]) == [2] and rig.offers(ranked[0]) == [3]
    with pytest.raises(SystemExit) as stop:  # a wave is only ever widened
        release.main(["--dir", str(store.dir), "promote", "3", "--wave", str(first)])
    assert "a wider wave is more than that" in str(stop.value)
    release.main(["--dir", str(store.dir), "promote", "3", "--all"])
    assert capsys.readouterr().out == "promoted release 3 for urban-edge-x86_64: now to every unit and district\n"
    rig.later()
    for d in ranked[2:]:
        rig.report(d, os=2, compatible=X86)
        assert rig.offers(d) == [2, 3]
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", str(store.dir), "promote", "3", "--all"])
    assert str(stop.value) == "refused: release 3 for urban-edge-x86_64 already goes to every unit and district"


def test_a_first_wave_of_named_devices_and_the_cli_words_of_a_wave(rig, store, tmp_path, capsys, monkeypatch):
    stand_in_rauc(monkeypatch, X86)
    folder = str(store.dir)
    release.main(["--dir", folder, "add", str(package(tmp_path, "x3")), "--version", "3", "--devices", "edge-J-02"])
    assert "to a first wave: edge-J-02; rauc verified" in capsys.readouterr().out
    for d in DEVICES:
        rig.report(d, os=2, compatible=X86)
    assert {d: rig.offers(d) for d in DEVICES} == {d: [3] if d == "edge-J-02" else [] for d in DEVICES}
    token = rig.tokens("edge-J-02")[0]
    rig.hub.links[token]["device"] = "edge-J-01"  # a link bound to a device outside the wave serves nothing
    assert rig.hub.update_file(token) is None
    release.main(["--dir", folder, "add", str(package(tmp_path, "x4")), "--version", "4", "--wave", "25"])
    assert "to a first wave: 25% of the fleet by device id; rauc verified" in capsys.readouterr().out
    release.main(["--dir", folder, "list"])
    listing = capsys.readouterr().out
    assert re.search(r"^\s*3 offered\s+urban-edge-x86_64 .* to edge-J-02$", listing, re.M)
    assert re.search(r"^\s*4 offered\s+urban-edge-x86_64 .* to 25% of the fleet by device id$", listing, re.M)
    for bad in (["--devices", "edge-J-01", "--wave", "10"], ["--wave", "0"], ["--wave", "101"]):
        with pytest.raises(SystemExit):
            release.main(["--dir", folder, "add", str(package(tmp_path, "x5")), "--version", "5", *bad])
    assert store.get(5) is None


# ---------------------------------------------------------------- a failure or a rollback halts the rollout

def test_a_refusal_by_a_device_of_the_wave_halts_every_package_of_that_version(rig, store, tmp_path, capsys):
    store.add(package(tmp_path, "x3"), 3, ["edge-J-01"])
    store.add(package(tmp_path, "a3"), 3, ["edge-J-01"], compatible=ARM)
    rig.report("edge-J-01", os=2, compatible=X86)
    token = rig.tokens("edge-J-01")[0]
    rig.report("edge-J-01", os=2, compatible=X86, update={"state": "refused", "version": 3, "reason": "install failed"})
    halted = store.get(3)["halted"]
    assert halted["device"] == "edge-J-01" and halted["compatible"] == X86
    assert halted["reason"] == "refused by the device: install failed" and store.get(3, ARM)["halted"] == halted
    assert rig.events("rollout-halted") == [("rollout-halted", "release-3",
                                             "edge-J-01 (urban-edge-x86_64): refused by the device: install failed", "platform")]
    assert rig.hub.update_file(token) is None  # what was offered is not served any more
    release.main(["--dir", str(store.dir), "promote", "3", "--all"])  # even a wider wave gets nothing while halted
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "promoted release 3 for urban-edge-x86_64: now to every unit and district"
    assert out[1].startswith("halted: release 3 was halted by edge-J-01 (urban-edge-x86_64): refused by the device: "
                             "install failed, at ") and out[1].endswith("nothing of it is offered until `release resume 3`")
    rig.later()
    for d, compatible in (("edge-J-02", ARM), ("edge-J-03", X86), ("district-1", X86), ("edge-J-01", X86)):
        rig.report(d, os=2, compatible=compatible)
    assert {d: rig.offers(d) for d in DEVICES} == {"edge-J-01": [3], "edge-J-02": [], "edge-J-03": [], "district-1": []}
    rig.report("edge-J-01", os=2, compatible=X86, update={"state": "refused", "version": 3, "reason": "install failed"})
    assert len(rig.events("rollout-halted")) == 1  # audited once
    # Devices: the halt with the device and the reason
    view = {(r["version"], r["compatible"]): r for r in rig.hub.releases_view()}
    assert view[(3, ARM)]["halted"]["reason"] == "refused by the device: install failed"
    assert view[(3, ARM)]["in_wave"] == ["edge-J-02"] and view[(3, X86)]["halted"]["device"] == "edge-J-01"
    release.main(["--dir", str(store.dir), "list"])
    listing = capsys.readouterr().out
    assert re.search(r"^\s*3 HALTED\s+urban-edge-aarch64 ", listing, re.M)
    assert "      halted by edge-J-01 (urban-edge-x86_64): refused by the device: install failed, at " in listing


@pytest.mark.parametrize("state,reason", [("refused", "restart pending"),
                                          ("failed", "download failed: ConnectionResetError [Errno 104]"),
                                          ("failed", "no path to the centre yet (waiting for the tunnel status)"),
                                          ("failed", "the package is shorter than offered (5 of 9 bytes)"),
                                          ("failed", "the centre answered 404"), ("failed", "not enough free space for the package"),
                                          ("downloading", ""), ("installed", "restarting into the new system")])
def test_what_is_retried_or_on_its_way_does_not_halt(rig, store, tmp_path, state, reason):
    store.add(package(tmp_path, "x3"), 3)
    rig.report("edge-J-01", os=2, compatible=X86, update={"state": state, "version": 3, "reason": reason})
    assert store.get(3)["halted"] is None and not rig.events("rollout-halted")


def test_a_package_that_is_not_the_one_offered_halts_but_not_outside_the_wave_or_for_another_release(rig, store, tmp_path):
    store.add(package(tmp_path, "x3"), 3, ["edge-J-02"])
    failed = {"state": "failed", "version": 3, "reason": "the package's hash differs from the offer"}
    rig.report("edge-J-01", os=2, compatible=X86, update=failed)  # not in the wave: not its release's failure
    rig.report("edge-J-02", os=2, compatible=X86, update={**failed, "version": 7})  # no release 7
    rig.report("edge-J-02", os=2, compatible=ARM, update=failed)  # no arm64 release 3
    assert store.get(3)["halted"] is None
    rig.report("edge-J-02", os=2, compatible=X86, update=failed)
    assert store.get(3)["halted"]["reason"] == "download failed: the package's hash differs from the offer"
    store.withdraw(3)
    store.add(package(tmp_path, "x4"), 4, ["edge-J-02"])
    store.withdraw(4)
    rig.report("edge-J-02", os=2, compatible=X86, update={**failed, "version": 4})  # withdrawn: nothing to halt
    assert store.get(4)["halted"] is None and len(rig.events("rollout-halted")) == 1


class Session:
    """A device's websocket as serve() sees it: the hello signed with the unit's key, then its reports, then gone."""
    def __init__(self, pki, device, reports):
        self.key = crypto.load_private((pki / "edge" / f"{device}.key").read_text())
        self.device, self.sent, self.reports, self.step = device, [], list(reports), 0

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def receive_text(self):
        self.step += 1
        if self.step == 1:
            return json.dumps({"type": "hello", "device": self.device, "version": "0.1.0", "epoch": 1,
                               "sig": crypto.sign(self.key, {"device": self.device, "nonce": self.sent[0]["nonce"]})})
        if self.reports:
            return json.dumps({"type": "report", "t": 1, "queues": {}, "events": [], "health": self.reports.pop(0)})
        raise WebSocketDisconnect()

    async def close(self, code=1000):
        pass

    def offers(self):
        return [f["body"]["update"]["version"] for f in self.sent if f["type"] == "cmd" and "update" in f["body"]]


def serve(rig, pki, device, *reports):
    rig.hub.sessions.pop(device, None)
    ws = Session(pki, device, reports)
    asyncio.run(rig.hub.serve(ws))
    return ws


def test_a_rollback_halts_the_release_and_the_device_is_not_offered_it_again(rig, store, pki, tmp_path):
    store.add(package(tmp_path, "x3"), 3, ["edge-J-01"])
    run = {"os": 2, "compatible": X86}
    first = serve(rig, pki, "edge-J-01", run, {**run, "update": {"state": "installing", "version": 3, "reason": ""}},
                  {**run, "update": {"state": "installed", "version": 3, "reason": "restarting into the new system"}})
    assert first.offers() == [3]
    # the new slot failed its self-check and the machine went back: version 2 again, no update state (/run is new)
    second = serve(rig, pki, "edge-J-01", run, run)
    assert second.offers() == []  # not even at session start, before its first report told what it runs
    assert store.get(3)["halted"]["reason"] == "rolled back: installed 3, runs 2"
    assert rig.events("rollout-halted")[0][1:3] == ("release-3", "edge-J-01 (urban-edge-x86_64): rolled back: installed 3, runs 2")
    rig.later()
    assert serve(rig, pki, "edge-J-01", run).offers() == []


def test_a_new_system_that_came_up_and_then_fell_back_is_a_rollback_too(rig, store, pki, tmp_path):
    store.add(package(tmp_path, "a3"), 3, compatible=ARM)
    run = {"os": 2, "compatible": ARM}
    serve(rig, pki, "edge-J-02", run, {**run, "update": {"state": "installed", "version": 3, "reason": ""}})
    serve(rig, pki, "edge-J-02", {"os": 3, "compatible": ARM})  # it came up on 3 ...
    assert store.get(3, ARM)["halted"] is None
    serve(rig, pki, "edge-J-02", run)  # ... and fell back to 2
    assert store.get(3, ARM)["halted"] == {**store.get(3, ARM)["halted"], "device": "edge-J-02", "compatible": ARM,
                                           "reason": "rolled back: installed 3, runs 2"}


def test_a_device_that_reconnects_while_installing_or_runs_the_release_is_not_a_rollback(rig, store, pki, tmp_path):
    store.add(package(tmp_path, "x3"), 3)
    run = {"os": 2, "compatible": X86}
    serve(rig, pki, "edge-J-01", run, {**run, "update": {"state": "installing", "version": 3, "reason": ""}})
    serve(rig, pki, "edge-J-01", {**run, "update": {"state": "installing", "version": 3, "reason": ""}})  # link blip
    serve(rig, pki, "edge-J-01", {"os": 3, "compatible": X86})
    serve(rig, pki, "edge-J-01", {"os": 3, "compatible": X86})
    assert store.get(3)["halted"] is None and not rig.events("rollout-halted")


def test_a_package_added_to_a_halted_version_starts_halted_and_resume_and_withdraw_are_the_admins(rig, store, tmp_path,
                                                                                                capsys, monkeypatch):
    store.add(package(tmp_path, "x3"), 3, ["edge-J-01"])
    refused = {"state": "refused", "version": 3, "reason": "signature"}
    rig.report("edge-J-01", os=2, compatible=X86, update=refused)
    stand_in_rauc(monkeypatch, ARM)
    release.main(["--dir", str(store.dir), "add", str(package(tmp_path, "a3")), "--version", "3"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("added release 3 for urban-edge-aarch64: ") and len(out) == 2
    assert out[1].startswith("halted: release 3 was halted by edge-J-01 (urban-edge-x86_64): refused by the device: signature")
    rig.report("edge-J-02", os=2, compatible=ARM)
    assert rig.offers("edge-J-02") == []  # the arm64 package of a halted version is not offered
    release.main(["--dir", str(store.dir), "resume", "3"])
    assert capsys.readouterr().out.startswith("resumed release 3: offered again to its wave (it was halted by edge-J-01 "
                                              "(urban-edge-x86_64): refused by the device: signature, at ")
    rig.report("edge-J-01", os=2, compatible=X86, update=refused)  # the same old report does not halt it again
    rig.report("edge-J-02", os=2, compatible=ARM)
    assert store.get(3)["halted"] is None and rig.offers("edge-J-02") == [3]
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", str(store.dir), "resume", "3"])
    assert str(stop.value) == "refused: release 3 is not halted"
    rig.report("edge-J-02", os=2, compatible=ARM, update={"state": "refused", "version": 3, "reason": "install failed"})
    assert store.get(3, ARM)["halted"]["device"] == "edge-J-02" and len(rig.events("rollout-halted")) == 2
    release.main(["--dir", str(store.dir), "withdraw", "3"])  # a halted release withdrawn: gone for good
    assert len(capsys.readouterr().out.splitlines()) == 2
    assert store.get(3)["withdrawn"] and store.get(3, ARM)["withdrawn"]
    with pytest.raises(SystemExit) as stop:
        release.main(["--dir", str(store.dir), "resume", "3"])
    assert str(stop.value) == "refused: release 3 is already withdrawn"


# ---------------------------------------------------------------- what held before still holds

def test_the_version_floor_and_the_earlier_rules_hold_with_two_systems(rig, store, tmp_path):
    store.add(package(tmp_path, "a3"), 3, compatible=ARM)
    store.set_floor(3, by="test")
    rig.report("edge-J-02", os=2, compatible=ARM)
    assert rig.hub.floor_verdict("edge-J-02") == (3, 2) and rig.offers("edge-J-02") == [3]  # offers bypass the floor
    rig.report("edge-J-02", os=2, compatible=ARM)
    assert rig.offers("edge-J-02") == [3]  # within REOFFER: once
    rig.report("edge-J-02", os=3, compatible=ARM)
    assert rig.hub.floor_verdict("edge-J-02") is None
    store.withdraw(3, ARM)
    rig.later()
    rig.report("edge-J-03", os=2, compatible=ARM)
    assert rig.offers("edge-J-03") == []


def test_devices_shows_the_releases_and_their_rollout(rig, store, tmp_path):
    store.add(package(tmp_path, "x2"), 2)
    store.add(package(tmp_path, "a2"), 2, ["edge-J-02"], compatible=ARM)
    rig.report("edge-J-01", os=2, compatible=X86)
    rig.report("edge-J-02", os=1, compatible=ARM)
    rig.report("edge-J-02", os=1, compatible=ARM, update={"state": "refused", "version": 2, "reason": "hash"})
    route = next(r for r in rig.hub.release_routes(lambda: "viewer").routes if r.path == "/api/releases")
    rows = asyncio.run(route.endpoint(who="viewer"))
    assert [(r["version"], r["compatible"]) for r in rows] == [(2, ARM), (2, X86)]
    arm, x86 = rows
    assert arm["devices"] == ["edge-J-02"] and arm["in_wave"] == ["edge-J-02"] and arm["running"] == []
    assert arm["halted"]["device"] == "edge-J-02" and arm["halted"]["reason"] == "refused by the device: hash"
    assert x86["halted"] == arm["halted"]  # one version number, one halt
    assert x86["in_wave"] == ["district-1", "edge-J-01", "edge-J-03"] and x86["running"] == ["edge-J-01"]
