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
from ..devices.trust import GatewayTrust
from ..equipment.adapter import ControllerAdapter
from ..equipment.edge import EdgeController

VERSION = "0.1.0"
MAX_SKEW = 30  # seconds between the issue time of a command and the clock of the unit


class EdgeDevice:
    def __init__(self, device_id: str, tls: str | None, key, gateway_public, adapter: ControllerAdapter,
                 state_path: Path | None = None, wall=time.time, script: dict | None = None,
                 root_public=None, gateway_state: Path | None = None, probe=None):
        """tls: the junction this unit is installed at, or None to take it from the platform's welcome;
        gateway_public: gateway.pub, or None for a unit that takes the gateway key from the root's certificate;
        state_path: where the last accepted sequence number is kept; wall(): wall-clock seconds;
        script: demo faults, e.g. {"heat": (120, 540)} or {"door": (420, 500)} in simulation seconds;
        root_public: the installed root key, to follow gateway key rotations (devices/trust.py);
        gateway_state: where the accepted gateway certificate is kept;
        probe(): {"temp", "door"} from the cabinet's real sensors (edge/probe.py), None in either when absent.
        Without a probe the unit is a demo one and its temperature and door follow `script`."""
        self.id, self.tls, self.key = device_id, tls, key
        self.trust = GatewayTrust(root_public, gateway_public, gateway_state)
        self.adapter = adapter
        self.controller = EdgeController(adapter)
        self.script = script or {}
        self.probe = probe
        self.started = time.monotonic()
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
    @property
    def gateway_public(self):
        return self.trust.public

    def hello(self, nonce: str) -> dict:
        return {"type": "hello", "device": self.id, "version": VERSION, "epoch": self.trust.epoch,
                "sig": crypto.sign(self.key, {"device": self.id, "nonce": nonce})}

    def on_welcome(self, msg: dict) -> bool:
        """The platform states which junction this unit is enrolled for and which plans are approved
        there (the junction passport), and presents its gateway certificate. A unit configured for another
        junction, or offered a gateway key it cannot trust, refuses to work."""
        why = self.trust.offer(msg.get("gateway"))
        if why:
            self.refused = why
            return False
        tls = msg.get("tls")
        if self.tls is None and isinstance(tls, str) and tls:
            self.tls = tls
        if tls != self.tls:
            return False
        learn, plans = getattr(self.adapter, "learn", None), msg.get("plans")
        if learn is not None and isinstance(plans, list):
            learn(self.tls, {int(p) for p in plans if isinstance(p, (int, str)) and str(p).isdigit()})
        return True

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
        if self.gateway_public is None:
            return "no gateway key yet"
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

    def cabinet(self) -> tuple[float | None, str | None]:
        """Temperature and door: measured on a real unit, scripted on a demo one."""
        if self.probe is not None:
            seen = self.probe()
            return seen.get("temp"), seen.get("door")
        self._temp += random.uniform(-0.2, 0.2)
        heat = self.script.get("heat")
        if heat is not None:  # demo: a cabinet in the sun for a while, then it cools down again
            if heat[0] <= self.clock < heat[1]:
                self._temp += 0.08
            elif self.clock >= heat[1] and self._temp > 42:
                self._temp -= 0.06
        door = self.script.get("door")
        door_open = door is not None and door[0] <= self.clock < door[1]
        return round(self._temp, 1), "open" if door_open else "closed"

    def report(self, queues: dict) -> dict:
        """What goes up to the platform: aggregates, lease events and the unit's own health."""
        temp, door = self.cabinet()
        events, self.outbox = self.outbox, []
        return {"type": "report", "t": self.clock, "queues": queues, "events": events, "epoch": self.trust.epoch,
                "health": {"version": VERSION, "temp": temp, "door": door,
                           "controller": getattr(self.adapter, "last_error", None) or "ok",
                           "leases": len(self.controller.leases), "uptime": round(time.monotonic() - self.started)}}

    def reset(self):
        """The street was reset (scenario restart in the demo): nothing is leased any more."""
        self.controller.leases.clear()
        self.outbox.clear()
