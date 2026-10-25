"""Step 14, G1: the pilot machine's own district (ROLE=district,center) is an ordinary district.

At first boot the machine makes its district side a key of its own and hands the enrolment code to the centre on the same
machine (urban-prepare); the centre enrols it by itself, once, through the same Registry.enroll_district as
`urban-center provision enroll-district`, audited as district-enrolled; its kit points at the centre over the loopback;
its reports are accepted only over its signed channel and give the centre only the queues of junctions routed through
it. A separate district, a unit and a centre without the district role do not change (urban/node/pilot.py)."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from urban.devices import crypto
from urban.devices.enrolment import install_kit, make_code, make_kit, parse_code
from urban.devices.hub import DeviceHub
from urban.devices.registry import Registry, provision
from urban.node import pilot
from urban.platform import Platform
from urban.sources import clock

ROOT = Path(__file__).resolve().parents[2]
ROOTFS = ROOT / "os" / "rootfs"
OWN = "district-center"
JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"}]


def cam(jid, tls, *approaches):
    return {"id": jid, "tls": tls, "camera": False, "lon": 71.78, "lat": 40.38, "name": jid,
            "approaches": [{"id": a, "name": a, "zone": 100.0} for a in approaches],
            "phases": [{"index": i, "duration": 40, "min_green": 10, "edges": [a], "streets": [a]}
                       for i, a in enumerate(approaches)],
            "plans": [{"id": 0, "splits": {str(i): 40 for i in range(len(approaches))}}]}


class Audit:
    def __init__(self):
        self.records = []

    def __call__(self, event, target, note="", actor="platform"):
        self.records.append((event, target, note, actor))

    def of(self, event):
        return [r for r in self.records if r[0] == event]


@pytest.fixture
def pki(tmp_path):
    """The centre's keys and certificate, as its provisioning makes them in field mode."""
    folder = tmp_path / "platform" / "pki"
    provision(JUNCTIONS, folder, host="urban-center", demo_devices=False)
    return folder


@pytest.fixture
def machine(tmp_path):
    """The pilot's district side: its key (an Ed25519 file here; a TPM key on a machine with a TPM) and the code
    urban-prepare hands to the centre in /run/urban/own-district.code."""
    key = crypto.new_key()
    d_pki = tmp_path / "etc" / "pki"
    (d_pki / "edge").mkdir(parents=True)
    (d_pki / "edge" / f"{OWN}.key").write_text(crypto.private_hex(key))
    code = tmp_path / "own-district.code"
    code.write_text(make_code(OWN, "PILOT-1", crypto.public_hex(key)) + "\n")
    return SimpleNamespace(key=key, public=crypto.public_hex(key), pki=d_pki, code=code,
                           marker=tmp_path / "platform" / "own-district.json")


def hub_of(pki, on_report=lambda rec, report: None):
    return DeviceHub(Registry(pki / "devices.json"), crypto.load_private((pki / "gateway.key").read_text()),
                     on_report=on_report)


def enrol(pki, machine, hub=None, audit=None, name="urban-center", port=8443):
    hub = hub or hub_of(pki)
    audit = audit if audit is not None else Audit()
    said = asyncio.run(pilot.enroll_own(hub, audit, machine.code, name, port, machine.marker))
    return hub, audit, said


# ---------------------------------------------------------------- enrolled once, by itself, like any district

def test_the_centre_enrols_its_own_district_by_itself_as_an_ordinary_district(pki, machine, tmp_path):
    hub, audit, said = enrol(pki, machine)
    fp = crypto.fingerprint(machine.public)
    assert said == f"{OWN} (key {fp}) enrolled at urban-center"
    rec = hub.registry.devices[OWN]
    # the record is the one `urban-center provision enroll-district` makes for the same code and address
    other = tmp_path / "other"
    provision(JUNCTIONS, other, demo_devices=False)
    by_hand = Registry(other / "devices.json").enroll_district(OWN, "urban-center", machine.public, "PILOT-1")
    assert rec == by_hand
    assert rec["kind"] == "district" and rec["status"] == "active" and rec["address"] == "urban-center"
    # in the audit like any district's enrolment (Devices -> Enrol says district-enrolled with the address and key)
    assert audit.records == [("district-enrolled", OWN,
                              f"urban-center key {fp} (this machine's own district, enrolled by itself)", "platform")]
    # in Devices like any district
    row = next(r for r in hub.status() if r["id"] == OWN)
    assert (row["kind"], row["address"], row["fingerprint"], row["status"], row["online"]) == (
        "district", "urban-center", fp, "active", False)
    assert json.loads(machine.marker.read_text())["fingerprint"] == fp and hub.own == {OWN}
    # it authenticates like any district: with its own key, over the centre's challenge
    sig = crypto.sign(machine.key, {"device": OWN, "nonce": "n1"})
    assert hub.registry.authenticate(OWN, "n1", sig)["id"] == OWN


