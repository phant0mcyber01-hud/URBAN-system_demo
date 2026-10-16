"""Controllers described by profile files: SNMP and Modbus register maps, refused when unsafe."""
import json
from types import SimpleNamespace

import pytest

from test_contact_buses import FakeModbusTcp
from urban.edge.__main__ import make_adapter
from urban.equipment import profiles
from urban.equipment.adapter import AdapterError
from urban.equipment.edge import EdgeController
from urban.equipment.modbus_tcp import ModbusTcp
from urban.equipment.ntcip import NtcipAdapter
from urban.equipment.profiles import ProfileAdapter, ProfileError


class Snmp:
    """Records sets; answers gets from a dict."""

    def __init__(self, values=None, fail=False):
        self.sets, self.values, self.fail = [], dict(values or {}), fail

    def set(self, oid, value):
        if self.fail:
            raise OSError("no answer from the controller")
        self.sets.append((oid, value))
        self.values[oid] = value

    def get(self, oid):
        if oid not in self.values:
            raise OSError("noSuchName")
        return self.values[oid]


def test_the_ntcip_profile_writes_exactly_what_the_ntcip_adapter_writes():
    tables = {"T": {0, 1, 2}}
    by_profile = Snmp()
    ProfileAdapter(profiles.load("ntcip-1202"), by_profile, tables, backup_time=150).select("T", 2)
    by_code = []
    NtcipAdapter(lambda tls, oid, value: by_code.append((oid, value)), tables, backup_time=150).select("T", 2)
    assert by_profile.sets == by_code
    edge = EdgeController(ProfileAdapter(profiles.load("ntcip-1202"), Snmp(), dict(tables)))
    assert edge.handle({"op": "lease", "id": "c", "tls": "T", "plan": 1, "ttl": 60}, 0)[0]["status"] == "applied"
    assert edge.tick(61)[0]["status"] == "reverted"
    assert edge.adapter.client.sets[-1] == ("1.3.6.1.4.1.1206.4.2.1.4.14.0", 0)  # standby: local control


def test_a_modbus_register_controller_is_driven_and_checked():
    dev = FakeModbusTcp(registers={200: 0})
    try:
        profile = profiles.load("modbus-register-template")
        client = ModbusTcp("127.0.0.1", dev.port, timeout=2)
        a = ProfileAdapter(profile, client, {"T": {0, 1, 4}}, backup_time=90)

        def follow(*_):  # the controller shows the plan it runs in register 200
            dev.registers[200] = dev.registers.get(100, 0)
        orig = dev._handle
        dev._handle = lambda pdu: (r := orig(pdu), follow())[0]
        a.select("T", 4)
        assert dev.writes[-2:] == [("register", 101, 90), ("register", 100, 4)]
        a.base("T")
        assert dev.writes[-1] == ("register", 100, 0)

        dev._handle = orig  # a controller that ignores the request: read-back fails, the unit goes back to base
        dev.registers[200] = 0
        with pytest.raises(AdapterError, match="did not take the plan"):
            a.select("T", 1)
        assert dev.writes[-1] == ("register", 100, 0)
    finally:
        dev.close()


def test_unsafe_or_malformed_profiles_are_refused():
    good = json.loads(profiles.BUILTIN.joinpath("ntcip-1202.json").read_text(encoding="utf-8"))
    cases = {
        "no way back": {**good, "base": []},
        "base depends on the plan": {**good, "base": [{"set": "1.3.6.1.4.1.1206.4.2.1.4.14.0", "value": "{pattern}"}]},
        "never selects": {**good, "select": [{"set": "1.3.6.1.4.1.1206.4.2.1.3.3.0", "value": "{backup}"}]},
        "not an oid": {**good, "select": [{"set": "sysContact", "value": "{pattern}"}]},
        "free text value": {**good, "select": [{"set": "1.3.6.1.4.1.1206.4.2.1.4.14.0", "value": "rm -rf"}]},
        "value out of range": {**good, "base": [{"set": "1.3.6.1.4.1.1206.4.2.1.4.14.0", "value": 70000}]},
        "unknown key": {**good, "exec": "x"},
        "unknown transport": {**good, "transport": "telnet"},
        "pattern out of range": {**good, "patterns": {"1": 999}},
    }
    for why, profile in cases.items():
        with pytest.raises(ProfileError):
            profiles.parse(profile)
            pytest.fail(why)
    assert profiles.parse(good)["name"] == "NTCIP 1202 pattern control"  # control: the original is accepted


def test_a_dead_controller_and_the_unit_options(tmp_path):
    a = ProfileAdapter(profiles.load("ntcip-1202"), Snmp(fail=True), {"T": {0, 1}})
    with pytest.raises(AdapterError, match="no answer"):
        a.select("T", 1)
    custom = tmp_path / "my-controller.json"
    custom.write_text(json.dumps({"name": "Vendor X", "transport": "modbus-tcp", "unit": 3,
                                  "select": [{"register": 10, "value": "{pattern}"}],
                                  "base": [{"register": 10, "value": 0}]}), encoding="utf-8")
    args = SimpleNamespace(adapter="profile", controller="10.0.0.9", profile=str(custom), snmp_version=1,
                           community="private", backup_time=120)
    built = make_adapter(args)
    assert isinstance(built, ProfileAdapter) and built.client.unit == 3 and built.client.address == ("10.0.0.9", 502)
    with pytest.raises(SystemExit, match="--profile"):
        make_adapter(SimpleNamespace(**{**vars(args), "profile": str(tmp_path / "missing.json")}))
