"""Plan selection through contacts: codes, the enable contact, feedback, and open contacts as the safe state."""
from types import SimpleNamespace

import pytest

from urban.edge.__main__ import make_adapter
from urban.equipment.adapter import AdapterError
from urban.equipment.contacts import ContactAdapter, ContactMap, Feedback
from urban.equipment.edge import EdgeController


class Bus:
    """Records every write; feedback inputs and failures on demand."""

    def __init__(self, inputs=None, fail=False):
        self.writes, self.inputs, self.fail = [], inputs or [], fail

    def write(self, states):
        if self.fail:
            raise OSError("board does not answer")
        self.writes.append(list(states))

    def read(self, count):
        if callable(self.inputs):
            return self.inputs()
        return self.inputs[:count]


def adapter(cmap, bus=None, feedback=None, plans=(0, 1, 2, 3, 5, 9, 12)):
    bus = bus or Bus()
    return ContactAdapter(bus, {"T": set(plans)}, cmap, feedback, settle=0, sleep=lambda s: None), bus


def test_codes():
    assert ContactMap("binary", 3).code(5) == [True, False, True]
    assert ContactMap("binary", 3).code(8) is None
    assert ContactMap("onehot", 4).code(3) == [False, False, True, False]
    assert ContactMap("onehot", 4).code(0) == [False] * 4 and ContactMap("onehot", 4).code(5) is None
    assert ContactMap("bcd", 8).code(12) == [False, True, False, False, True, False, False, False]  # 2, then 1
    assert ContactMap("bcd", 4).code(10) is None
    with pytest.raises(ValueError):
        ContactMap("gray", 3)
    with pytest.raises(ValueError):
        ContactMap("bcd", 6)
    with pytest.raises(ValueError):
        ContactMap("binary", 3, enable=1)  # the enable contact among the code contacts


def test_start_opens_everything_and_base_is_all_open():
    a, bus = adapter(ContactMap("binary", 3))
    assert bus.writes == [[False, False, False]]  # whatever a previous run left behind
    a.select("T", 5)
    assert bus.writes[-1] == [True, False, True]
    a.base("T")
    assert bus.writes[-1] == [False, False, False]


def test_enable_contact_closes_after_the_code_and_opens_before_it_is_cleared():
    a, bus = adapter(ContactMap("binary", 3, first=0, enable=3))
    bus.writes.clear()
    a.select("T", 3)
    assert bus.writes == [[True, True, False, False], [True, True, False, True]]  # code first, then enable
    bus.writes.clear()
    a.base("T")
    assert bus.writes == [[True, True, False, False], [False, False, False, False]]  # enable off first, then the code


def test_plan_numbers_of_the_controller_and_what_cannot_be_encoded():
    a, bus = adapter(ContactMap("onehot", 4, codes={9: 2}))
    a.select("T", 9)
    assert bus.writes[-1] == [False, True, False, False]  # our plan 9 is the controller's plan 2
    with pytest.raises(AdapterError, match="cannot be put"):
        a.select("T", 12)
    with pytest.raises(AdapterError, match="not loaded"):
        a.select("T", 7)


def test_feedback_confirms_or_the_plan_is_dropped():
    clock = iter(range(100))
    a = ContactAdapter(Bus(inputs=[True]), {"T": {0, 1}}, ContactMap("binary", 2), Feedback(0, timeout=2),
                       settle=0, sleep=lambda s: None, clock=lambda: next(clock))
    a.select("T", 1)
    assert a.current == 1

    bus = Bus(inputs=[False])
    clock = iter(range(100))
    a = ContactAdapter(bus, {"T": {0, 1}}, ContactMap("binary", 2), Feedback(0, timeout=2),
                       settle=0, sleep=lambda s: None, clock=lambda: next(clock))
    with pytest.raises(AdapterError, match="did not confirm"):
        a.select("T", 1)
    assert bus.writes[-1] == [False, False] and a.current is None  # back to base at once


def test_a_dead_board_is_an_adapter_error_and_the_edge_drops_the_lease():
    a, bus = adapter(ContactMap("binary", 3))
    edge = EdgeController(a)
    ev = edge.handle({"op": "lease", "id": "c1", "tls": "T", "plan": 1, "ttl": 60}, 0)
    assert ev[0]["status"] == "applied"
    bus.fail = True
    ev = edge.handle({"op": "lease", "id": "c1", "tls": "T", "ttl": 60}, 30)
    assert ev[0]["status"] == "reverted" and "controller lost" in ev[0]["reason"]
    with pytest.raises(AdapterError):
        a.select("T", 1)
    assert a.last_error == "board does not answer"


def test_the_unit_builds_contact_adapters_from_its_options(tmp_path):
    base = dict(adapter="contacts", controller="COM9", bus="modbus-rtu", encoding="bcd", relays=8, enable_contact=8,
                feedback_input=None, plan_codes="1=11", gpio_lines=None, active_low=False, relay_unit=1, baud=9600,
                profile=None, community="private", snmp_version=1, backup_time=180)
    a = make_adapter(SimpleNamespace(**base))
    assert isinstance(a, ContactAdapter) and a.map.encoding == "bcd" and a.map.enable == 8 and a.map.codes == {1: 11}
    with pytest.raises(SystemExit, match="four contacts"):
        make_adapter(SimpleNamespace(**{**base, "relays": 6}))
    with pytest.raises(SystemExit, match="lines needed"):
        make_adapter(SimpleNamespace(**{**base, "bus": "gpio", "controller": "/dev/gpiochip0", "gpio_lines": "17,27"}))
    legacy = make_adapter(SimpleNamespace(**{**base, "adapter": "relay", "encoding": "binary", "relays": 3,
                                             "enable_contact": None, "plan_codes": None}))
    assert isinstance(legacy, ContactAdapter) and legacy.map.encoding == "binary"
