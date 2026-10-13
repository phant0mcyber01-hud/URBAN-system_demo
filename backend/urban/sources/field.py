"""The street, as URBAN Edge units see it in the demo: queue sensors and signal controllers.

The simulation thread calls step() once a simulated second. Edge units connect to serve()
over a local WebSocket: readings go out, controller operations come in. This stands for the
cables inside a cabinet. It listens on localhost only, refuses browsers (any Origin header)
and wants the field token the platform hands to the Edge process, so no other local client
can pull on the cables.

The controllers are emulated with their own loss-of-communication timer (the unitBackupTime
of NTCIP): a plan that is not confirmed for BACKUP_TIME seconds is dropped by the controller
itself, even if the Edge unit that asked for it has died.
"""
import asyncio
import hmac
import json
import queue

from fastapi import WebSocket, WebSocketDisconnect

from ..equipment.adapter import AdapterError, ControllerAdapter

BACKUP_TIME = 240  # seconds a controller keeps a commanded plan without confirmation
SUPERSEDED = 4409   # close code: another set of Edge units took over this field link


class ControllerBank:
    """Emulated controllers: an adapter plus the backup timer every real controller has."""

    def __init__(self, adapter: ControllerAdapter, backup_time: int = BACKUP_TIME):
        self.adapter, self.backup_time = adapter, backup_time
        self.confirmed: dict[str, int] = {}  # tls -> time of the last control write

    def apply(self, op: dict, now: int):
        tls = op.get("tls")
        try:
            if op.get("op") == "select":
                self.adapter.select(tls, op.get("plan"))
                self.confirmed[tls] = now
            elif op.get("op") == "keepalive" and tls in self.confirmed:
                self.confirmed[tls] = now
            elif op.get("op") == "base":
                self.adapter.base(tls)
                self.confirmed.pop(tls, None)
        except AdapterError:
            pass  # a controller ignores what it does not understand

    def tick(self, now: int) -> list[str]:
        """Controllers whose commanded plan timed out; they are back on their base plan."""
        lost = [tls for tls, at in self.confirmed.items() if now - at > self.backup_time]
        for tls in lost:
            self.adapter.base(tls)
            del self.confirmed[tls]
        return lost

    def reset(self):
        self.confirmed.clear()


class FieldEmulator:
    def __init__(self, token: str = ""):
        self.token = token
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ops: queue.Queue = queue.Queue()
        self.clients: set[WebSocket] = set()

    # ---- simulation thread
    def step(self, bank: ControllerBank, now: int, queues: dict, reset: bool = False):
        """queues: tls -> {approach edge id: metres}. Applies pending operations, sends the readings."""
        if reset:
            bank.reset()
            while not self.ops.empty():
                self.ops.get_nowait()
        while not self.ops.empty():
            bank.apply(self.ops.get_nowait(), now)
        bank.tick(now)
        if self.loop is not None and self.clients:
            frame = {"t": now, "queues": queues}
            if reset:
                frame["reset"] = True
            asyncio.run_coroutine_threadsafe(self._broadcast(json.dumps(frame)), self.loop)

    # ---- event loop
    async def _broadcast(self, text: str):
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.clients.discard(ws)

    async def serve(self, ws: WebSocket):
        if ws.headers.get("origin"):  # a web page is never a cable
            await ws.close(code=4403)
            return
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
            token = str(hello.get("token", "")) if isinstance(hello, dict) else ""
        except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError):
            return
        if not self.token or not hmac.compare_digest(token, self.token):
            await ws.close(code=4401)
            return
        self.loop = asyncio.get_running_loop()
        for other in list(self.clients):  # one set of units per street: the newest one wins
            self.clients.discard(other)
            try:
                await other.close(code=SUPERSEDED)
            except Exception:
                pass
        self.clients.add(ws)
        try:
            while True:
                op = json.loads(await ws.receive_text())
                if isinstance(op, dict):
                    self.ops.put(op)
        except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
            pass
        finally:
            self.clients.discard(ws)
