"""Unit keys born on the unit: keygen, enrolment of the public half, live pick-up by a running platform."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from urban.devices import crypto
from urban.devices.enrolment import install_kit, make_code, make_kit, parse_code
from urban.devices.hub import DeviceHub
from urban.devices.registry import ACTIVE, REVOKED, Registry, provision
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
    args = SimpleNamespace(unit="edge-J-01", key=None, pki=str(unit_pki), serial="URB-0001")
    keygen(args)
    out = capsys.readouterr().out
    code = parse_code(next(line for line in out.splitlines() if "URBAN1:" in line))
    public = code["public"]
    assert code["device"] == "edge-J-01" and code["serial"] == "URB-0001"
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


def test_enrolment_code_survives_copying_and_catches_damage():
    public = crypto.public_hex(crypto.new_key())
    code = make_code("edge-J-07", "URB-0042", public)
    assert parse_code(code) == {"device": "edge-J-07", "serial": "URB-0042", "public": public}
    assert parse_code("  " + code[:30] + chr(10) + code[30:] + " ")["public"] == public  # wrapped by a messenger
    damaged = code.replace(public[:4], "0000" if public[:4] != "0000" else "1111")
    with pytest.raises(ValueError, match="контрольная сумма"):
        parse_code(damaged)
    with pytest.raises(ValueError, match="не код подключения"):
        parse_code(public)
    with pytest.raises(ValueError):
        make_code("J-07", "", public)  # not a unit id


def test_the_kit_goes_in_place_only_on_its_own_unit(tmp_path, field_pki):
    unit_pki = tmp_path / "unit" / "pki"
    keygen(SimpleNamespace(unit="edge-J-01", key=None, pki=str(unit_pki), serial=""))
    key = crypto.load_private((unit_pki / "edge" / "edge-J-01.key").read_text())
    reg = Registry(field_pki / "devices.json")
    rec = reg.enroll("edge-J-01", JUNCTIONS[0], crypto.public_hex(key), "URB-0001")
    kit = make_kit(rec, field_pki, "city.example", 8443)
    assert kit["platform"] == "city.example:8443" and kit["root_pub"] and kit["gateway_pub"] is None
    assert not any("PRIVATE" in str(v) for v in kit.values())  # nothing secret in it

    env = tmp_path / "unit" / "edge-edge-J-01.env"
    written = install_kit(kit, "edge-J-01", unit_pki / "edge" / "edge-J-01.key", unit_pki, env)
    assert (unit_pki / "ca.pem").read_text() == (field_pki / "ca.pem").read_text()
    assert (unit_pki / "root.pub").read_text().strip() == (field_pki / "root.pub").read_text().strip()
    assert "PLATFORM=city.example:8443" in env.read_text() and str(env) in written

    # a second setup keeps what the installer wrote and only moves the platform
    env.write_text(env.read_text().replace("ADAPTER=none", "ADAPTER=ntcip"))
    install_kit({**kit, "platform": "new.example:8443", "server_name": "new.example"}, "edge-J-01",
                unit_pki / "edge" / "edge-J-01.key", unit_pki, env)
    text = env.read_text()
    assert "ADAPTER=ntcip" in text and "PLATFORM=new.example:8443" in text and "SERVER_NAME=new.example" in text

    with pytest.raises(ValueError, match="for edge-J-01"):
        install_kit(kit, "edge-J-02", unit_pki / "edge" / "edge-J-01.key", unit_pki, env)
    other = reg.enroll("edge-J-01", JUNCTIONS[0], crypto.public_hex(crypto.new_key()))  # the box was replaced
    with pytest.raises(ValueError, match="made for key"):
        install_kit(make_kit(other, field_pki, "city.example", 8443), "edge-J-01",
                    unit_pki / "edge" / "edge-J-01.key", unit_pki, env)


def test_a_unit_is_removed_only_after_it_was_revoked(field_pki):
    reg = Registry(field_pki / "devices.json")
    reg.enroll("edge-J-01", JUNCTIONS[0], crypto.public_hex(crypto.new_key()))
    with pytest.raises(ValueError):
        reg.remove("edge-J-01")
    reg.set_status("edge-J-01", REVOKED)
    assert reg.remove("edge-J-01")["id"] == "edge-J-01"
    assert reg.by_tls("T1") is None and Registry(field_pki / "devices.json").devices == {}
    reg.enroll("edge-J-01b", JUNCTIONS[0], crypto.public_hex(crypto.new_key()))  # the junction is free again
