"""The field side of URBAN Edge in the demo: sensors and the controller behind one local link.

In a cabinet these are cables: cameras on one port, the controller on a serial line. Here both
are emulated by the simulation process and reached over a local WebSocket. A real unit
replaces this module with the camera pipeline and a NTCIP or relay adapter.
"""
from ..equipment.adapter import AdapterError, ControllerAdapter


class FieldAdapter(ControllerAdapter):
    """Controller access through the field link. `send` raises ConnectionError when the link is down."""

    def __init__(self, send, plans: dict[str, set[int]]):
        self.send, self._plans = send, plans
        self.last_error: str | None = None  # what the unit reports as its controller link state

    def learn(self, tls, plans):
        self._plans[tls] = set(plans)

    def plans(self, tls):
        return set(self._plans.get(tls, ()))

    def _op(self, op, tls, **extra):
        try:
            self.send({"op": op, "tls": tls, **extra})
            self.last_error = None
        except ConnectionError as err:
            self.last_error = str(err)
            raise AdapterError(f"controller link: {err}") from err

    def select(self, tls, plan):
        self._op("select", tls, plan=plan)

    def keepalive(self, tls, plan):
        self._op("keepalive", tls, plan=plan)

    def base(self, tls):
        self._op("base", tls)
