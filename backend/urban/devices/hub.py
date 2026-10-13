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

from fastapi import WebSocket, WebSocketDisconnect

from . import crypto
from .registry import REVOKED, Registry

HELLO_TIMEOUT = 10  # seconds a connection may stay unauthenticated


class DeviceHub:
    def __init__(self, registry: Registry, gateway_key, on_report, clock=lambda: 0):
        """on_report(device record, report dict); clock(): platform time put into every command."""
        self.registry, self.key, self.on_report, self.clock = registry, gateway_key, on_report, clock
        self.sessions: dict[str, WebSocket] = {}
        self.seen: dict[str, dict] = {}  # device id -> {"at": wall time, "health": {...}}
        self._seq = time.time_ns() // 1_000_000  # survives restarts without going backwards

    # ---- towards devices: this is gateway.send
    def online(self, tls: str) -> bool:
        rec = self.registry.by_tls(tls)
        return rec is not None and rec["id"] in self.sessions

    def envelope(self, device: str, body: dict) -> dict:
        self._seq += 1
        env = {"device": device, "seq": self._seq, "t": self.clock(), "body": body}
        return {**env, "sig": crypto.sign(self.key, env)}

    def send(self, msg: dict):
        """Deliver a gateway message to the device of that junction (or to all for revoke_all).

        A device that is not connected simply does not get it: its lease runs out by itself.
        """
        if msg.get("op") == "revoke_all":
            targets = list(self.sessions)
        else:
            rec = self.registry.by_tls(msg.get("tls", ""))
            targets = [rec["id"]] if rec and rec["id"] in self.sessions else []
        for device in targets:
            text = json.dumps({"type": "cmd", **self.envelope(device, msg)}, ensure_ascii=False)
            asyncio.ensure_future(self._deliver(device, text))

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
            rec = self.registry.authenticate(str(hello.get("device")), nonce, str(hello.get("sig")))
            if rec is None:
                await ws.close(code=4401)
                return
            device = rec["id"]
            old = self.sessions.pop(device, None)
            if old is not None:  # the same unit reconnected: one session per device
                await _close(old)
            self.sessions[device] = ws
            self.seen[device] = {"at": time.time(), "health": {"version": hello.get("version")}}
            await ws.send_text(json.dumps({"type": "welcome", "t": self.clock()}))
            while True:
                report = json.loads(await ws.receive_text())
                if report.get("type") == "report":
                    self.seen[device] = {"at": time.time(), "health": report.get("health", {})}
                    self.on_report(rec, report)
        except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError):
            pass
        finally:
            if device is not None and self.sessions.get(device) is ws:
                del self.sessions[device]

    # ---- registry operations
    async def set_status(self, device: str, status: str) -> dict | None:
        rec = self.registry.set_status(device, status)
        if rec is not None and status == REVOKED:
            ws = self.sessions.pop(device, None)
            if ws is not None:
                await _close(ws)
        return rec

    def status(self) -> list[dict]:
        now = time.time()
        out = []
        for rec in self.registry.devices.values():
            seen = self.seen.get(rec["id"], {})
            out.append({"id": rec["id"], "junction": rec["junction"], "name": rec.get("name", ""),
                        "status": rec["status"], "online": rec["id"] in self.sessions,
                        "age": round(now - seen["at"]) if "at" in seen else None,
                        "health": seen.get("health", {})})
        return out


async def _close(ws: WebSocket):
    try:
        await ws.close(code=4403)
    except Exception:
        pass
