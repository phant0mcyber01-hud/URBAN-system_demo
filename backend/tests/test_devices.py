import json

import pytest

from urban.devices import crypto
from urban.devices.hub import DeviceHub
from urban.devices.registry import ACTIVE, REVOKED, Registry, device_id, provision
from urban.edge.device import MAX_SKEW, EdgeDevice
from urban.edge.field import FieldAdapter
from urban.equipment.adapter import AdapterError, ControllerAdapter
from urban.sources.field import ControllerBank

JUNCTIONS = [{"id": "J-01", "tls": "T1", "name": "First"}, {"id": "CAM-02", "tls": "T2", "name": "Second"}]


class FakeController(ControllerAdapter):
    def __init__(self, tables):
        self.tables, self.calls = tables, []

    def plans(self, tls):
        return set(self.tables.get(tls, ()))

    def select(self, tls, plan):
        self.calls.append(("select", tls, plan))

    def base(self, tls):
        self.calls.append(("base", tls))

    def keepalive(self, tls, plan):
        self.calls.append(("keepalive", tls, plan))


@pytest.fixture
def pki(tmp_path):
    assert provision(JUNCTIONS, tmp_path) is True
    return tmp_path


WALL = {"now": 1_700_000_000}  # one wall clock shared by hub and devices in a test


def make(pki, tls="T1", junction="J-01", state_path=None):
    """A registry, a hub and the Edge unit of one junction, wired as in the real setup."""
    registry = Registry(pki / "devices.json")
    gateway_key = crypto.load_private((pki / "gateway.key").read_text())
    hub = DeviceHub(registry, gateway_key, on_report=lambda rec, report: None, clock=lambda: 100,
                    wall=lambda: WALL["now"])
    ident = device_id(junction)
    ctl = FakeController({tls: {0, 1, 2}})
    dev = EdgeDevice(ident, tls, crypto.load_private((pki / "edge" / f"{ident}.key").read_text()),
                     crypto.load_public((pki / "gateway.pub").read_text()), ctl,
                     state_path=state_path, wall=lambda: WALL["now"])
    dev.tick(100)
    return registry, hub, dev, ctl


def lease(tls="T1", plan=2):
    return {"op": "lease", "id": "cmd-1", "tls": tls, "plan": plan, "ttl": 180}


# ---------------------------------------------------------------- provisioning and the registry

