"""City OS modules: the registry of built-in modules and the core's wiring of them (see base.py, docs/city-os-modules.md).

    load_modules(["traffic", "lighting", "water"], core) -> instances, attached, in the order named
    report_router(modules)                        -> on_report(rec, report) for the device hub: a device's report
                                                     reaches only the module that owns its device kind
    describe(modules)                             -> what GET /api/modules returns
    mount_routes(app, modules)                    -> every module's router at /api/<id>
    tick_all / startup_all                        -> the once-a-second tick and the start-up of all modules
"""
from __future__ import annotations

import importlib
import re

from ..devices.registry import kind_of
from .base import Core, Module

# built-in modules: name -> (module path, class). Imported only when named, so a missing or broken optional
# module cannot stop a centre that does not use it.
BUILTIN = {"traffic": ("urban.modules.traffic", "TrafficModule"),
           "lighting": ("urban.modules.lighting", "LightingModule"),
           "water": ("urban.modules.water", "WaterModule")}
NAME = re.compile(r"^[a-z]+$")
# a record of this kind is served by the module of the other kind: districts relay the units' reports to traffic
SERVED_AS = {"district": "unit"}


class ModuleError(ValueError):
    """A module could not be loaded: said clearly at start, never ignored."""


def load_modules(names: list[str], core: Core) -> list[Module]:
    """Instantiate and attach the modules named, in that order. An unknown or repeated name, or a module that
    cannot be imported, is an error (ModuleError) at start."""
    out: list[Module] = []
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        if name not in BUILTIN:
            raise ModuleError(f"unknown module {name!r} in MODULES; available: {', '.join(sorted(BUILTIN))}")
        if any(m.id == name for m in out):
            raise ModuleError(f"module {name!r} is named twice in MODULES")
        path, cls = BUILTIN[name]
        try:
            module = getattr(importlib.import_module(path), cls)()
        except ImportError as err:
            raise ModuleError(f"module {name!r} cannot be loaded: {err}") from err
        if module.id != name or not NAME.fullmatch(module.id):
            raise ModuleError(f"module {name!r} says its id is {module.id!r}")
        module.attach(core)
        out.append(module)
    owners: dict[str, str] = {}
    for m in out:  # one kind of device, one owner
        if m.device_kind:
            if m.device_kind in owners:
                raise ModuleError(f"modules {owners[m.device_kind]!r} and {m.id!r} both own {m.device_kind!r} devices")
            owners[m.device_kind] = m.id
    return out


def report_router(modules: list[Module], log=print):
    """The callback for DeviceHub(on_report=...): hands a report to the module that owns the device's kind
    (a district server's to the module of units, i.e. traffic). A kind nobody owns is ignored, with one log line."""
    by_kind = {m.device_kind: m for m in modules if m.device_kind}
    unowned: set[str] = set()

    def route(rec: dict, report: dict) -> None:
        kind = kind_of(rec)
        module = by_kind.get(SERVED_AS.get(kind, kind))
        if module is None:
            if kind not in unowned:
                unowned.add(kind)
                log(f"modules: no module owns {kind!r} devices, reports of {rec.get('id')} are ignored")
            return
        module.on_report(rec, report)
    return route


def describe(modules: list[Module], log=print) -> list[dict]:
    """The body of GET /api/modules. A module whose summary fails is still listed, with an empty summary."""
    out = []
    for m in modules:
        try:
            summary = m.summary()
        except Exception as err:  # the list must not fail because one module does
            log(f"modules: summary of {m.id} failed: {err!r}")
            summary = {}
        out.append({"id": m.id, "title": m.title, "device_kind": m.device_kind, "summary": summary})
    return out


def mount_routes(app, modules: list[Module]) -> None:
    """Every module's router at /api/<id> (a module with no routes adds none)."""
    for m in modules:
        router = m.routes()
        if router is not None:
            app.include_router(router, prefix=f"/api/{m.id}")


def tick_all(modules: list[Module], now: float, log=print) -> None:
    """About once a second: every module's tick. A module that fails is logged and does not stop the others."""
    for m in modules:
        try:
            m.tick(now)
        except Exception as err:
            log(f"modules: tick of {m.id} failed: {err!r}")


async def startup_all(modules: list[Module], log=print) -> None:
    """Every module's background work at application start; a failing one is logged, the centre still starts."""
    for m in modules:
        try:
            await m.startup()
        except Exception as err:
            log(f"modules: startup of {m.id} failed: {err!r}")
