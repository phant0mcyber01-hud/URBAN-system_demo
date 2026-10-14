"""What URBAN Edge needs from a signal controller, whatever protocol it speaks.

A controller holds a table of plans approved by the traffic engineer. Plan 0 is always the
base (local) plan. An adapter can only ask the controller to run one of those plans; it has
no way to switch lamps or to invent timings.
"""
from abc import ABC, abstractmethod

BASE_PLAN = 0


class AdapterError(Exception):
    """The controller refused or could not be reached."""


class ControllerAdapter(ABC):
    @abstractmethod
    def plans(self, tls: str) -> set[int]:
        """Plan ids loaded in this controller, including BASE_PLAN. Empty if the controller is unknown."""

    @abstractmethod
    def select(self, tls: str, plan: int) -> None:
        """Ask the controller to run an approved plan. Raises AdapterError if it cannot."""

    @abstractmethod
    def base(self, tls: str) -> None:
        """Hand control back to the base plan of the controller."""

    def keepalive(self, tls: str, plan: int) -> None:
        """Confirm the selected plan. Protocols with a loss-of-communication timer need this."""


class NoController(ControllerAdapter):
    """A unit with no controller wired yet: it learns the approved table from the platform, reports
    queues and health, and refuses every plan request so that nothing is promised to anyone."""

    def __init__(self):
        self.tables: dict[str, set[int]] = {}
        self.last_error = "no controller attached"

    def learn(self, tls: str, plans: set[int]):
        self.tables[tls] = set(plans)

    def plans(self, tls):
        return set(self.tables.get(tls, ()))

    def select(self, tls, plan):
        raise AdapterError("no controller attached")

    def base(self, tls):
        pass