def test_provisioning_creates_one_identity_per_junction_and_is_idempotent(pki):
    devices = json.loads((pki / "devices.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in devices] == ["edge-J-01", "edge-CAM-02"]
    assert all((pki / name).exists() for name in ("ca.pem", "server.pem", "server.key", "gateway.key", "gateway.pub"))
    before = (pki / "edge" / "edge-J-01.key").read_text()
    assert provision(JUNCTIONS, pki) is False
    assert (pki / "edge" / "edge-J-01.key").read_text() == before
    assert provision(JUNCTIONS + [{"id": "J-03", "tls": "T3", "name": "Third"}], pki) is True
    assert len(json.loads((pki / "devices.json").read_text(encoding="utf-8"))) == 3


def test_device_authenticates_only_with_its_own_key_and_a_fresh_challenge(pki):
    registry, _, dev, _ = make(pki)
    hello = dev.hello("nonce-1")
    assert registry.authenticate(hello["device"], "nonce-1", hello["sig"])["tls"] == "T1"
    assert registry.authenticate(hello["device"], "nonce-2", hello["sig"]) is None      # replayed handshake
    assert registry.authenticate("edge-CAM-02", "nonce-1", hello["sig"]) is None         # signed by someone else
    assert registry.authenticate("edge-UNKNOWN", "nonce-1", hello["sig"]) is None


def test_a_revoked_device_cannot_authenticate_until_it_is_trusted_again(pki):
    registry, _, dev, _ = make(pki)
    registry.set_status(dev.id, REVOKED)
    hello = dev.hello("n")
    assert registry.authenticate(dev.id, "n", hello["sig"]) is None
    assert Registry(pki / "devices.json").devices[dev.id]["status"] == REVOKED            # it is written down
    registry.set_status(dev.id, ACTIVE)
    assert registry.authenticate(dev.id, "n", hello["sig"]) is not None


# ---------------------------------------------------------------- signed commands

def test_device_obeys_a_command_signed_by_the_gateway(pki):
    _, hub, dev, ctl = make(pki)
    assert dev.on_command(hub.envelope(dev.id, lease())) is True
    assert ctl.calls == [("select", "T1", 2)]
    assert dev.report({})["events"][0]["status"] == "applied"


def refused(dev, env):
    assert dev.on_command(env) is False
    return dev.report({})["events"][-1]["reason"]


def test_device_refuses_forged_tampered_and_misaddressed_commands(pki):
    _, hub, dev, ctl = make(pki)
    stranger = DeviceHub(Registry(pki / "devices.json"), crypto.new_key(), lambda *a: None, clock=lambda: 100,
                         wall=lambda: WALL["now"])
    assert refused(dev, stranger.envelope(dev.id, lease())) == "bad signature"
    tampered = hub.envelope(dev.id, lease(plan=1))
    tampered["body"]["plan"] = 2
    assert refused(dev, tampered) == "bad signature"
    assert refused(dev, hub.envelope("edge-CAM-02", lease())) == "addressed to another device"
    assert refused(dev, hub.envelope(dev.id, lease(tls="T2"))) == "not my junction"
    assert refused(dev, {"device": dev.id}) == "malformed command"
    assert ctl.calls == []


def test_device_refuses_replayed_and_stale_commands(pki):
    _, hub, dev, ctl = make(pki)
    first = hub.envelope(dev.id, lease())
    assert dev.on_command(first) is True
    assert refused(dev, first) == "replayed or out of order"
    late = hub.envelope(dev.id, {"op": "release", "id": "cmd-1", "tls": "T1"})
    WALL["now"] += MAX_SKEW + 1                                      # the unit looks at it too late
    try:
        assert refused(dev, late) == "stale command"
    finally:
        WALL["now"] -= MAX_SKEW + 1
    assert len(ctl.calls) == 1


def test_sequence_high_water_mark_survives_a_restart(pki):
    state = pki / "edge" / "edge-J-01.seq"
    _, hub, dev, ctl = make(pki, state_path=state)
    captured = hub.envelope(dev.id, lease())
    assert dev.on_command(captured) is True
    _, _, reborn, ctl2 = make(pki, state_path=state)                 # same unit after a power cycle
    assert reborn.last_seq == captured["seq"]
    assert refused(reborn, captured) == "replayed or out of order"
    assert ctl2.calls == []


def test_release_reaches_the_device_and_the_lease_still_expires_without_the_platform(pki):
    _, hub, dev, ctl = make(pki)
    dev.on_command(hub.envelope(dev.id, lease()))
    assert dev.on_command(hub.envelope(dev.id, {"op": "release", "id": "cmd-1", "tls": "T1"})) is True
    assert ctl.calls[-1] == ("base", "T1")
    dev.on_command(hub.envelope(dev.id, lease()))
    dev.tick(100 + 180)                                            # no session, no renewals: the unit acts alone
    assert ctl.calls[-1] == ("base", "T1") and dev.controller.leases == {}
    assert [e["reason"] for e in dev.outbox if e.get("status") == "reverted"] == ["released", "expired"]   # kept until a session comes back


def test_reports_carry_aggregates_and_health_but_nothing_else(pki):
    _, _, dev, _ = make(pki)
    report = dev.report({"e1": 42.0})
    assert set(report) == {"type", "t", "queues", "events", "health", "epoch"}  # epoch: which gateway key it obeys
    assert report["queues"] == {"e1": 42.0} and report["health"]["version"]


# ---------------------------------------------------------------- the field side

def test_field_adapter_turns_a_dead_link_into_an_adapter_error():
    sent = []
    FieldAdapter(sent.append, {"T1": {0, 1}}).select("T1", 1)
    assert sent == [{"op": "select", "tls": "T1", "plan": 1}]

    def down(msg):
        raise ConnectionError("unplugged")
    with pytest.raises(AdapterError):
        FieldAdapter(down, {"T1": {0, 1}}).select("T1", 1)


def test_emulated_controller_drops_a_plan_that_nobody_confirms():
    ctl = FakeController({"T1": {0, 1, 2}})
    bank = ControllerBank(ctl, backup_time=240)
    bank.apply({"op": "select", "tls": "T1", "plan": 2}, 0)
    bank.apply({"op": "keepalive", "tls": "T1", "plan": 2}, 200)
    assert bank.tick(400) == []                                    # confirmed 200 s ago
    assert bank.tick(441) == ["T1"] and ctl.calls[-1] == ("base", "T1")   # the Edge unit died: the controller acts alone
    bank.apply({"op": "keepalive", "tls": "T1", "plan": 2}, 500)   # a late confirmation revives nothing
    assert bank.confirmed == {}
