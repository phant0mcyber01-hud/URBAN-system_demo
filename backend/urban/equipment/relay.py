"""Controllers that pick a plan from dry-contact inputs.

The plan number is put on N relays as a binary code. All relays open means the base plan,
so a dead or unpowered URBAN Edge leaves the junction on its own program.
Not tried on real hardware: `write` is whatever drives the relay board.
"""
from typing import Callable

from .adapter import BASE_PLAN, AdapterError, ControllerAdapter


class RelayAdapter(ControllerAdapter):
    def __init__(self, write: Callable[[str, list[bool]], None], tables: dict[str, set[int]], relays: int = 3):
        """write(tls, states): drive the relays of that junction; tables: tls id -> approved plan ids."""
        self.write, self.tables, self.relays = write, tables, relays

    def plans(self, tls):
        return set(self.tables.get(tls, ()))

    def select(self, tls, plan):
        if plan not in self.plans(tls) or not 0 <= plan < 2 ** self.relays:
            raise AdapterError("plan cannot be encoded on the relay inputs")
        self._drive(tls, plan)

    def base(self, tls):
        self._drive(tls, BASE_PLAN)

    def _drive(self, tls, plan):
        try:
            self.write(tls, [bool(plan >> bit & 1) for bit in range(self.relays)])
        except OSError as err:
            raise AdapterError(f"relay board: {err}") from err
