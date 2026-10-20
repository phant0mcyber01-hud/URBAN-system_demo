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
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from . import crypto
from .health import clean
from .registry import ACTIVE, REVOKED, Registry, kind_of

HELLO_TIMEOUT = 10  # seconds a connection may stay unauthenticated
LINK_TTL = 3600     # seconds a download link for an update package stays valid
REOFFER = 600       # seconds before the same release is offered to the same device again (it may be downloading)


class DeviceHub:
    def __init__(self, registry: Registry, gateway_key, on_report, clock=lambda: 0, wall=time.time,
                 plans: dict[str, set[int]] | None = None, gateway_cert: dict | None = None, releases=None):
        """on_report(device record, report dict); clock(): platform time sent in the welcome;
        wall(): wall-clock seconds stamped into every command, so a device can refuse stale ones;
        plans: tls id -> approved plan ids, handed to each unit at session start (its junction passport);
        gateway_cert: the root's certificate for gateway_key, presented at session start (devices/trust.py);
        releases: the ReleaseStore of OS update packages to offer (urban/release.py), or None to offer nothing."""
        self.releases = releases
        self.links: dict[str, dict] = {}  # download token -> {"device", "version", "until"}
        self._offered: dict[str, tuple[int, float]] = {}  # device id -> (release version, wall time of the offer)
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

    def send_device(self, device: str, body: dict) -> bool:
        """Deliver a signed command to one device by its id (an update offer is not about any junction).
        False if it is not connected."""
        if device not in self.sessions:
            return False
        text = json.dumps({"type": "cmd", **self.envelope(device, body)}, ensure_ascii=False)
        asyncio.ensure_future(self._deliver(device, text))
        return True

    # ---- over-the-air updates: the offer goes down the signed channel, the package comes over the same listener
    def pending_release(self, device: str, rec: dict) -> dict | None:
        """The release this device should be offered now, or None: it must be a unit or a district server that
        reported a system version (an integer) older than the newest release that is not withdrawn and targets it."""
        if self.releases is None or kind_of(rec) not in ("unit", "district"):
            return None
        have = self.seen.get(device, {}).get("health", {}).get("os")
        rel = self.releases.newest_for(device)
        if rel is None or not isinstance(have, int) or isinstance(have, bool) or have >= rel["version"]:
            return None
        if not self.releases.path(rel).is_file():
            return None
        return rel

    def offer_update(self, device: str, rel: dict) -> dict | None:
        """Mint a download link bound to this device and release and build the signed offer for it."""
        now = self.wall()
        self.links = {t: v for t, v in self.links.items() if v["until"] > now and v["device"] != device}
        token = secrets.token_hex(32)
        self.links[token] = {"device": device, "version": rel["version"], "until": now + LINK_TTL}
        self._offered[device] = (rel["version"], now)
        return {"update": {"version": rel["version"], "sha256": rel["sha256"], "size": rel["size"],
                           "path": f"/update/{token}"}}

    async def _offer(self, device: str, rec: dict):
        """Offer the device the release it is due, unless the same one went out less than REOFFER ago."""
        try:
            rel = self.pending_release(device, rec)
            if rel is None:
                return
            last = self._offered.get(device)
            if last is not None and last[0] == rel["version"] and 0 <= self.wall() - last[1] < REOFFER:
                return
            body = self.offer_update(device, rel)
            text = json.dumps({"type": "cmd", **self.envelope(device, body)}, ensure_ascii=False)
            await self._deliver(device, text)
        except (KeyError, TypeError, OSError):  # a damaged release entry must not end the device's session
            print(f"hub: could not offer an update to {device}", flush=True)
            traceback.print_exc()

    def update_file(self, token: str) -> tuple[Path, str] | None:
        """The package a download link stands for: valid for one device and one release for LINK_TTL, and only
        while that release is still offered to that device. (path, file name), or None."""
        link = self.links.get(token)
        if link is None or self.releases is None:
            return None
        if link["until"] <= self.wall():
            self.links.pop(token, None)
            return None
        rec = self.registry.devices.get(link["device"])
        rel = self.releases.get(link["version"])
        if (rec is None or rec["status"] != ACTIVE or rel is None or rel.get("withdrawn")
                or (rel.get("devices") is not None and link["device"] not in rel["devices"])):
            return None
        path = self.releases.path(rel)
        return (path, path.name) if path.is_file() else None

    def update_routes(self) -> APIRouter:
        """GET /update/{token} for the device listener: the package a download link stands for. Whatever is wrong
        with the link (unknown, expired, withdrawn, another device's) is the same plain 404."""
        router = APIRouter()

        @router.get("/update/{token}")
        def update_package(token: str):
            found = self.update_file(token)
            if found is None:
                raise HTTPException(404)
            path, name = found
            return FileResponse(path, media_type="application/octet-stream", filename=name)
        return router

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
            self.registry.mark_seen(device)
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
            self._offered.pop(device, None)  # a new session may have a device that lost its download
            await self._offer(device, rec)
            while True:
                report = json.loads(await ws.receive_text())
                if isinstance(report, dict) and report.get("type") == "report":
                    self.seen[device] = {"at": time.time(), "health": clean(report.get("health"))}
                    if isinstance(report.get("epoch"), int) and not isinstance(report["epoch"], bool):
                        self.epochs[device] = report["epoch"]  # the gateway key epoch the unit obeys now
                    if kind_of(rec) == "district":  # which junctions it is trusted for, from the registry as it is now
                        rec = {**rec, "junctions": [d["junction"] for d in self.registry.devices.values()
                                                    if d.get("district") == rec["id"] and d.get("junction")
                                                    and d["status"] == ACTIVE]}  # a revoked unit is no one's junction
                    self.on_report(rec, report)
                    await self._offer(device, rec)
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

    async def enroll(self, device: str, junction: dict, public: str, serial: str = "", district: str | None = None) -> dict:
        rec = self.registry.enroll(device, junction, public, serial, district)
        ws = self.sessions.pop(device, None)
        if ws is not None:  # a session on the previous key ends; the unit reconnects with its new one
            await _close(ws)
        return rec

    async def enroll_district(self, device: str, address: str, public: str, serial: str = "") -> dict:
        rec = self.registry.enroll_district(device, address, public, serial)
        ws = self.sessions.pop(device, None)
        if ws is not None:
            await _close(ws)
        return rec

    def assign(self, device: str, district: str | None) -> dict | None:
        return self.registry.assign(device, district)

    async def set_plans(self, tls: str, plans: set[int] | None):
        """A junction's approved plans changed: its unit is reconnected so it learns them at session start."""
        if plans:
            self.plans[tls] = set(plans)
        else:
            self.plans.pop(tls, None)
        for rec in self.registry.devices.values():
            if tls and rec.get("tls") == tls and (ws := self.sessions.pop(rec["id"], None)) is not None:
                await _close(ws, CLOSED_PLANS)

    async def remove(self, device: str) -> dict | None:
        rec = self.registry.remove(device)
        ws = self.sessions.pop(device, None)
        if ws is not None:
            await _close(ws)
        self.seen.pop(device, None)
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
            out.append({"id": rec["id"], "kind": kind_of(rec), "junction": rec.get("junction"), "tls": rec.get("tls"),
                        "name": rec.get("name", ""), "address": rec.get("address"), "district": rec.get("district"),
                        "serial": rec.get("serial", ""), "enrolled": rec.get("enrolled", ""),
                        "first_seen": rec.get("first_seen"),
                        "fingerprint": crypto.fingerprint(rec["public_key"]),
                        "gateway_epoch": self.epochs.get(rec["id"]),
                        "status": rec["status"], "online": rec["id"] in self.sessions,
                        "connects": self.connects.get(rec["id"], 0),
                        "age": round(now - seen["at"]) if "at" in seen else None,
                        "health": seen.get("health", {})})
        return out


CLOSED_TRUST, CLOSED_PLANS = 4403, 4012  # key or status changed / the junction's plan table changed


async def _close(ws: WebSocket, code: int = CLOSED_TRUST):
    try:
        await ws.close(code=code)
    except Exception:
        pass
