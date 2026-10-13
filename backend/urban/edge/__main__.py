"""Runs URBAN Edge units as a separate process.

    python -m urban.edge                  every enrolled unit of the demo, one session each
    python -m urban.edge --only J-21      a single unit

Each unit keeps its own outbound TLS session to the platform, authenticates with its own
key and obeys only commands signed by the gateway. All units share one local field link to
the simulation, which stands for the cameras and the controller in each cabinet.
"""
import argparse
import asyncio
import json
import ssl
import time
import traceback

import websockets

from .. import config as c
from ..devices import crypto
from ..devices.registry import device_id
from ..junctions import approved_plans, load_all_junctions
from .device import EdgeDevice
from ..sources.field import SUPERSEDED
from .field import FieldAdapter

RETRY = 2            # seconds between connection attempts
FIELD_PATIENCE = 20  # seconds without the field link before a demo fleet gives up


class Fleet:
    def __init__(self, only: str | None):
        junctions = [j for j in load_all_junctions() if only in (None, j["id"])]
        gateway_public = crypto.load_public((c.PKI / "gateway.pub").read_text())
        self.field_ws = None
        adapter = FieldAdapter(self.field_send, approved_plans(junctions))
        self.devices = {}
        for j in junctions:
            ident = device_id(j["id"])
            key = crypto.load_private((c.PKI / "edge" / f"{ident}.key").read_text())
            self.devices[j["tls"]] = EdgeDevice(ident, j["tls"], key, gateway_public, adapter,
                                                state_path=c.PKI / "edge" / f"{ident}.seq")
        self.field_token = (c.PKI / "field.token").read_text().strip()  # the demo cable has a lock on it
        self.sessions: dict[str, object] = {}  # device id -> open websocket to the platform
        self.tls_context = ssl.create_default_context(cafile=str(c.PKI / "ca.pem"))  # pin our CA, nothing else
        self.field_seen = time.monotonic()

    # ---- field link: sensors in, controller operations out
    def field_send(self, msg: dict):
        ws = self.field_ws
        if ws is None:
            raise ConnectionError("field link is down")
        asyncio.ensure_future(self._field_write(ws, json.dumps(msg)))

    async def _field_write(self, ws, text):
        try:
            await ws.send(text)
        except Exception:
            pass

    async def field(self, keep_running: bool):
        url = f"ws://127.0.0.1:{c.PORT}/field"
        while True:
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    await ws.send(json.dumps({"token": self.field_token}))
                    self.field_ws = ws
                    async for text in ws:
                        self.field_seen = time.monotonic()
                        await self.on_field(json.loads(text))
                    if ws.close_code == SUPERSEDED:
                        print("edge: another set of units took over, stopping", flush=True)
                        raise SystemExit
            except (OSError, websockets.WebSocketException):
                pass
            except Exception:  # a bad frame must not take every unit down
                traceback.print_exc()
            finally:
                self.field_ws = None
            if not keep_running and time.monotonic() - self.field_seen > FIELD_PATIENCE:
                print("edge: the field link is gone, stopping", flush=True)
                raise SystemExit
            await asyncio.sleep(RETRY)

    async def on_field(self, frame: dict):
        if frame.get("reset"):
            for dev in self.devices.values():
                dev.reset()
        now, queues = frame["t"], frame.get("queues", {})
        for tls, dev in self.devices.items():
            dev.tick(now)
            ws = self.sessions.get(dev.id)
            if ws is not None:  # offline units keep their leases running and their events for later
                asyncio.ensure_future(self._report(dev, ws, json.dumps(dev.report(queues.get(tls, {})))))

    async def _report(self, dev, ws, text):
        try:
            await ws.send(text)
        except Exception:
            self.sessions.pop(dev.id, None)

    # ---- one authenticated session per unit
    async def session(self, dev: EdgeDevice):
        url = f"wss://127.0.0.1:{c.DEVICE_PORT}/device"
        while True:
            try:
                async with websockets.connect(url, ssl=self.tls_context, server_hostname="localhost") as ws:
                    challenge = json.loads(await ws.recv())
                    await ws.send(json.dumps(dev.hello(challenge["nonce"])))
                    if json.loads(await ws.recv()).get("type") != "welcome":
                        raise ConnectionError("not welcome")
                    self.sessions[dev.id] = ws
                    async for text in ws:
                        msg = json.loads(text)
                        if msg.get("type") == "cmd":
                            dev.on_command(msg)
            except (OSError, websockets.WebSocketException, ConnectionError, json.JSONDecodeError, KeyError):
                pass
            except Exception:  # one unit's trouble stays its own
                traceback.print_exc()
            finally:
                self.sessions.pop(dev.id, None)
            await asyncio.sleep(RETRY)

    async def run(self, keep_running: bool):
        print(f"edge: {len(self.devices)} unit(s), platform wss://127.0.0.1:{c.DEVICE_PORT}", flush=True)
        await asyncio.gather(self.field(keep_running), *(self.session(d) for d in self.devices.values()))


def main():
    ap = argparse.ArgumentParser(description="URBAN Edge units for the demo")
    ap.add_argument("--only", help="run a single unit, by junction id (for example J-21)")
    ap.add_argument("--keep-running", action="store_true", help="do not stop when the field link disappears")
    args = ap.parse_args()
    try:
        asyncio.run(Fleet(args.only).run(args.keep_running))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
