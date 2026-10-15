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
import time
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config as c
from ..accounts import Accounts, CodeRequired, allows
from ..core.bus import PLATFORM_FRAME, SOURCE_FRAME, Bus
from ..devices import crypto
from ..devices.hub import DeviceHub
from ..devices.registry import ACTIVE, REVOKED, Registry, provision
from ..city.passport import PassportError, Passports, problems
from ..junctions import APPROVED, DRAFT, _junction, approved, approved_plans, load_all_junctions, load_cameras
from ..platform import Platform
from ..sources import clock
from ..sources.field import FieldEmulator
from ..export import Exporter, sinks_from_env
from ..store import Store

FIELD = c.SOURCE == "field"  # real units, no simulation, no demo fleet, no field link
SPAWN_EDGE = os.getenv("URBAN_EDGE", "auto") != "manual" and not FIELD  # manual: start `python -m urban.edge` yourself

c.MAP.mkdir(parents=True, exist_ok=True)
junctions = load_all_junctions() or load_cameras()
passports = Passports(c.MAP / "signals.json")
# the demo generates every unit's key here; in field mode units bring their own and are enrolled one by one
provision(junctions, c.PKI, host=c.DEVICE_NAME, demo_devices=not FIELD)
FIELD_TOKEN = secrets.token_urlsafe(24)  # new on every start; the Edge process reads it from the pki folder
(c.PKI / "field.token").write_text(FIELD_TOKEN)

accounts = Accounts(c.USERS, mfa_roles=c.MFA_ROLES)
FIRST_PASSWORD = accounts.bootstrap()

bus = Bus()
field = FieldEmulator(FIELD_TOKEN)
def _meta() -> dict:
    """The city: name, centre and radius of the map. Before `python -m urban.city create` only the area from .env."""
    path = c.MAP / "meta.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    w, s, e, n = (float(v) for v in c.BBOX.split(","))
    return {"name": "", "center": [(w + e) / 2, (s + n) / 2], "radius": 1200}


meta = _meta()
GATEWAY_CERT = json.loads((c.PKI / "gateway.cert.json").read_text())
hub = DeviceHub(Registry(c.PKI / "devices.json"), crypto.load_private((c.PKI / "gateway.key").read_text()),
                on_report=lambda rec, report: platform.on_report(rec["tls"], report), clock=lambda: platform.t,
                plans=approved_plans(junctions), gateway_cert=GATEWAY_CERT)
store = Store(c.DB, mode=c.SOURCE, retention_days=c.HISTORY_DAYS)
platform = Platform(meta["center"], meta["radius"], junctions, send=hub.send, reachable=hub.online, store=store,
                    corridor=c.GREEN_CORRIDOR)
exporter = Exporter(store, crypto.load_private((c.PKI / "audit.key").read_text()), sinks_from_env(c.STATE))
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

    if FIELD:
        clock.run(publish, stop)
        return
    from ..sources import sumo  # the demo twin only: the real system does not need SUMO installed
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
        print(f"\n  Accounts: {len(accounts.users)} (manage with python -m urban.users)", flush=True)
    print(f"  History: {c.DB.name}, run #{store.run}, audit chain {store.chain['records']} records; "
          f"the full check runs in the background and every {CHAIN_CHECK // 60} min", flush=True)
    marker = c.STATE / "gateway.epoch"
    seen = int(marker.read_text()) if marker.exists() else None
    if seen != GATEWAY_CERT["epoch"]:
        if seen is not None:
            platform.gateway.audit.append(platform.t, "gateway-key-rotated", "-", "-", f"epoch {GATEWAY_CERT['epoch']}",
                                          crypto.fingerprint(GATEWAY_CERT["gateway"]), actor="provision", mode="approval")
        marker.write_text(str(GATEWAY_CERT["epoch"]))
    print(f"  Gateway key: epoch {GATEWAY_CERT['epoch']}, {crypto.fingerprint(GATEWAY_CERT['gateway'])}", flush=True)
    if FIELD:
        ready = sum(approved(j) for j in junctions)
        print(f"  City: {meta.get('name') or 'not set up (python -m urban.city create)'}, {len(junctions)} junctions, "
              f"{ready} with an approved passport\n  Mode: real equipment, waiting for URBAN Edge units\n", flush=True)
    else:
        print("  Mode: DEMO twin (SUMO, synthetic junctions, emulated Edge fleet)\n", flush=True)
    field.loop = asyncio.get_running_loop()
    stop = threading.Event()
    threading.Thread(target=source_thread, args=(field.loop, stop), daemon=True).start()
    watchdog = asyncio.create_task(edge_watchdog(stop)) if SPAWN_EDGE else None
    checker = asyncio.create_task(chain_checker())
    shipper = asyncio.create_task(exporter.loop()) if exporter.sinks else None
    print(f"  Audit copy: {', '.join(s.name for s in exporter.sinks)}" if exporter.sinks
          else "  Audit copy: none (set AUDIT_EXPORT or AUDIT_SYSLOG in .env)", flush=True)
    yield
    stop.set()
    checker.cancel()
    if shipper is not None:
        shipper.cancel()
    if watchdog is not None:
        watchdog.cancel()


