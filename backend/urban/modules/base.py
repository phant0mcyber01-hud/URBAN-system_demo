"""City OS modules: what a city service plugs into, and what the core gives it.

URBAN is a City OS: one core (devices and their keys, signed commands, accounts and roles, the audit log, updates
from the centre, the district relay) and modules for city services on top of it. Traffic is the first module,
street lighting the second, water (pump stations, read only: it sends no command at all) the third; a module owns one kind of device and one part of the interface, and never touches
another module's devices. docs/city-os-modules.md is the guide for writing one.

Rules every module keeps (the core enforces the first three, the module the rest):
- a command reaches a device only through Core.send: signed with the gateway key, numbered, time-stamped; the
  device checks all three (edge.device.envelope_refusal) and refuses anything else;
- a person acts through the module's routes, behind the core's roles (viewer / operator / admin; operators and
  admins without their second factor are viewers);
- everything a person or the module decides goes into the core's audit log (Core.audit);
- every command is a lease: it says for how long it holds, the device returns to its own safe behaviour when it
  runs out or the link is lost, and the module renews it only while it is still wanted;
- a device is trusted only for its own object (a lighting cabinet for its cabinet, a pump station's unit for its
  station, a unit for its junction).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class Core:
    """The services the core lends a module. Built by the platform (api/app.py) and by tests with fakes."""
    send: Callable[[str, dict], bool]                # (device id, body) -> delivered now; signs and numbers it
    online: Callable[[str], bool]                    # device id -> has a session now
    devices: Callable[[str], list[dict]]             # device kind -> registry records of that kind
    audit: Callable[..., Any]                        # audit(event, target, note="", actor="platform")
    now: Callable[[], float]                         # wall-clock seconds
    viewer: Any = None                               # FastAPI dependencies giving the account name
    operator: Any = None
    admin: Any = None
    demo: bool = False                               # the demo twin, not the real city
    extra: dict = field(default_factory=dict)        # anything else a built-in module needs from the platform
    # device id -> why the core will not command it now (words for people: below the centre's version floor), or
    # None; send() refuses such a device anyway, this lets a module say so to the person before trying
    refusal: Callable[[str], str | None] = lambda device: None


class Module(Protocol):
    id: str                 # "traffic", "lighting", "water": [a-z]+, also the URL prefix /api/<id>/ and the UI tab
    title: str              # Russian, for the UI
    device_kind: str | None  # the registry kind it owns ("unit" for traffic, "lighting", "water"), None if none

    def attach(self, core: Core) -> None:
        """Called once at start, before any report."""

    def on_report(self, rec: dict, report: dict) -> None:
        """A report from one of its own devices (rec is the registry record). Must not raise on bad input:
        a device's report is untrusted data."""

    def routes(self):
        """A FastAPI APIRouter mounted at /api/<id>, or None."""

    def tick(self, now: float) -> None:
        """Called about once a second: renew leases that are still wanted, let lapsed ones go."""

    def summary(self) -> dict:
        """Short state for /api/modules: {"devices": n, "online": n, "alerts": n, ...}."""

    async def startup(self) -> None:
        """Background work the module needs (for example emulated devices in the demo). Optional."""
