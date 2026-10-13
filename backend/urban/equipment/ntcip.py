"""Controllers that speak NTCIP 1202 over SNMP.

Selecting a plan is one write to systemPatternControl. Every such write restarts the
unitBackupTime countdown of the controller; when it runs out the controller goes back to
local control by itself, so the lease is enforced a second time inside the controller.

Not tried on a real controller. `snmp_set(tls, oid, value)` is the transport, kept out of
this module so it can be tested without a device. Check the OIDs against the MIB of the
controller actually used.
"""
from typing import Callable

from .adapter import AdapterError, ControllerAdapter

ASC = "1.3.6.1.4.1.1206.4.2.1"
UNIT_BACKUP_TIME = f"{ASC}.3.3.0"          # seconds without a control write before reverting; 0 disables
SYSTEM_PATTERN_CONTROL = f"{ASC}.4.14.0"   # 0 = standby (local time-base control), 1..253 = pattern number
STANDBY = 0


class NtcipAdapter(ControllerAdapter):
    def __init__(self, snmp_set: Callable[[str, str, int], None], tables: dict[str, set[int]],
                 backup_time: int = 180, pattern_of: Callable[[int], int] = lambda plan: plan):
        """tables: tls id -> approved plan ids; pattern_of: our plan id -> pattern number in the controller."""
        self.snmp_set, self.tables, self.backup_time, self.pattern_of = snmp_set, tables, backup_time, pattern_of
        self._armed = set()

    def plans(self, tls):
        return set(self.tables.get(tls, ()))

    def select(self, tls, plan):
        if plan not in self.plans(tls):
            raise AdapterError("plan is not loaded in this controller")
        if tls not in self._armed:  # the controller must fall back on its own before we touch anything
            self._set(tls, UNIT_BACKUP_TIME, self.backup_time)
            self._armed.add(tls)
        self._set(tls, SYSTEM_PATTERN_CONTROL, self.pattern_of(plan))

    def keepalive(self, tls, plan):
        self._set(tls, SYSTEM_PATTERN_CONTROL, self.pattern_of(plan))

    def base(self, tls):
        self._set(tls, SYSTEM_PATTERN_CONTROL, STANDBY)

    def _set(self, tls, oid, value):
        try:
            self.snmp_set(tls, oid, value)
        except OSError as err:
            raise AdapterError(f"snmp: {err}") from err
