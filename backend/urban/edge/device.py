"""One URBAN Edge unit: identity, command checks, leases and reports. No networking here.

A command is obeyed only if all of this holds:
- it is signed by the gateway key this unit was provisioned with;
- it is addressed to this unit and to its own junction;
- its sequence number is higher than anything seen before (no replays);
- it is fresh: issued within MAX_SKEW seconds of the wall clock of the unit.
Everything else is refused and reported. Leases then run exactly as in EdgeController.

The sequence high-water mark survives restarts (it is written to `state_path`), so a command
captured before a restart cannot be replayed after it.
"""
import random
import time
from pathlib import Path

from ..devices import crypto
from ..equipment.adapter import ControllerAdapter
from ..equipment.edge import EdgeController

VERSION = "0.1.0"
MAX_SKEW = 30  # seconds between the issue time of a command and the clock of the unit


class EdgeDevice:
    def __init__(self, device_id: str, tls: str, key, gateway_public, adapter: ControllerAdapter,
                 state_path: Path | None = None, wall=time.time):
        """state_path: where the last accepted sequence number is kept; wall(): wall-clock seconds."""
        self.id, self.tls, self.key, self.gateway_public = device_id, tls, key, gateway_public
        self.controller = EdgeController(adapter)
        self.state_path, self.wall = state_path, wall
        self.last_seq = 0
        if state_path is not None and state_path.exists():
            try:
                self.last_seq = int(state_path.read_text())
            except ValueError:
                pass
        self.clock = 0
        self.outbox: list[dict] = []  # events waiting to be reported
        self._temp = 38.0 + random.random() * 6

    # ---- session
    def hello(self, nonce: str) -> dict:
        return {"type": "hello", "device": self.id, "version": VERSION,
                "sig": crypto.sign(self.key, {"device": self.id, "nonce": nonce})}

    # ---- commands from the gateway
    def on_command(self, env: dict) -> bool:
        why = self._refusal(env)
        if why:
            self.outbox.append({"type": "edge", "status": "refused", "device": self.id, "reason": why})
            return False
        self.last_seq = env["seq"]
        if self.state_path is not None:
            self.state_path.write_text(str(self.last_seq))
        self.outbox += self.controller.handle(env["body"], self.clock)
        return True

    def _refusal(self, env: dict) -> str | None:
        try:
            signed = {k: env[k] for k in ("device", "seq", "t", "body")}
            body = env["body"]
        except (KeyError, TypeError):
            return "malformed command"
        if not crypto.verify(self.gateway_public, signed, str(env.get("sig"))):
            return "bad signature"
        if signed["device"] != self.id:
            return "addressed to another device"
        if not isinstance(signed["seq"], int) or signed["seq"] <= self.last_seq:
            return "replayed or out of order"
        if not isinstance(signed["t"], (int, float)) or abs(self.wall() - signed["t"]) > MAX_SKEW:
            return "stale command"
        if not isinstance(body, dict) or body.get("tls") != self.tls:
            return "not my junction"
        return None

    # ---- once a second
    def tick(self, now: int):
        """Advance the clock and expire leases. Runs whether or not the platform is reachable."""
        self.clock = now
        self.outbox += self.controller.tick(now)

    def report(self, queues: dict) -> dict:
        """What goes up to the platform: aggregates and events only."""
        self._temp += random.uniform(-0.2, 0.2)
        events, self.outbox = self.outbox, []
        return {"type": "report", "t": self.clock, "queues": queues, "events": events,
                "health": {"version": VERSION, "temp": round(self._temp, 1), "door": "closed",
                           "leases": len(self.controller.leases)}}

    def reset(self):
        """The street was reset (scenario restart in the demo): nothing is leased any more."""
        self.controller.leases.clear()
        self.outbox.clear()