def test_after_a_restart_it_is_the_same_district_never_a_second_enrolment(pki, machine):
    hub, audit, _ = enrol(pki, machine)
    before = (pki / "devices.json").read_bytes()
    for _ in range(2):  # the centre restarts (a new process: a new registry and hub from the same files)
        again, audit2, said = enrol(pki, machine)
        assert audit2.records == [] and (pki / "devices.json").read_bytes() == before
        assert said.startswith(f"{OWN} (key {crypto.fingerprint(machine.public)}), active, enrolled ")
        assert again.own == {OWN}
    # the admin revokes it: a restart does not bring it back
    Registry(pki / "devices.json").set_status(OWN, "revoked")
    again, audit2, said = enrol(pki, machine)
    assert audit2.records == [] and again.registry.devices[OWN]["status"] == "revoked" and ", revoked," in said
    # the admin removes it: a restart does not enrol it a second time
    Registry(pki / "devices.json").remove(OWN)
    again, audit2, said = enrol(pki, machine)
    assert OWN not in again.registry.devices and audit2.records == [] and "not enrolled again" in said
    assert again.own == set()


def test_a_name_held_by_another_key_and_a_code_that_is_not_a_district_are_left_alone(pki, machine, tmp_path):
    other = crypto.public_hex(crypto.new_key())
    Registry(pki / "devices.json").enroll_district(OWN, "10.1.0.9", other)
    before = (pki / "devices.json").read_bytes()
    hub, audit, said = enrol(pki, machine)
    assert said.startswith(f"not enrolled: {OWN} is enrolled with another key")
    assert (pki / "devices.json").read_bytes() == before and audit.records == [] and not machine.marker.exists()
    assert hub.own == set()
    # the machine's key already enrolled under another name: refused by the registry, said, nothing written
    fresh = tmp_path / "fresh"
    provision(JUNCTIONS, fresh, demo_devices=False)
    Registry(fresh / "devices.json").enroll_district("district-9", "10.1.0.9", machine.public)
    hub, audit, said = enrol(fresh, machine)
    assert said.startswith(f"not enrolled: {OWN}: this key is already enrolled as district-9") and audit.records == []
    # a code that is not a district's (or a broken one) is never enrolled
    for text in (make_code("edge-J-01", "X", machine.public), "URBAN1:garbage"):
        machine.code.write_text(text)
        hub, audit, said = enrol(pki, machine)
        assert said.startswith("not enrolled:") and audit.records == []
    assert (pki / "devices.json").read_bytes() == before


def test_no_code_no_own_district_a_plain_centre_a_district_and_a_unit_are_unchanged(pki, machine, tmp_path):
    machine.code.unlink()
    hub, audit, said = enrol(pki, machine)
    assert said is None and audit.records == [] and hub.registry.devices == {} and not machine.marker.exists()
    # urban-prepare: the key of a unit and of a district is still theirs ($UNIT), only the pilot gets district-<name>;
    # the code file is written for the pilot alone, and a separate district still starts urban-district@<its name>
    prepare = (ROOTFS / "usr" / "local" / "bin" / "urban-prepare").read_text(encoding="utf-8")
    assert '  unit|district) KEYID=$UNIT ;;\n  district,center) OWN="district-$UNIT"; KEYID=$OWN ;;\n' in prepare
    assert 'rm -f /run/urban/own-district.code' in prepare
    assert 'if [ -n "$OWN" ] && [ -n "$code" ]; then\n    { printf \'%s\\n\' "$code" > /run/urban/own-district.code' in prepare
    assert ('if [ "$ROLE" = district ] && [ -f "$D/etc/edge-$UNIT.env" ]; then' in prepare
            and 'systemctl start --no-block "urban-district@$UNIT.service"' in prepare)
    assert '/usr/local/bin/urban-center provision own-kit --code /run/urban/own-district.code' in prepare
    # the kit is made before the centre starts (they would race for its CA on the first boot)
    centre = prepare.index('case ",$ROLE," in *,center,*)')
    assert prepare.index("own_district_kit || OWN", centre) < prepare.index("systemctl start --no-block urban-platform", centre)
    # the centre enrols it at its start (api/app.py), and the reporter's unit passes UPDATES (empty: on)
    app = (ROOT / "backend" / "urban" / "api" / "app.py").read_text(encoding="utf-8")
    assert "await pilot.enroll_own(hub, core_audit, c.OWN_DISTRICT, c.DEVICE_NAME, c.DEVICE_PORT, c.OWN_DISTRICT_MARKER)" in app
    unit = (ROOTFS / "etc" / "systemd" / "system" / "urban-district@.service").read_text(encoding="utf-8")
    assert '--updates "${UPDATES}"' in unit