CHAIN_CHECK = 900  # seconds between full verifications of the stored audit chain


async def chain_checker():
    """Verifies the whole stored chain off the event loop: once right after start, then periodically,
    so tampering with the database while the platform runs is seen within minutes, not at the next restart."""
    while True:
        result = await asyncio.to_thread(store.audit_verify)
        if not result["intact"]:
            print(f"AUDIT CHAIN BROKEN at record #{result['broken_at']}: the history file was changed outside the platform",
                  flush=True)
        await asyncio.sleep(CHAIN_CHECK)


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
    if FIELD:  # there are no emulated cables in field mode
        await ws.close(code=4404)
        return
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
    code: str = ""  # one-time code, for accounts with a second factor


class Code(BaseModel):
    code: str


class Current(BaseModel):
    current: str


class NewUser(BaseModel):
    name: str
    password: str
    role: str


class Password(BaseModel):
    password: str
    current: str = ""  # required when changing one's own password


class Enabled(BaseModel):
    enabled: bool


@app.post("/api/login")
async def login(body: Credentials, request: Request):
    try:
        session = accounts.sign_in(body.name, body.password, request.client.host if request.client else "", body.code)
    except CodeRequired:
        raise HTTPException(401, "code required")
    if session is None:
        platform.gateway.audit.append(platform.t, "login-failed", "-", "-", body.name[:40], "", actor="-", mode="approval")
        raise HTTPException(401, "wrong name or password, or too many attempts")
    platform.gateway.audit.append(platform.t, "login", "-", "-", session["name"], session["role"],
                                  actor=session["name"], mode="approval")
    return {"token": session["token"], "name": session["name"], "role": session["role"],
            "mfa_setup": session["mfa_setup"], "mfa": bool(accounts.users[session["name"]].get("totp"))}


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
    return {"name": s["name"], "role": s["role"], "mfa_setup": s["mfa_setup"],
            "mfa": bool(accounts.users.get(s["name"], {}).get("totp"))}


@app.post("/api/me/mfa/start")
async def mfa_start(body: Current, who: str = Depends(viewer)):
    """Begin enrolling a second factor; needs the current password, so a stolen session cannot do it."""
    if not accounts.check(who, body.current):
        raise HTTPException(400, "current password is wrong")
    return accounts.start_mfa(who)


@app.post("/api/me/mfa/confirm")
async def mfa_confirm(body: Code, who: str = Depends(viewer)):
    if not accounts.confirm_mfa(who, body.code):
        raise HTTPException(400, "wrong or expired code")
    platform.gateway.audit.append(platform.t, "mfa-enabled", "-", "-", who, "", actor=who, mode="approval")
    return {"ok": True}


@app.post("/api/users/{name}/mfa-reset")
async def mfa_reset(name: str, who: str = Depends(admin)):
    if name not in accounts.users:
        raise HTTPException(404, "unknown user")
    accounts.reset_mfa(name)
    platform.gateway.audit.append(platform.t, "mfa-reset", "-", "-", name, "", actor=who, mode="approval")
    return accounts.public(name)


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
    """Everyone may change their own password, knowing the current one; admins may reset anyone else's."""
    if name == who:
        if not accounts.check(who, body.current):
            raise HTTPException(400, "current password is wrong")
    elif not allows(accounts.role_of(who), "admin"):
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
    return {"ok": True, "t": platform.t, "mode": c.SOURCE, "devices_online": len(hub.sessions)}


@app.get("/api/state")
def state(who: str = Depends(viewer)):
    return {**platform.snapshot(), "mode": c.SOURCE, "corridor": platform.corridor_on, "city": meta.get("name", "")}


# ---- junction passports: what the engineer approved; drafts are watched, never commanded ----
def _passport_view(raw: dict) -> dict:
    return {**raw, "problems": problems(_junction(raw, DRAFT if FIELD else APPROVED))}


@app.get("/api/junctions")
def junction_list(who: str = Depends(viewer)):
    return [_passport_view(j) for j in passports.items]


class PassportEdit(BaseModel):
    name: str | None = None
    approaches: list[dict] = []
    phases: list[dict] = []


class Withdrawal(BaseModel):
    why: str = ""


async def _passport_changed(raw: dict, event: str, who: str, detail: str):
    """Apply a passport to the running platform, the gateway and the unit, and record it."""
    j = _junction(raw, DRAFT)
    platform.update_junction(j, event)
    await hub.set_plans(j["tls"], {pl["id"] for pl in j["plans"]} if approved(j) else None)
    platform.gateway.audit.append(platform.t, event, "-", "-", j["id"], detail, actor=who, mode="approval")


