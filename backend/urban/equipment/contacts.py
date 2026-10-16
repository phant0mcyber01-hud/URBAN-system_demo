"""Plan selection through contact inputs: the way to drive almost any signal controller.

Nearly every controller in service - NEMA TS2 cabinets, Russian and Uzbek ДКС/КДУ, Chinese and European models -
has inputs for an external plan selection ("ДУ", "external control", "plan select"), closed by a dry contact.
They differ only in how the plan number is put on the inputs and whether a separate "remote control enabled"
input must be closed too. ContactMap describes that; ContactAdapter drives it through any contact bus
(relay board on RS-485 or Ethernet, or the unit's own GPIO lines - see buses.py).

Safety, the same as for every adapter, and stricter, because contacts have no timer of their own:
- all contacts open is the base plan: a dead unit, a cut cable or an unpowered relay board means local control;
- the code is set before the enable contact closes, and the enable contact opens before the code is cleared,
  so the controller never sees a half-written plan number;
- with feedback wired (the controller's "external plan active" output), a plan the controller does not confirm
  is dropped at once;
- the adapter opens everything when it starts, whatever a previous run left behind.
What contacts cannot do by themselves is notice that this unit hung with a plan selected; the watchdog of the
unit (and, for an independently powered relay board, a hardware dead-man relay on the enable contact) does.
See docs/controllers.md.
"""
import time
from dataclasses import dataclass, field

from .adapter import AdapterError, ControllerAdapter

ENCODINGS = ("binary", "onehot", "bcd")


@dataclass
class ContactMap:
    """How a controller reads a plan number from its inputs.

    encoding  binary: plan n as bits on `inputs` contacts (n < 2**inputs)
              onehot: one contact per plan, contact k-1 for plan k (k <= inputs)
              bcd:    decimal digits, four contacts each, least significant digit first (inputs = 4 * digits)
    inputs    number of plan-code contacts, starting at bus output `first`
    enable    bus output of the "remote control enabled" contact, or None if the controller has none
    codes     our plan id -> the plan number the controller expects (default: the same number)
    """
    encoding: str = "binary"
    inputs: int = 3
    first: int = 0
    enable: int | None = None
    codes: dict[int, int] = field(default_factory=dict)

    def __post_init__(self):
        if self.encoding not in ENCODINGS:
            raise ValueError(f"encoding must be one of {', '.join(ENCODINGS)}")
        if not 1 <= self.inputs <= 32 or self.first < 0:
            raise ValueError("inputs: 1..32 contacts from a non-negative first output")
        if self.encoding == "bcd" and self.inputs % 4:
            raise ValueError("bcd needs four contacts per digit")
        if self.enable is not None and self.first <= self.enable < self.first + self.inputs:
            raise ValueError("the enable contact cannot be one of the plan-code contacts")

    @property
    def width(self) -> int:
        """Bus outputs used, from 0 to the highest one."""
        return max(self.first + self.inputs, (self.enable + 1) if self.enable is not None else 0)

    def number(self, plan: int) -> int:
        return self.codes.get(plan, plan)

    def code(self, number: int) -> list[bool] | None:
        """The plan-code contacts for a controller plan number, or None if this encoding cannot express it."""
        n = self.inputs
        if self.encoding == "binary":
            return [bool(number >> b & 1) for b in range(n)] if 0 <= number < 2 ** n else None
        if self.encoding == "onehot":
            return [i == number - 1 for i in range(n)] if 0 <= number <= n else None
        digits = n // 4
        if not 0 <= number < 10 ** digits:
            return None
        out = []
        for _ in range(digits):
            d, number = number % 10, number // 10
            out += [bool(d >> b & 1) for b in range(4)]
        return out

    def outputs(self, number: int | None, enabled: bool) -> list[bool]:
        """Every bus output: the plan code (all open for None) and the enable contact."""
        states = [False] * self.width
        if number is not None:
            code = self.code(number)
            if code is None:
                raise ValueError("plan cannot be encoded")
            states[self.first:self.first + self.inputs] = code
        if self.enable is not None:
            states[self.enable] = enabled
        return states


@dataclass
class Feedback:
    """A controller output wired back to a bus input: closed while the externally selected plan runs."""
    input: int
    timeout: float = 3.0   # seconds the controller may take to confirm


class ContactAdapter(ControllerAdapter):
    def __init__(self, bus, tables: dict[str, set[int]], cmap: ContactMap, feedback: Feedback | None = None,
                 settle: float = 0.2, sleep=time.sleep, clock=time.monotonic):
        """bus: write(states) and, for feedback, read(count) -> list[bool] (see buses.py).
        settle: seconds between setting the code and closing the enable contact."""
        self.bus, self.tables, self.map, self.feedback = bus, tables, cmap, feedback
        self.settle, self.sleep, self.clock = settle, sleep, clock
        self.last_error = None
        self.current: int | None = None
        try:
            self._write(self.map.outputs(None, False))  # whatever a previous run left: open everything
        except AdapterError:
            pass  # reported on the first real use

    def learn(self, tls, plans):
        self.tables[tls] = set(plans)

    def plans(self, tls):
        return set(self.tables.get(tls, ()))

    def select(self, tls, plan):
        if plan not in self.plans(tls):
            raise AdapterError("plan is not loaded in this controller")
        number = self.map.number(plan)
        if self.map.code(number) is None:
            raise AdapterError(f"plan number {number} cannot be put on {self.map.inputs} {self.map.encoding} contacts")
        if self.map.enable is not None:
            self._write(self.map.outputs(number, False))  # the code first, with control still local
            self.sleep(self.settle)
        self._write(self.map.outputs(number, True))
        self.current = plan
        self._confirm()

    def keepalive(self, tls, plan):
        # rewrite: a relay board that lost power or was reset in between must show the plan again
        self._write(self.map.outputs(self.map.number(plan), True))
        self._confirm()

    def base(self, tls):
        held, self.current = self.current, None
        if self.map.enable is not None and held is not None:
            # control goes back to the controller first, with the code still steady; then the code is cleared
            self._write(self.map.outputs(self.map.number(held), False))
            self.sleep(self.settle)
        self._write(self.map.outputs(None, False))

    def _confirm(self):
        if self.feedback is None:
            return
        end = self.clock() + self.feedback.timeout
        while True:
            try:
                if self.bus.read(self.feedback.input + 1)[self.feedback.input]:
                    return
            except (OSError, IndexError) as err:
                self._fail(f"feedback input: {err}")
            if self.clock() >= end:
                self._fail("the controller did not confirm the external plan")
            self.sleep(0.1)

    def _fail(self, why: str):
        try:
            self._write(self.map.outputs(None, False))
        except AdapterError:
            pass
        self.current = None
        raise AdapterError(why)

    def _write(self, states: list[bool]):
        try:
            self.bus.write(states)
            self.last_error = None
        except OSError as err:
            self.last_error = str(err)
            raise AdapterError(f"contacts: {err}") from err
