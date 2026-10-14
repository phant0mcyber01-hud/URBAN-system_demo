"""Gateway key rotation: a root-signed certificate with an epoch, adopted by units, never rolled back."""
import json

import pytest

from urban.devices import crypto, trust
from urban.devices.hub import DeviceHub
from urban.devices.registry import Registry, device_id, provision
from urban.edge.device import EdgeDevice
from urban.equipment.adapter import NoController

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}]
WALL = 1_700_000_000


@pytest.fixture
def pki(tmp_path):
    provision(JUNCTIONS, tmp_path)
    return tmp_path


def unit(pki, gateway_pub=True):
    ident = device_id("J-01")
    return EdgeDevice(ident, "T1", crypto.load_private((pki / "edge" / f"{ident}.key").read_text()),
                      crypto.load_public((pki / "gateway.pub").read_text()) if gateway_pub else None,
                      NoController(), wall=lambda: WALL,
                      root_public=crypto.load_public((pki / "root.pub").read_text()),
                      gateway_state=pki / "edge" / f"{ident}.gateway.json")


def command(pki, seq):
    hub = DeviceHub(Registry(pki / "devices.json"), crypto.load_private((pki / "gateway.key").read_text()),
                    on_report=lambda r, p: None, wall=lambda: WALL)
    hub._seq = seq
    return {"type": "cmd", **hub.envelope("edge-J-01", {"op": "release", "id": "x", "tls": "T1"})}


def welcome(pki):
    return {"type": "welcome", "tls": "T1", "plans": [0, 1], "gateway": json.loads((pki / "gateway.cert.json").read_text())}


def test_provision_issues_a_root_and_a_certificate(pki):
    cert = json.loads((pki / "gateway.cert.json").read_text())
    root = crypto.load_public((pki / "root.pub").read_text())
    assert cert["epoch"] == 1 and cert["gateway"] == (pki / "gateway.pub").read_text().strip()
    assert trust.valid(root, cert) and not trust.valid(root, dict(cert, epoch=2))
    assert trust.ensure(pki) is False


def test_older_folders_get_a_root_and_a_certificate(tmp_path):
    provision(JUNCTIONS, tmp_path)
    for name in ("root.key", "root.pub", "gateway.cert.json"):
        (tmp_path / name).unlink()
    assert trust.ensure(tmp_path) is True
    assert json.loads((tmp_path / "gateway.cert.json").read_text())["epoch"] == 1


def test_unit_follows_a_rotation_and_never_goes_back(pki):
    dev = unit(pki)
    assert dev.on_welcome(welcome(pki)) and dev.trust.epoch == 1
    assert dev.on_command(command(pki, 1))
    old_welcome, old_command = welcome(pki), command(pki, 2)

    trust.rotate(pki)
    assert dev.on_welcome(welcome(pki)) and dev.trust.epoch == 2       # adopted
    assert dev.on_command(command(pki, 3))                             # the new key works
    assert not dev.on_command(old_command)                             # the old key no longer does
    assert dev.outbox[-1]["reason"] == "bad signature"
    assert not dev.on_welcome(old_welcome) and "older" in dev.refused  # no going back

    again = unit(pki)                                                  # after a restart of the unit
    assert again.trust.epoch == 2 and not again.on_welcome(old_welcome)


def test_a_certificate_not_signed_by_the_root_is_refused(pki):
    dev = unit(pki)
    other_root = crypto.new_key()
    forged = trust.issue(other_root, crypto.public_hex(crypto.new_key()), 99)
    assert not dev.on_welcome({**welcome(pki), "gateway": forged}) and "root" in dev.refused
    assert dev.trust.epoch == 0


def test_unit_without_gateway_pub_learns_the_key_from_the_certificate(pki):
    dev = unit(pki, gateway_pub=False)
    assert dev.gateway_public is None and not dev.on_command(command(pki, 1))  # nothing to check against yet
    assert dev.on_welcome(welcome(pki)) and dev.on_command(command(pki, 2))
    bare = unit(pki, gateway_pub=False)
    (pki / "edge" / "edge-J-01.gateway.json").unlink()  # forget what the first unit learned
    bare = unit(pki, gateway_pub=False)
    assert not bare.on_welcome({**welcome(pki), "gateway": None}) and "no gateway certificate" in bare.refused


def test_rotation_needs_the_matching_root_key(pki, tmp_path):
    (pki / "root.key").rename(tmp_path / "usb-root.key")               # kept off the server
    with pytest.raises(FileNotFoundError):
        trust.rotate(pki)
    stranger = tmp_path / "stranger.key"
    stranger.write_text(crypto.private_hex(crypto.new_key()))
    with pytest.raises(ValueError):
        trust.rotate(pki, stranger)
    assert trust.rotate(pki, tmp_path / "usb-root.key")["epoch"] == 2


def test_hello_reports_the_epoch(pki):
    dev = unit(pki)
    dev.on_welcome(welcome(pki))
    assert dev.hello("n")["epoch"] == 1
