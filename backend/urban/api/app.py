"""HTTP/WebSocket API, static UI and the device endpoint.

    python -m urban   ->  UI on http://localhost:8080, devices on wss://localhost:8443

Three kinds of clients, three levels of trust:
- anyone on this machine may read (UI, /api/state, the read-only /ws);
- an operator, with the token printed at startup, may decide on proposals, pull everything
  back, cut the demo link and revoke a device;
- an URBAN Edge unit connects to /device over TLS and is trusted only after it signs a
  challenge with the key enrolled in the registry.

Data flow up:   Edge units -> /device -> DeviceHub -> Platform (queue readings, lease events)
                simulation -> bus -> Platform.handle -> /ws (vehicles for the twin, analysis)
Data flow down: operator -> Command Gateway -> DeviceHub (signed) -> Edge unit -> controller
The local /field socket stands for the cables in a cabinet: sensors and controller of the demo.
"""
import asyncio
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config as c
from ..core.bus import PLATFORM_FRAME, SOURCE_FRAME, Bus
from ..devices import crypto
from ..devices.hub import DeviceHub
from ..devices.registry import ACTIVE, REVOKED, Registry, provision
from ..junctions import load_all_junctions, load_cameras
from ..platform import Platform
from ..sources import sumo
from ..sources.field import FieldEmulator

OPERATOR_TOKEN = os.getenv("URBAN_OPERATOR_TOKEN") or secrets.token_urlsafe(9)
ACTOR = "operator"
SPAWN_EDGE = os.getenv("URBAN_EDGE", "auto") != "manual"  # manual: start `python -m urban.edge` yourself

junctions = load_all_junctions() or load_cameras()
provision(junctions, c.PKI)  # demo convenience: a deployment enrols devices one by one
FIELD_TOKEN = secrets.token_urlsafe(24)  # new on every start; the Edge process reads it from the pki folder
(c.PKI / "field.token").write_text(FIELD_TOKEN)

bus = Bus()
field = FieldEmulator(FIELD_TOKEN)
meta = json.loads((c.OUT / "meta.json").read_text())
hub = DeviceHub(Registry(c.PKI / "devices.json"), crypto.load_private((c.PKI / "gateway.key").read_text()),
                on_report=lambda rec, report: platform.on_report(rec["tls"], report), clock=lambda: platform.t)
platform = Platform(meta["center"], meta["radius"], junctions, send=hub.send, reachable=hub.online)
clients: set[WebSocket] = set()


def devices_brief() -> dict:
    """Per junction: [online, status, temperature, version, device id] for the UI."""
    return {d["junction"]: [int(d["online"]), d["status"], d["health"].get("temp"), d["health"].get("version"), d["id"]]
            for d in hub.status()}


async def analyse(raw: dict):
    frame = platform.handle(raw)
    if raw["t"] % 5 == 0 or frame.get("reset"):
        frame["dev"] = devices_brief()
    await bus.publish(PLATFORM_FRAME, frame)


async def broadcast(frame: dict):
    msg = json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            clients.discard(ws)


bus.subscribe(SOURCE_FRAME, analyse)
bus.subscribe(PLATFORM_FRAME, broadcast)


def source_thread(loop: asyncio.AbstractEventLoop, stop: threading.Event):
    def publish(frame):
        if stop.is_set():
            raise SystemExit
        asyncio.run_coroutine_threadsafe(bus.publish(SOURCE_FRAME, frame), loop)

    while not stop.is_set():  # loop the scenario for the demo
        try:
            sumo.run(publish, field=field)
        except SystemExit:
            return
        except Exception:
            traceback.print_exc()
            stop.wait(5)


@asynccontextmanager
async def lifespan(_):
    print(f"\n  Operator token: {OPERATOR_TOKEN}\n  (viewing needs no token; approving commands does)\n", flush=True)
    field.loop = asyncio.get_running_loop()
    stop = threading.Event()
    threading.Thread(target=source_thread, args=(field.loop, stop), daemon=True).start()
    watchdog = asyncio.create_task(edge_watchdog(stop)) if SPAWN_EDGE else None
    yield
    stop.set()
    if watchdog is not None:
        watchdog.cancel()


