"""Runs URBAN Edge units as a separate process.

Demo fleet (the platform in simulation mode starts it by itself):
    python -m urban.edge                  every enrolled unit, one session each
    python -m urban.edge --only J-21      a single unit
All demo units share one local field link to the simulation, which stands for the cameras
and the controller in each cabinet.

One real unit, on its own computer in a cabinet (the platform in field mode):
    python -m urban.edge --unit edge-J-21 --platform city.example:8443 --adapter relay --controller /dev/ttyUSB0
It needs its key, the CA of the platform and the gateway's public key (--pki or --key/--ca/--gateway-pub),
learns its junction and the approved plan table from the platform at session start, ticks on its own
wall clock, and reads queue lengths from --sensors (a JSON file {approach id: metres} that the camera
pipeline rewrites). Without --sensors it reports no queues; with --adapter none it refuses every plan.

Every unit keeps its own outbound TLS session to the platform, authenticates with its own key
and obeys only commands signed by the gateway.
"""
import argparse
import asyncio
import json
import ssl
import time
import traceback
from pathlib import Path

import websockets

from .. import config as c
from ..devices import crypto
from ..devices.registry import device_id
from ..equipment.adapter import NoController
from ..equipment.relay import RelayAdapter
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
        # demo faults so the health view has something to show: one cabinet overheats, one gets opened
        scripts = {junctions[3]["tls"]: {"heat": (120, 540)}} if len(junctions) > 3 else {}
        if len(junctions) > 7:
            scripts[junctions[7]["tls"]] = {"door": (420, 500)}
        for j in junctions:
            ident = device_id(j["id"])
            key = crypto.load_private((c.PKI / "edge" / f"{ident}.key").read_text())
            self.devices[j["tls"]] = EdgeDevice(ident, j["tls"], key, gateway_public, adapter,
                                                state_path=c.PKI / "edge" / f"{ident}.seq",
                                                script=scripts.get(j["tls"]))
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
                    welcome = json.loads(await ws.recv())
                    if welcome.get("type") != "welcome" or not dev.on_welcome(welcome):
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


def read_sensors(path: Path | None) -> dict:
    """Queue lengths from the camera pipeline: {approach id: metres}. Anything unreadable counts as no data."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        try:
            out[str(k)] = max(0.0, float(v))
        except (TypeError, ValueError):
            pass
    return out


def make_adapter(kind: str, controller: str | None):
    if kind == "none":
        return NoController()
    if kind == "relay":
        if not controller:
            raise SystemExit("--adapter relay needs --controller <device path of the relay board>")
        board = Path(controller)

        def write(tls, states):  # one line per change; the board driver is whatever reads this device
            with board.open("a", encoding="utf-8") as f:
                f.write(tls + " " + "".join("1" if s else "0" for s in states) + "\n")
        return RelayAdapter(write, {})
    raise SystemExit(f"adapter {kind!r}: NTCIP needs an SNMP client on the unit, not implemented yet")


class Unit:
    """One real URBAN Edge unit: wall clock, sensors from a file, a controller adapter, one session."""

    def __init__(self, args):
        pki = Path(args.pki)
        self.id = args.unit
        key = crypto.load_private(Path(args.key or pki / "edge" / f"{self.id}.key").read_text())
        gateway_public = crypto.load_public(Path(args.gateway_pub or pki / "gateway.pub").read_text())
        self.adapter = make_adapter(args.adapter, args.controller)
        self.dev = EdgeDevice(self.id, args.junction, key, gateway_public, self.adapter,
                              state_path=Path(args.state or pki / "edge" / f"{self.id}.seq"))
        self.sensors = Path(args.sensors) if args.sensors else None
        self.url = f"wss://{args.platform}/device"
        self.server_name = args.server_name
        self.tls_context = ssl.create_default_context(cafile=str(args.ca or pki / "ca.pem"))
        self.ws = None

    async def ticker(self):
        while True:
            now = int(time.time())
            self.dev.tick(now)
            ws = self.ws
            if ws is not None:
                try:
                    await ws.send(json.dumps(self.dev.report(read_sensors(self.sensors))))
                except Exception:
                    self.ws = None
            await asyncio.sleep(max(0.0, (now + 1) - time.time()))

    async def session(self):
        while True:
            try:
                async with websockets.connect(self.url, ssl=self.tls_context, server_hostname=self.server_name) as ws:
                    challenge = json.loads(await ws.recv())
                    await ws.send(json.dumps(self.dev.hello(challenge["nonce"])))
                    welcome = json.loads(await ws.recv())
                    if welcome.get("type") != "welcome":
                        raise ConnectionError("not welcome")
                    if not self.dev.on_welcome(welcome):
                        print(f"edge: the platform enrols {self.id} for junction {welcome.get('tls')}, "
                              f"this unit is configured for {self.dev.tls}: refusing to work", flush=True)
                        raise SystemExit
                    print(f"edge: {self.id} online at {self.dev.tls}, approved plans "
                          f"{sorted(self.adapter.plans(self.dev.tls))}", flush=True)
                    self.ws = ws
                    async for text in ws:
                        msg = json.loads(text)
                        if msg.get("type") == "cmd":
                            self.dev.on_command(msg)
            except (OSError, websockets.WebSocketException, ConnectionError, json.JSONDecodeError, KeyError) as err:
                print(f"edge: platform unreachable ({err.__class__.__name__}), retrying", flush=True)
            finally:
                self.ws = None
            await asyncio.sleep(RETRY)

    async def run(self):
        print(f"edge: unit {self.id}, platform {self.url}, adapter {self.adapter.__class__.__name__}", flush=True)
        await asyncio.gather(self.ticker(), self.session())


def main():
    ap = argparse.ArgumentParser(description="URBAN Edge units")
    ap.add_argument("--only", help="demo fleet: run a single unit, by junction id (for example J-21)")
    ap.add_argument("--keep-running", action="store_true", help="demo fleet: do not stop when the field link disappears")
    ap.add_argument("--unit", help="one real unit: its device id (for example edge-J-21)")
    ap.add_argument("--platform", default=f"127.0.0.1:{c.DEVICE_PORT}", help="host:port of the platform's device listener")
    ap.add_argument("--server-name", default="localhost", help="name on the platform's certificate")
    ap.add_argument("--junction", help="tls id this unit is installed at; checked against the platform")
    ap.add_argument("--adapter", choices=("none", "relay", "ntcip"), default="none")
    ap.add_argument("--controller", help="relay: device path of the relay board")
    ap.add_argument("--sensors", help="JSON file {approach id: metres} rewritten by the camera pipeline")
    ap.add_argument("--pki", default=str(c.PKI), help="folder with ca.pem, gateway.pub and edge/<unit>.key")
    for name in ("--key", "--ca", "--gateway-pub", "--state"):
        ap.add_argument(name)
    args = ap.parse_args()
    try:
        asyncio.run(Unit(args).run() if args.unit else Fleet(args.only).run(args.keep_running))
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