# ---------------------------------------------------------------- its kit: the centre over the loopback

def test_its_kit_points_at_the_centre_over_the_loopback_and_installs_like_a_districts(pki, machine, tmp_path):
    code = pilot.read_code(machine.code)
    kit = pilot.own_kit(code, pki, "urban-center", 8443)
    assert kit["platform"] == "127.0.0.1:8443" and kit["server_name"] == "urban-center" and kit["kind"] == "district"
    assert kit["fingerprint"] == crypto.fingerprint(machine.public) and kit["ca_pem"] == (pki / "ca.pem").read_text()
    # otherwise the kit any district gets (Devices -> kit), made for the same key
    rec = {"id": OWN, "kind": "district", "junction": None, "name": "", "serial": "PILOT-1", "public_key": machine.public}
    assert {k: v for k, v in kit.items() if k != "platform"} == {
        k: v for k, v in make_kit(rec, pki, "urban-center", 8443).items() if k != "platform"}
    env = tmp_path / "etc" / f"edge-{OWN}.env"
    install_kit(kit, OWN, machine.pki / "edge" / f"{OWN}.key", machine.pki, env)
    text = env.read_text()
    assert "PLATFORM=127.0.0.1:8443\n" in text and "SERVER_NAME=urban-center\n" in text
    assert "SENSORS=/run/urban/q/queues.json\n" in text and (machine.pki / "root.pub").exists()
    assert pilot.own_kit(code, pki, "urban-center", 9443)["platform"] == "127.0.0.1:9443"
    # the centre's certificate covers the name the district checks on the loopback
    from urban.devices.registry import server_names
    assert "urban-center" in server_names(pki)


def test_the_centres_command_prints_the_kit(pki, machine, monkeypatch, capsys):
    from urban import config as c
    from urban import provision as prov
    monkeypatch.setattr(c, "PKI", pki)
    monkeypatch.setattr(c, "DEVICE_NAME", "urban-center")
    monkeypatch.setattr(c, "DEVICE_PORT", 8443)
    monkeypatch.setattr(c, "SOURCE", "field")
    monkeypatch.setattr(prov, "load_all_junctions", lambda: [])
    monkeypatch.setattr(sys, "argv", ["urban.provision", "own-kit", "--code", str(machine.code)])
    prov.main()
    kit = json.loads(capsys.readouterr().out)
    assert kit["unit"] == OWN and kit["platform"] == "127.0.0.1:8443" and kit["server_name"] == "urban-center"
    monkeypatch.setattr(sys, "argv", ["urban.provision", "own-kit", "--code", str(machine.code) + ".none"])
    with pytest.raises(SystemExit, match="has no district of its own"):
        prov.main()


def test_the_address_is_the_centres_own_name(pki, machine):
    assert pilot.address("urban-center", 8443) == "urban-center"
    assert pilot.address("urban-center", 9443) == "urban-center:9443"
    assert pilot.address("bad_name,x", 8443) == "127.0.0.1:8443"  # not a host name: the loopback, still a district
    hub, _, _ = enrol(pki, machine, name="urban-center", port=9443)
    assert hub.registry.devices[OWN]["address"] == "urban-center:9443"


# ---------------------------------------------------------------- its reports: signed channel, routed junctions only