def _editable():
    if not FIELD:
        raise HTTPException(409, "demo twin: its junctions are synthetic and cannot be edited")


@app.post("/api/junctions/{ident}/passport")
async def passport_edit(ident: str, body: PassportEdit, who: str = Depends(admin)):
    _editable()
    change = {"approaches": body.approaches, "phases": body.phases, **({"name": body.name} if body.name else {})}
    try:
        raw = passports.edit(ident, change, who)
    except PassportError as err:
        raise HTTPException(400, str(err))
    await _passport_changed(raw, "passport-edited", who, f"version {raw['passport']['version']}, draft")
    return _passport_view(raw)


@app.post("/api/junctions/{ident}/approve")
async def passport_approve(ident: str, who: str = Depends(admin)):
    _editable()
    try:
        raw = passports.approve(ident, who)
    except PassportError as err:
        raise HTTPException(400, str(err))
    p = raw["passport"]
    await _passport_changed(raw, "passport-approved", who, f"version {p['version']}, hash {p['hash']}")
    return _passport_view(raw)


@app.post("/api/junctions/{ident}/withdraw")
async def passport_withdraw(ident: str, body: Withdrawal, who: str = Depends(admin)):
    _editable()
    try:
        raw = passports.withdraw(ident, who, body.why)
    except PassportError as err:
        raise HTTPException(400, str(err))
    await _passport_changed(raw, "passport-withdrawn", who, body.why[:120] or "back to draft")
    return _passport_view(raw)


@app.get("/api/audit")
def audit(who: str = Depends(viewer)):
    return {**platform.audit(), "intact": platform.gateway.verify(), "records": platform.gateway.records[-50:],
            "chain": store.audit_check()}


# ---- history: what the store kept across restarts; all read-only ----
@app.get("/api/history")
def history(who: str = Depends(viewer)):
    return {"counts": store.counts(), "run": store.run, "chain": store.audit_check(), "runs": store.runs(10),
            "export": exporter.status()}


@app.get("/api/history/commands")
def history_commands(tls: str | None = None, before: float | None = None, limit: int = 50, who: str = Depends(viewer)):
    return store.commands(tls, before, max(1, min(limit, 200)))


@app.get("/api/history/audit")
def history_audit(before: int | None = None, limit: int = 100, who: str = Depends(viewer)):
    return store.audit_page(before, max(1, min(limit, 500)))


@app.get("/api/history/devices/{device}")
def history_device(device: str, hours: float = 24, who: str = Depends(viewer)):
    return store.device_samples(device, time.time() - max(0.0, min(hours, 24 * 31)) * 3600)


@app.get("/api/history/queues/{junction}")
def history_queues(junction: str, hours: float = 24, who: str = Depends(viewer)):
    return store.queues(junction, time.time() - max(0.0, min(hours, 24 * 31)) * 3600)


@app.get("/api/devices")
def devices(who: str = Depends(viewer)):
    """The registry with live health: enrolment data, session, last report and alerts per unit."""
    from ..devices.health import assess
    return [{**row, "alerts": assess(row)} for row in hub.status()]


class Link(BaseModel):
    up: bool


class DeviceStatus(BaseModel):
    status: str


class Enrolment(BaseModel):
    junction: str        # junction id, e.g. J-21
    public_key: str      # hex, printed by `python -m urban.edge --keygen` on the unit
    serial: str = ""


@app.post("/api/devices/{device}/enroll")
async def device_enroll(device: str, body: Enrolment, who: str = Depends(admin)):
    """Enrol a unit's public key (installation or a replaced box). The unit's private key never leaves it."""
    junction = next((j for j in junctions if j["id"] == body.junction), None)
    if junction is None:
        raise HTTPException(404, "unknown junction")
    try:
        rec = await hub.enroll(device, junction, body.public_key, body.serial)
    except ValueError as err:
        raise HTTPException(400, str(err))
    fp = crypto.fingerprint(rec["public_key"])
    platform.gateway.audit.append(platform.t, "device-enrolled", "-", "-", device, f"{body.junction} key {fp}",
                                  actor=who, mode="approval")
    return {"id": rec["id"], "junction": rec["junction"], "serial": rec["serial"], "fingerprint": fp}


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


class Group(BaseModel):
    ids: list[str]
    approve: bool


@app.post("/api/commands/group")
async def decide_group(body: Group, who: str = Depends(operator)):
    """Apply or reject a whole street at once; every proposal is still checked and recorded on its own."""
    if not body.ids:
        raise HTTPException(400, "no proposals")
    results = platform.gateway.decide_many(body.ids, body.approve, who, platform.t)
    if not any(results.values()):
        raise HTTPException(409, "none of these could be decided: not pending, the link is down, or devices offline")
    return results


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


app.mount("/data", StaticFiles(directory=c.MAP), name="data")
if c.WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=c.WEB_DIST, html=True), name="web")
