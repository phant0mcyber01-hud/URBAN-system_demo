"""Device hub: the platform end of the channel to URBAN Edge devices.

A device connects out to us (never the other way round), over TLS. It is trusted only after
it signs our challenge with the key enrolled in the registry. Commands going down are signed
with the gateway key and carry a rising sequence number and a timestamp, so a device can
refuse forgeries and replays without trusting the network.
"""
import asyncio
import json
import secrets
import time
import traceback

from fastapi import WebSocket, WebSocketDisconnect

from . import crypto
from .health import clean
from .registry import REVOKED, Registry

HELLO_TIMEOUT = 10  # seconds a connection may stay unauthenticated


class DeviceHub:
    def __init__(self, registry: Registry, gateway_key, on_report, clock=lambda: 0, wall=time.time,
                 plans: dict[str, set[int]] | None = None, gateway_cert: dict | None = None):
        """on_report(device record, report dict); clock(): platform time sent in the welcome;
        wall(): wall-clock seconds stamped into every command, so a device can refuse stale ones;
        plans: tls id -> approved plan ids, handed to each unit at session start (its junction passport);
        gateway_cert: the root's certificate for gateway_key, presented at session start (devices/trust.py)."""
        self.plans = plans or {}
        self.gateway_cert = gateway_cert
        self.registry, self.key, self.on_report, self.clock, self.wall = registry, gateway_key, on_report, clock, wall
        self.sessions: dict[str, WebSocket] = {}
        self.seen: dict[str, dict] = {}  # device id -> {"at": wall time, "health": {...}}
        self.connects: dict[str, int] = {}  # device id -> sessions since the platform started
        self._stale: set[str] = set()  # sessions opened on a key or status that has since changed
        self.epochs: dict[str, int | None] = {}  # device id -> gateway key epoch it trusted when it connected
        self._seq = time.time_ns() // 1_000_000  # survives restarts without going backwards

    # ---- towards devices: this is gateway.send
    def online(self, tls: str) -> bool:
        rec = self.registry.by_tls(tls)
        return rec is not None and rec["id"] in self.sessions

    def envelope(self, device: str, body: dict) -> dict:
        self._seq += 1
        env = {"device": device, "seq": self._seq, "t": int(self.wall()), "body": body}
        return {**env, "sig": crypto.sign(self.key, env)}

    def send(self, msg: dict):
        """Deliver a gateway message to the device of that junction.

        A device that is not connected simply does not get it: its lease runs out by itself.
        """
        rec = self.registry.by_tls(msg.get("tls", ""))
        if rec is None or rec["id"] not in self.sessions:
            return
        text = json.dumps({"type": "cmd", **self.envelope(rec["id"], msg)}, ensure_ascii=False)
        asyncio.ensure_future(self._deliver(rec["id"], text))

    async def _deliver(self, device: str, text: str):
        ws = self.sessions.get(device)
        if ws is None:
            return
        try:
            await ws.send_text(text)
        except Exception:
            self.sessions.pop(device, None)

    # ---- from devices
    async def serve(self, ws: WebSocket):
        await ws.accept()
        nonce = secrets.token_hex(16)
        device = None
        try:
            await ws.send_text(json.dumps({"type": "challenge", "nonce": nonce}))
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT))
            if not isinstance(hello, dict):
                hello = {}
            rec = self.registry.authenticate(str(hello.get("device")), nonce, str(hello.get("sig")))
            if rec is None:
                await ws.close(code=4401)
                return
            device = rec["id"]
            old = self.sessions.pop(device, None)
            if old is not None:  # the same unit reconnected: one session per device
                await _close(old)
            self.sessions[device] = ws
            self.connects[device] = self.connects.get(device, 0) + 1
            self.seen[device] = self.reconnected(device, hello.get("version"))
            epoch = hello.get("epoch")
            self.epochs[device] = epoch if isinstance(epoch, int) else None
            await ws.send_text(json.dumps({"type": "welcome", "t": self.clock(), "tls": rec["tls"],
                                           "plans": sorted(self.plans.get(rec["tls"], ())),
                                           "gateway": self.gateway_cert}))
            while True:
                report = json.loads(await ws.receive_text())
                if isinstance(report, dict) and report.get("type") == "report":
                    self.seen[device] = {"at": time.time(), "health": clean(report.get("health"))}
                    if isinstance(report.get("epoch"), int) and not isinstance(report["epoch"], bool):
                        self.epochs[device] = report["epoch"]  # the gateway key epoch the unit obeys now
                    self.on_report(rec, report)
        except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError):
            pass  # the usual ends of a session: gone, silent, or talking nonsense
        except Exception:  # a bug in report handling must not look like a flaky network
            print(f"hub: dropping the session of {device}: report handling failed", flush=True)
            traceback.print_exc()
        finally:
            if device is not None and self.sessions.get(device) is ws:
                del self.sessions[device]

    def reconnected(self, device: str, version) -> dict:
        """The hub's view of a unit at session start: its last known health stays until the first report,
        so a short link outage does not clear its alerts and raise them again a second later."""
        last = self.seen.get(device, {}).get("health", {})
        return {"at": time.time(), "health": {**last, **clean({"version": version})}}

    # ---- registry operations
    def _sync(self):
        """Pick up enrolments made by another process; sessions of units whose key or status changed are closed
        (on the event loop; from a worker thread they are remembered and closed on the next call there)."""
        self._stale |= self.registry.refresh()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for device in list(self._stale):
            self._stale.discard(device)
            ws = self.sessions.pop(device, None)
            if ws is not None:
                asyncio.ensure_future(_close(ws))

    async def enroll(self, device: str, junction: dict, public: str, serial: str = "") -> dict:
        rec = self.registry.enroll(device, junction, public, serial)
        ws = self.sessions.pop(device, None)
        if ws is not None:  # a session on the previous key ends; the unit reconnects with its new one
            await _close(ws)
        return rec

    async def set_status(self, device: str, status: str) -> dict | None:
        rec = self.registry.set_status(device, status)
        if rec is not None and status == REVOKED:
            ws = self.sessions.pop(device, None)
            if ws is not None:
                await _close(ws)
        return rec

    def status(self) -> list[dict]:
        self._sync()
        now = time.time()
        out = []
        for rec in self.registry.devices.values():
            seen = self.seen.get(rec["id"], {})
            out.append({"id": rec["id"], "junction": rec["junction"], "tls": rec["tls"], "name": rec.get("name", ""),
                        "serial": rec.get("serial", ""), "enrolled": rec.get("enrolled", ""),
                        "fingerprint": crypto.fingerprint(rec["public_key"]),
                        "gateway_epoch": self.epochs.get(rec["id"]),
                        "status": rec["status"], "online": rec["id"] in self.sessions,
                        "connects": self.connects.get(rec["id"], 0),
                        "age": round(now - seen["at"]) if "at" in seen else None,
                        "health": seen.get("health", {})})
        return out


async def _close(ws: WebSocket):
    try:
        await ws.close(code=4403)
    except Exception:
        pass