class FakeWS:
    """One session of a district against the hub: the hello signed with `key`, then the reports, then the link drops."""

    def __init__(self, device, key, reports):
        self.device, self.key, self.reports = device, key, list(reports)
        self.sent, self.closed = [], None

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def receive_text(self):
        from fastapi import WebSocketDisconnect
        if len(self.sent) == 1:
            nonce = self.sent[0]["nonce"]
            return json.dumps({"type": "hello", "device": self.device, "version": "1", "epoch": 1,
                               "sig": crypto.sign(self.key, {"device": self.device, "nonce": nonce})})
        if self.reports:
            return json.dumps({"type": "report", "events": [], "health": {}, **self.reports.pop(0)})
        raise WebSocketDisconnect()

    async def close(self, code=1000):
        self.closed = code


def test_its_reports_give_the_centre_only_the_queues_of_junctions_routed_through_it(pki, machine):
    p = Platform([71.78, 40.38], 1000, [cam("J-01", "T1", "J-01-SW", "J-01-E"), cam("J-02", "T2", "J-02-S")])
    t0 = 1_800_000_000
    p.handle(clock.frame(t0))
    hub, audit, _ = enrol(pki, machine, hub=hub_of(pki, on_report=lambda rec, report: p.on_report(rec, report)))
    reg = hub.registry
    reg.enroll("edge-J-01", JUNCTIONS[0], crypto.public_hex(crypto.new_key()))  # both units straight to the centre
    reg.enroll("edge-J-02", JUNCTIONS[1], crypto.public_hex(crypto.new_key()))
    report = {"t": t0, "queues": {"J-01-SW": 12.5, "J-02-S": 40.0}}
    # no junction routed through it yet: its numbers are not taken (the control)
    asyncio.run(hub.serve(FakeWS(OWN, machine.key, [report])))
    assert p.sensors == {}
    # the admin routes J-01's unit through it (urban-center provision assign edge-J-01 --district district-center)
    assert reg.assign("edge-J-01", OWN)["district"] == OWN
    asyncio.run(hub.serve(FakeWS(OWN, machine.key, [report])))
    assert p.sensors == {"J-01": {"J-01-SW": (t0, 12.5)}}  # J-02 is not routed through it: not taken
    # a report signed with another key never gets in
    p.sensors.clear()
    ws = FakeWS(OWN, crypto.new_key(), [report])
    asyncio.run(hub.serve(ws))
    assert ws.closed == 4401 and p.sensors == {}
    # routed back straight to the centre: its numbers are not taken any more
    reg.assign("edge-J-01", None)
    asyncio.run(hub.serve(FakeWS(OWN, machine.key, [report])))
    assert p.sensors == {}


def test_a_unit_routed_through_it_keeps_its_way_to_the_centre_and_gets_no_tunnel(pki, machine):
    hub, audit, _ = enrol(pki, machine)
    reg = hub.registry
    unit_key = crypto.public_hex(crypto.new_key())
    direct = reg.enroll("edge-J-01", JUNCTIONS[0], unit_key)
    direct_kit = make_kit(direct, pki, "urban-center", 8443)
    rec = reg.assign("edge-J-01", OWN)
    via_kit = make_kit(rec, pki, "urban-center", 8443, via=reg.devices[OWN])
    # its next kit points where it already pointed: the centre, by the name on its certificate
    assert via_kit["platform"] == direct_kit["platform"] == "urban-center:8443" and via_kit["via"] == OWN
    # the pilot has no tunnel: the unit's tunnel key is taken as any unit's, but no link is ever made or sent for it
    from urban.net import wireguard
    assert reg.wg_report("edge-J-01", wireguard.new_keypair()[1])[0] == "accepted"
    assert hub._wg_problem(reg.devices["edge-J-01"]) == "district has no tunnel key yet"
    assert hub._wg_build("edge-J-01") is None and hub._wg_build(OWN) is None
    assert not [r for r in audit.records if r[0] == "wg-link-refused"]
    assert hub._wg_row(reg.devices["edge-J-01"])["problem"] == "district has no tunnel key yet"


# ---------------------------------------------------------------- its system is the centre's: no updates, no floor

class Releases:
    """A release 2 for x86 offered to every device (what ReleaseStore.newest_for gives)."""

    def __init__(self, path):
        self.file = path
        path.write_bytes(b"bundle")

    def newest_for(self, device, compatible):
        return {"version": 2, "compatible": compatible, "sha256": "0" * 64, "size": 6, "file": self.file.name}

    def path(self, rel):
        return self.file


