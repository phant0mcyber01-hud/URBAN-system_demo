"""HTTP/WebSocket API and static UI.

    python -m urban   ->  http://localhost:8080

Reading is open on this machine. Anything that changes state (approve or reject a proposed
command, kill switch, cutting the demo link) needs the operator token printed at startup.
Data flow: source thread -> bus(source.frame) -> Platform.handle -> bus(platform.frame) -> WebSocket.
Operator actions go to the Command Gateway, the only path towards (emulated) equipment.
"""
import asyncio
import hmac
import json
import os
import queue
import secrets
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
from ..platform import Platform
from ..sources import sumo
from .cameras import load_all_junctions, load_cameras

OPERATOR_TOKEN = os.getenv("URBAN_OPERATOR_TOKEN") or secrets.token_urlsafe(9)
ACTOR = "operator"


equipment: queue.Queue = queue.Queue()  # gateway -> emulated signal controllers
bus = Bus()
meta = json.loads((c.OUT / "meta.json").read_text())
# queue detectors on every signalised junction; 8 of them also have a camera window in the UI
platform = Platform(meta["center"], meta["radius"], load_all_junctions() or load_cameras(), send=equipment.put)
clients: set[WebSocket] = set()


async def analyse(raw: dict):
    await bus.publish(PLATFORM_FRAME, platform.handle(raw))


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
            sumo.run(publish, equipment)
        except SystemExit:
            return
        except Exception:
            traceback.print_exc()
            stop.wait(5)


@asynccontextmanager
async def lifespan(_):
    print(f"\n  Operator token: {OPERATOR_TOKEN}\n  (viewing needs no token; approving commands does)\n", flush=True)
    stop = threading.Event()
    threading.Thread(target=source_thread, args=(asyncio.get_running_loop(), stop), daemon=True).start()
    yield
    stop.set()


app = FastAPI(lifespan=lifespan, title="URBAN platform API")
app.add_middleware(GZipMiddleware, minimum_size=2000)


@app.middleware("http")
async def revalidate_data(request, call_next):
    """Map layers are regenerated in place by the pipeline: browsers must revalidate them (ETag), not guess."""
    response = await call_next(request)
    if request.url.path.startswith("/data/"):
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
    return {"ok": True, "t": platform.t}


@app.get("/api/state")
def state():
    return platform.snapshot()


@app.get("/api/audit")
def audit():
    return {**platform.audit(), "intact": platform.gateway.verify(), "records": platform.gateway.records[-50:]}


@app.get("/api/me")
def me(who: str = Depends(operator)):
    return {"operator": True}


class Link(BaseModel):
    up: bool


# async handlers run on the event loop, the same thread that runs Platform.handle: no races on gateway state
@app.post("/api/commands/{pid}/approve")
async def approve(pid: str, who: str = Depends(operator)):
    p = platform.gateway.decide(pid, True, who, platform.t)
    if p is None:
        raise HTTPException(409, "cannot approve: not pending, unknown, or the link is down")
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