async def edge_watchdog(stop: threading.Event):
    """Keeps the demo Edge process alive: if it dies, start it again after a short pause."""
    edge = None
    try:
        while not stop.is_set():
            if edge is None or edge.poll() is not None:
                if edge is not None:
                    print(f"edge process exited with {edge.returncode}, restarting", flush=True)
                    await asyncio.sleep(3)
                edge = subprocess.Popen([sys.executable, "-m", "urban.edge", "--keep-running"])
            await asyncio.sleep(2)
    finally:
        if edge is not None and edge.poll() is None:
            edge.terminate()


app = FastAPI(lifespan=lifespan, title="URBAN platform API")
app.add_middleware(GZipMiddleware, minimum_size=2000)

# devices get their own listener (TLS, another port): nothing else is reachable through it
device_app = FastAPI(title="URBAN device endpoint", docs_url=None, redoc_url=None, openapi_url=None)


@device_app.websocket("/device")
async def device_endpoint(ws: WebSocket):
    await hub.serve(ws)


@app.websocket("/field")
async def field_endpoint(ws: WebSocket):
    await field.serve(ws)


@app.middleware("http")
async def revalidate_data(request, call_next):
    """Map layers and the page itself are rebuilt in place: browsers must revalidate them (ETag), not guess.
    Scripts and styles have hashed names and may be cached freely."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/data/") or path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache"
    return response

_failures: list[float] = []


def operator(x_operator_token: str = Header(default="")):
    """Operator authentication with a simple lockout against guessing."""
    now = time.time()
    _failures[:] = [f for f in _failures if now - f < 60]
    if len(_failures) >= 5:
        raise HTTPException(429, "too many failed attempts, wait a minute")
    if not hmac.compare_digest(x_operator_token.encode(), OPERATOR_TOKEN.encode()):
        _failures.append(now)
        raise HTTPException(401, "operator token required")
    return ACTOR


@app.get("/api/health")
def health():
    return {"ok": True, "t": platform.t, "devices_online": len(hub.sessions)}


@app.get("/api/state")
def state():
    return {**platform.snapshot(), "dev": devices_brief()}


@app.get("/api/audit")
def audit():
    return {**platform.audit(), "intact": platform.gateway.verify(), "records": platform.gateway.records[-50:]}


@app.get("/api/devices")
def devices():
    return hub.status()


@app.get("/api/me")
def me(who: str = Depends(operator)):
    return {"operator": True}


class Link(BaseModel):
    up: bool


class DeviceStatus(BaseModel):
    status: str


# async handlers run on the event loop, the same thread that runs Platform.handle: no races on gateway state
@app.post("/api/commands/{pid}/approve")
async def approve(pid: str, who: str = Depends(operator)):
    p = platform.gateway.decide(pid, True, who, platform.t)
    if p is None:
        raise HTTPException(409, "cannot approve: not pending, the link is down, or the device is offline")
    return p


@app.post("/api/commands/{pid}/reject")
async def reject(pid: str, who: str = Depends(operator)):
    p = platform.gateway.decide(pid, False, who, platform.t)
    if p is None:
        raise HTTPException(409, "cannot reject: not pending or unknown")
    return p


@app.post("/api/revoke-all")
async def revoke_all(who: str = Depends(operator)):
    platform.gateway.revoke_all(who, platform.t)
    return {"ok": True}


@app.post("/api/link")
async def link(body: Link, who: str = Depends(operator)):
    platform.gateway.set_link(body.up, who, platform.t)
    return {"link": platform.gateway.link_up}


@app.post("/api/devices/{device}/status")
async def device_status(device: str, body: DeviceStatus, who: str = Depends(operator)):
    """Revoke a device (it is disconnected and can no longer authenticate) or trust it again."""
    if body.status not in (ACTIVE, REVOKED):
        raise HTTPException(400, "status must be active or revoked")
    rec = await hub.set_status(device, body.status)
    if rec is None:
        raise HTTPException(404, "unknown device")
    platform.gateway.audit.append(platform.t, f"device-{body.status}", "-", "-", device, "", actor=who, mode="approval")
    return {"id": rec["id"], "status": rec["status"]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # ignored: the socket is read-only
    except WebSocketDisconnect:
        clients.discard(ws)


app.mount("/data", StaticFiles(directory=c.OUT), name="data")
if c.WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=c.WEB_DIST, html=True), name="web")
