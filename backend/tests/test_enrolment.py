"""Unit keys born on the unit: keygen, enrolment of the public half, live pick-up by a running platform."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from urban.devices import crypto
from urban.devices.hub import DeviceHub
from urban.devices.registry import ACTIVE, Registry, provision
from urban.edge.__main__ import keygen
from urban.platform import Platform

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "J-02", "tls": "T2", "name": "Second"}]


@pytest.fixture
def field_pki(tmp_path):
    provision(JUNCTIONS, tmp_path, demo_devices=False)
    return tmp_path


def test_field_provisioning_makes_no_unit_keys(field_pki):
    assert json.loads((field_pki / "devices.json").read_text()) == []
    assert not any((field_pki / "edge").iterdir())
    assert (field_pki / "gateway.key").exists() and (field_pki / "server.pem").exists()


def test_keygen_on_the_unit_then_enrol_the_public_half(tmp_path, field_pki, capsys):
    unit_pki = tmp_path / "unit"
    args = SimpleNamespace(unit="edge-J-01", key=None, pki=str(unit_pki))
    keygen(args)
    out = capsys.readouterr().out
    public = out.split("public key:")[1].split()[0]
    with pytest.raises(SystemExit):
        keygen(args)  # never overwrites
    key = crypto.load_private((unit_pki / "edge" / "edge-J-01.key").read_text())
    assert crypto.public_hex(key) == public and crypto.fingerprint(public) in out

    reg = Registry(field_pki / "devices.json")
    rec = reg.enroll("edge-J-01", JUNCTIONS[0], public, "URB-0001")
    assert rec["status"] == ACTIVE and rec["tls"] == "T1" and "key" not in json.dumps(rec).replace("public_key", "")
    assert reg.authenticate("edge-J-01", "n1", crypto.sign(key, {"device": "edge-J-01", "nonce": "n1"})) is rec
    with pytest.raises(ValueError):
        reg.enroll("edge-J-01", JUNCTIONS[0], "not hex")
    with pytest.raises(ValueError):
        reg.enroll("edge-other", JUNCTIONS[0], public)  # the junction already has a unit
    with pytest.raises(ValueError):
        reg.enroll("J-02", JUNCTIONS[1], public)          # not a unit id


def test_fingerprint_is_stable_and_short():
    fp = crypto.fingerprint("00" * 32)
    assert len(fp) == 19 and fp.count("-") == 3 and fp == crypto.fingerprint("00" * 32 + "\n")


class FakeWS:
    def __init__(self):
        self.closed = None

    async def close(self, code=1000):
        self.closed = code


def test_running_hub_picks_up_an_enrolment_from_another_process(field_pki):
    reg = Registry(field_pki / "devices.json")
    old = crypto.new_key()
    reg.enroll("edge-J-01", JUNCTIONS[0], crypto.public_hex(old))
    hub = DeviceHub(reg, crypto.new_key(), on_report=lambda rec, rep: None)
    ws = FakeWS()
    hub.sessions["edge-J-01"] = ws
    first = hub.status()
    time.sleep(0.01)
    new = crypto.new_key()
    Registry(field_pki / "devices.json").enroll("edge-J-01", JUNCTIONS[0], crypto.public_hex(new))  # the CLI

    async def tick():
        rows = hub.status()
        await asyncio.sleep(0)
        return rows
    rows = asyncio.run(tick())
    assert ws.closed == 4403 and "edge-J-01" not in hub.sessions   # the session on the old key ends
    assert rows[0]["fingerprint"] == crypto.fingerprint(crypto.public_hex(new)) != first[0]["fingerprint"]
    assert reg.authenticate("edge-J-01", "n", crypto.sign(old, {"device": "edge-J-01", "nonce": "n"})) is None


def test_hub_enrol_closes_the_old_session(field_pki):
    reg = Registry(field_pki / "devices.json")
    hub = DeviceHub(reg, crypto.new_key(), on_report=lambda rec, rep: None)
    ws = FakeWS()
    hub.sessions["edge-J-02"] = ws
    rec = asyncio.run(hub.enroll("edge-J-02", JUNCTIONS[1], crypto.public_hex(crypto.new_key()), "URB-2"))
    assert rec["serial"] == "URB-2" and ws.closed == 4403


def test_platform_audits_a_key_change():
    cam = {"id": "J-01", "tls": "T1", "camera": False, "lon": 71.78, "lat": 40.38, "name": "x",
           "approaches": [], "phases": [], "plans": []}
    p = Platform([71.78, 40.38], 1000, [cam])
    row = {"id": "edge-J-01", "junction": "J-01", "tls": "T1", "status": "active", "online": True, "age": 1,
           "health": {}, "fingerprint": "AAAA-BBBB-CCCC-DDDD"}
    p.devices([row], 1)
    assert not any(r["event"] == "device-key-changed" for r in p.gateway.records)
    p.devices([{**row, "fingerprint": "1111-2222-3333-4444"}], 2)
    rec = p.gateway.records[-1] if p.gateway.records[-1]["event"] == "device-key-changed" else \
        next(r for r in p.gateway.records if r["event"] == "device-key-changed")
    assert rec["target"] == "edge-J-01" and "1111-2222-3333-4444" in rec["note"]
