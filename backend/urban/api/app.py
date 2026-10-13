"""HTTP/WebSocket API, static UI and the device endpoint.

    python -m urban   ->  UI on http://localhost:8080, devices on wss://localhost:8443

Who may do what:
- nobody without an account: only /api/health and the login page are open;
- viewer: everything that reads (UI, /api/state, the read-only /ws);
- operator: also decides on proposals, pulls everything back, cuts the demo link;
- admin: also manages devices (revoke, restore) and accounts;
- an URBAN Edge unit connects to /device over TLS and is trusted only after it signs a
  challenge with the key enrolled in the registry.
The first start with no accounts creates `admin` and prints its one-time password.

Data flow up:   Edge units -> /device -> DeviceHub -> Platform (queue readings, lease events)
                simulation -> bus -> Platform.handle -> /ws (vehicles for the twin, analysis)
Data flow down: operator -> Command Gateway -> DeviceHub (signed) -> Edge unit -> controller
The local /field socket stands for the cables in a cabinet: sensors and controller of the demo.
"""
import asyncio
import json
import os
import secrets
import subprocess
import sys
import threading
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config as c
from ..accounts import Accounts, allows
from ..core.bus import PLATFORM_FRAME, SOURCE_FRAME, Bus
from ..devices import crypto
from ..devices.hub import DeviceHub
from ..devices.registry import ACTIVE, REVOKED, Registry, provision
from ..junctions import load_all_junctions, load_cameras
from ..platform import Platform
from ..sources import sumo
from ..sources.field import FieldEmulator

SPAWN_EDGE = os.getenv("URBAN_EDGE", "auto") != "manual"  # manual: start `python -m urban.edge` yourself

junctions = load_all_junctions() or load_cameras()
provision(junctions, c.PKI)  # demo convenience: a deployment enrols devices one by one
FIELD_TOKEN = secrets.token_urlsafe(24)  # new on every start; the Edge process reads it from the pki folder
(c.PKI / "field.token").write_text(FIELD_TOKEN)

accounts = Accounts(c.USERS)
FIRST_PASSWORD = accounts.bootstrap()

bus = Bus()
field = FieldEmulator(FIELD_TOKEN)
meta = json.loads((c.OUT / "meta.json").read_text())
hub = DeviceHub(Registry(c.PKI / "devices.json"), crypto.load_private((c.PKI / "gateway.key").read_text()),
                on_report=lambda rec, report: platform.on_report(rec["tls"], report), clock=lambda: platform.t)
platform = Platform(meta["center"], meta["radius"], junctions, send=hub.send, reachable=hub.online)
clients: set[WebSocket] = set()


async def analyse(raw: dict):
    frame = platform.handle(raw)
    if raw["t"] % 2 == 0 or frame.get("reset"):
        new = platform.devices(hub.status(), raw["t"])
        frame["dev"] = platform.dev
        if new:
            frame["devalerts"] = new
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
    if FIRST_PASSWORD:
        print(f"\n  First start: account 'admin' created, password: {FIRST_PASSWORD}\n"
              f"  Change it after signing in. More accounts: python -m urban.users add <name> --role operator\n", flush=True)
    else:
        print(f"\n  Accounts: {len(accounts.users)} (manage with python -m urban.users)\n", flush=True)
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

def _session(authorization: str) -> dict:
    scheme, _, token = (authorization or "").partition(" ")
    session = accounts.session(token) if scheme.lower() == "bearer" else None
    if session is None:
        raise HTTPException(401, "sign in required")
    return session


def role(needed: str):
    """Dependency: a signed-in account with at least this role. Returns the account name."""
    def check(authorization: str = Header(default="")):
        session = _session(authorization)
        if not allows(session["role"], needed):
            raise HTTPException(403, f"{needed} role required")
        return session["name"]
    return check


viewer, operator, admin = role("viewer"), role("operator"), role("admin")


class Credentials(BaseModel):
    name: str
    password: str


class NewUser(BaseModel):
    name: str
    password: str
    role: str


class Password(BaseModel):
    password: str


class Enabled(BaseModel):
    enabled: bool