def test_the_centre_offers_its_own_district_nothing_and_its_floor_does_not_judge_it(pki, machine, tmp_path):
    hub, _, _ = enrol(pki, machine)
    hub.registry.enroll_district("district-1", "10.1.0.2", crypto.public_hex(crypto.new_key()))
    hub.releases = Releases(tmp_path / "urban-2.raucb")
    hub.seen = {OWN: {"at": 0, "health": {}}, "district-1": {"at": 0, "health": {"os": 1}}}
    recs = hub.registry.devices
    assert hub.pending_release("district-1", recs["district-1"])["version"] == 2  # a separate district: as before
    assert hub.pending_release(OWN, recs[OWN]) is None
    hub.seen[OWN]["health"]["os"] = 1  # even a version in its report would not make it an update target
    assert hub.pending_release(OWN, recs[OWN]) is None
    hub.floor = lambda: 5  # an OS floor: a separate district that showed no version is below it, the own one is not
    hub.seen[OWN]["health"].pop("os")
    hub.seen["district-1"]["health"].pop("os")
    assert hub.floor_verdict("district-1") == (5, None) and hub.floor_verdict(OWN) is None
    rows = {r["id"]: r for r in hub.status()}
    assert rows[OWN]["below_floor"] is False and rows["district-1"]["below_floor"] is True


def test_the_reporter_of_the_own_district_takes_no_updates(pki, machine, tmp_path):
    from urban.node.district import District
    code = pilot.read_code(machine.code)
    install_kit(pilot.own_kit(code, pki, "urban-center", 8443), OWN, machine.pki / "edge" / f"{OWN}.key", machine.pki,
                tmp_path / "etc" / f"edge-{OWN}.env")

    def args(updates):
        return SimpleNamespace(unit=OWN, platform="127.0.0.1:8443", server_name="urban-center", pki=str(machine.pki),
                               sensors=None, relay=None, temp=None, key=None, ca=None, gateway_pub=None, root=None,
                               updates=updates)
    own = District(args("off"))
    assert own.dev.updater is None and "os" not in own.dev.health() and "update" not in own.dev.health()
    assert own.url == "wss://127.0.0.1:8443/device" and own.server_name == "urban-center"
    for value in ("on", "", None):  # a separate district: no UPDATES in its env file, the argument is empty
        assert District(args(value)).dev.updater is not None


# ---------------------------------------------------------------- what the camera's numbers pass through

def test_the_queue_file_is_readable_by_the_group_of_the_services_that_read_it(tmp_path, monkeypatch):
    """Vision runs as its own user with UMask=0077; the unit's and the district's service (user urban, its group)
    read the numbers: the file is 0640, written atomically."""
    import os

    from urban.sensors import pipeline
    modes = []
    real = os.chmod
    monkeypatch.setattr(pipeline.os, "chmod", lambda path, mode: (modes.append((Path(path).name, mode)), real(path, mode)))
    pipeline.QueuePipeline([]).write(tmp_path / "queues.json", 0)
    assert modes == [("queues.json.tmp", 0o640)] and json.loads((tmp_path / "queues.json").read_text()) == {}


def test_a_video_file_played_as_a_camera_starts_again_when_it_ends(tmp_path):
    import threading

    import cv2
    import numpy as np

    from urban.vision.source import VideoSource
    video = tmp_path / "short.avi"
    out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 20, (64, 48))
    for i in range(4):  # 0.2 s of video
        out.write(np.full((48, 64, 3), 40 * i, dtype=np.uint8))
    out.release()
    assert len(list(VideoSource(str(video), fps=20, realtime=False).frames())) == 4  # a check: played once
    stop, got = threading.Event(), []
    for _, frame in VideoSource(str(video), fps=20, realtime=True).frames(stop):
        got.append(int(frame[0, 0, 0]))
        if len(got) == 10:
            stop.set()
    assert len(got) == 10 and got[4] < got[3]  # played as a camera: from its beginning again after the 4th frame
    empty = tmp_path / "empty.avi"
    empty.write_bytes(b"")
    src = VideoSource(str(empty), realtime=True)
    assert list(src.frames(threading.Event())) == [] and src.error  # not a camera: it ends, and says why


def test_the_code_file_is_the_enrolment_code_keygen_prints(machine):
    code = pilot.read_code(machine.code)
    assert code == parse_code(machine.code.read_text()) and code["device"] == OWN and code["serial"] == "PILOT-1"
    assert pilot.read_code(machine.code.with_name("missing")) is None
