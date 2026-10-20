"""The traffic module: the existing traffic platform (urban/platform.py) seen as the first City OS module.

Nothing is rewritten. The platform keeps its own endpoints (/api/state, /api/commands, /api/junctions ...), its
command gateway and its simulation hooks; this class only gives it the module interface: it owns the units of
junctions (device kind "unit"; district servers are core devices whose reports also go here, as before) and hands
their reports to Platform.on_report, and it summarises the platform for GET /api/modules.
"""
from __future__ import annotations

from .base import Core


class TrafficModule:
    id = "traffic"
    title = "Светофоры и пробки"
    device_kind = "unit"

    def __init__(self, platform=None):
        self.platform = platform
        self.core: Core | None = None

    def attach(self, core: Core) -> None:
        self.core = core
        if self.platform is None:
            self.platform = core.extra.get("platform")
        if self.platform is None:
            raise ValueError("the traffic module needs the traffic platform (core.extra['platform'])")

    def on_report(self, rec: dict, report: dict) -> None:
        self.platform.on_report(rec, report)

    def routes(self):
        return None  # the traffic endpoints stay where they are, in api/app.py

    def tick(self, now: float) -> None:
        pass  # the platform runs on its own frames (the clock or the simulation)

    async def startup(self) -> None:
        pass

    def summary(self) -> dict:
        p = self.platform
        rows = [d for d in list(p.dev.values()) if d.get("k", "unit") == "unit"]
        pending = sum(1 for pr in list(p.gateway.proposals.values()) if pr.get("status") == "pending")
        return {"devices": len(rows), "online": sum(1 for d in rows if d.get("o")),
                "alerts": sum(len(d.get("al") or ()) for d in rows),
                "junctions": len(p.cameras), "units": len(rows),
                "districts": sum(1 for d in list(p.dev.values()) if d.get("k") == "district"),
                "pending": pending}
