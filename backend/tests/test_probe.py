"""A real unit reports only what its sensors measure; an absent sensor is unknown, never a made-up value."""
from urban.devices import crypto
from urban.devices.health import assess
from urban.edge.device import EdgeDevice
from urban.edge.probe import Probe, read_door, read_temperature
from urban.equipment.adapter import NoController


def test_temperature_files(tmp_path):
    zone = tmp_path / "temp"
    zone.write_text("47312\n")  # a Linux thermal zone: millidegrees
    assert read_temperature(zone) == 47.3
    w1 = tmp_path / "w1_slave"
    w1.write_text("72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n")
    assert read_temperature(w1) == 23.1
    w1.write_text("72 01 4b 46 7f ff 0e 10 57 : crc=00 NO\n")  # a failed read is no reading
    assert read_temperature(w1) is None
    zone.write_text("not a number")
    assert read_temperature(zone) is None
    assert read_temperature(tmp_path / "missing") is None


def test_door_switch(tmp_path):
    gpio = tmp_path / "value"
    gpio.write_text("1\n")
    assert read_door(gpio) == "open"
    gpio.write_text("0\n")
    assert read_door(gpio) == "closed"
    assert read_door(gpio, open_value="0") == "open"  # a switch wired the other way round
    assert read_door(tmp_path / "missing") is None


def test_a_real_unit_never_invents_its_cabinet(tmp_path):
    key = crypto.new_key()
    dev = EdgeDevice("edge-J-01", "J-01", key, crypto.load_public(crypto.public_hex(crypto.new_key())), NoController(),
                     probe=Probe(temp=str(tmp_path / "no-sensor"), door=None))
    health = dev.report({})["health"]
    assert health["temp"] is None and health["door"] is None  # no sensor, no number
    (tmp_path / "t").write_text("31000")
    (tmp_path / "d").write_text("1")
    dev.probe = Probe(temp=str(tmp_path / "t"), door=str(tmp_path / "d"))
    health = dev.report({})["health"]
    assert health["temp"] == 31.0 and health["door"] == "open"
    alerts = assess({"status": "active", "online": True, "age": 0, "health": health})
    assert any(a["kind"] == "door" for a in alerts)  # a real open door locks the junction


def test_an_enrolled_unit_that_never_connected_is_not_an_alarm():
    waiting = {"status": "active", "online": False, "age": None, "first_seen": None, "health": {}}
    assert assess(waiting) == []
    lost = {**waiting, "first_seen": "2026-10-15T10:00:00", "age": 300}
    assert [a["kind"] for a in assess(lost)] == ["offline"]