@app.post("/api/login")
async def login(body: Credentials, request: Request):
    session = accounts.sign_in(body.name, body.password, request.client.host if request.client else "")
    if session is None:
        platform.gateway.audit.append(platform.t, "login-failed", "-", "-", body.name[:40], "", actor="-", mode="approval")
        raise HTTPException(401, "wrong name or password, or too many attempts")
    platform.gateway.audit.append(platform.t, "login", "-", "-", session["name"], session["role"],
                                  actor=session["name"], mode="approval")
    return {"token": session["token"], "name": session["name"], "role": session["role"]}


@app.post("/api/logout")
async def logout(authorization: str = Header(default="")):
    session = accounts.session(authorization.partition(" ")[2])
    if session:
        accounts.sign_out(session["token"])
        platform.gateway.audit.append(platform.t, "logout", "-", "-", session["name"], "", actor=session["name"], mode="approval")
    return {"ok": True}


@app.get("/api/me")
def me(authorization: str = Header(default="")):
    s = _session(authorization)
    return {"name": s["name"], "role": s["role"]}


@app.get("/api/users")
def users(who: str = Depends(admin)):
    return [accounts.public(n) for n in accounts.users]


@app.post("/api/users")
async def create_user(body: NewUser, who: str = Depends(admin)):
    try:
        rec = accounts.create(body.name, body.password, body.role, by=who)
    except ValueError as err:
        raise HTTPException(400, str(err))
    platform.gateway.audit.append(platform.t, "user-created", "-", "-", rec["name"], rec["role"], actor=who, mode="approval")
    return rec


@app.post("/api/users/{name}/password")
async def user_password(name: str, body: Password, who: str = Depends(viewer)):
    """Admins may reset anyone; everyone may change their own."""
    session_name = who
    if name != session_name and not allows(accounts.session_role(session_name), "admin"):
        raise HTTPException(403, "admin role required")
    if name not in accounts.users:
        raise HTTPException(404, "unknown user")
    try:
        accounts.set_password(name, body.password)
    except ValueError as err:
        raise HTTPException(400, str(err))
    platform.gateway.audit.append(platform.t, "password-changed", "-", "-", name, "", actor=who, mode="approval")
    return {"ok": True}


@app.post("/api/users/{name}/enabled")
async def user_enabled(name: str, body: Enabled, who: str = Depends(admin)):
    if name not in accounts.users:
        raise HTTPException(404, "unknown user")
    if name == who and not body.enabled:
        raise HTTPException(400, "you cannot disable yourself")
    accounts.set_enabled(name, body.enabled)
    platform.gateway.audit.append(platform.t, "user-enabled" if body.enabled else "user-disabled", "-", "-", name, "",
                                  actor=who, mode="approval")
    return accounts.public(name)


@app.get("/api/health")
def health():
    return {"ok": True, "t": platform.t, "devices_online": len(hub.sessions)}


@app.get("/api/state")
def state(who: str = Depends(viewer)):
    return platform.snapshot()


@app.get("/api/audit")
def audit(who: str = Depends(viewer)):
    return {**platform.audit(), "intact": platform.gateway.verify(), "records": platform.gateway.records[-50:]}


@app.get("/api/devices")
def devices(who: str = Depends(viewer)):
    """The registry with live health: enrolment data, session, last report and alerts per unit."""
    from ..devices.health import assess
    return [{**row, "alerts": assess(row)} for row in hub.status()]


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
async def device_status(device: str, body: DeviceStatus, who: str = Depends(admin)):
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
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
        token = str(hello.get("token", "")) if isinstance(hello, dict) else ""
        if accounts.session(token) is None:
            await ws.close(code=4401)
            return
    except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError):
        return
    clients.add(ws)
    try:
        while True:  # the socket is read-only; it only closes when the session ends
            try:
                await asyncio.wait_for(ws.receive_text(), 30)
            except asyncio.TimeoutError:
                if accounts.session(token) is None:
                    clients.discard(ws)
                    await ws.close(code=4401)
                    return
    except (WebSocketDisconnect, RuntimeError):
        clients.discard(ws)


app.mount("/data", StaticFiles(directory=c.OUT), name="data")
if c.WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=c.WEB_DIST, html=True), name="web")
