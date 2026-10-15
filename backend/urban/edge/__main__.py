"""Runs URBAN Edge units as a separate process.

Demo fleet (the platform in simulation mode starts it by itself):
    python -m urban.edge                  every enrolled unit, one session each
    python -m urban.edge --only J-21      a single unit
All demo units share one local field link to the simulation, which stands for the cameras
and the controller in each cabinet.

One real unit, on its own computer in a cabinet (the platform in field mode):
    python -m urban.edge --unit edge-J-21 --platform city.example:8443 --adapter relay --controller /dev/ttyUSB0
    python -m urban.edge --unit edge-J-21 --platform city.example:8443 --adapter ntcip --controller 192.168.10.2:161
Trust: the unit carries the platform's ca.pem (whose TLS it accepts) and root.pub (whose gateway key it
obeys; the key itself comes in a certificate at session start, and rotations follow, never back).
Before the first start the unit makes its own key; only the public half leaves it:
    python -m urban.edge --keygen --unit edge-J-21 [--pki /etc/urban/pki]
and the administrator enrols that public key on the platform (python -m urban.provision enroll ...).
It needs its key, the CA of the platform and the gateway's public key (--pki or --key/--ca/--gateway-pub),
learns its junction and the approved plan table from the platform at session start, ticks on its own
wall clock, and reads queue lengths from --sensors (a JSON file {approach id: metres} that the camera
pipeline rewrites). Without --sensors it reports no queues; with --adapter none it refuses every plan.
Transports: relay = Modbus RTU coils on a serial relay board (needs pyserial, `pip install -e "backend[edge]"`),
ntcip = SNMP SetRequest to the controller: SNMPv3 authPriv by default, with SNMP_USER, SNMP_AUTH_PASS,
SNMP_PRIV_PASS (and SNMP_AUTH=sha|sha256) from the environment, never from the command line;
--snmp-version 1 or 0 falls back to a community string for an isolated link. Neither transport has been
tried on real hardware yet.

Every unit keeps its own outbound TLS session to the platform, authenticates with its own key
and obeys only commands signed by the gateway.
"""
import argparse
import asyncio
import json
import os
import ssl
import time
import traceback
from pathlib import Path

import websockets

from .. import config as c
from ..devices import crypto
from ..devices.registry import device_id
from ..equipment.adapter import NoController
from ..equipment.modbus import ModbusRelayBoard
from ..equipment.ntcip import NtcipAdapter
from ..equipment.relay import RelayAdapter
from ..equipment.snmp import SnmpClient, SnmpV3Client
from ..junctions import approved_plans, load_all_junctions
from .device import EdgeDevice
from ..sources.field import SUPERSEDED
from .field import FieldAdapter

RETRY = 2            # seconds between connection attempts
FIELD_PATIENCE = 20  # seconds without the field link before a demo fleet gives up


class Fleet:
    def __init__(self, only: str | None):
        junctions = [j for j in load_all_junctions(c.OUT) if only in (None, j["id"])]  # the demo twin
        gateway_public = crypto.load_public((c.PKI / "gateway.pub").read_text())
        root_public = crypto.load_public((c.PKI / "root.pub").read_text()) if (c.PKI / "root.pub").exists() else None
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
                                                script=scripts.get(j["tls"]), root_public=root_public,
                                                gateway_state=c.PKI / "edge" / f"{ident}.gateway.json")
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


def parse_endpoint(text: str, default_port: int) -> tuple[str, int]:
    """host, [host]:port, host:port or an IPv6 literal -> (host, port)."""
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest[1:] or default_port) if rest.startswith(":") else default_port
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host, int(port or default_port)
    return text, default_port  # a bare name or a bare IPv6 address


def make_adapter(args):
    """The controller link of this unit, from the command line. Plan tables come later, from the platform's welcome."""
    kind, controller = args.adapter, args.controller
    if kind == "none":
        return NoController()
    if not controller:
        raise SystemExit(f"--adapter {kind} needs --controller (relay: serial port of the board; ntcip: host[:port])")
    if kind == "relay":
        board = ModbusRelayBoard(controller, unit=args.relay_unit, baud=args.baud)
        return RelayAdapter(board.write, {}, relays=args.relays)
    host, port = parse_endpoint(controller, 161)
    if args.snmp_version == 3:
        user, auth_pass, priv_pass = (os.getenv(k, "") for k in ("SNMP_USER", "SNMP_AUTH_PASS", "SNMP_PRIV_PASS"))
        if not (user and auth_pass and priv_pass):
            raise SystemExit("SNMPv3 needs SNMP_USER, SNMP_AUTH_PASS and SNMP_PRIV_PASS in the environment "
                             "(the unit's env file); --snmp-version 1 uses a community string instead")
        try:
            client = SnmpV3Client(host, port, user=user, auth_password=auth_pass, priv_password=priv_pass,
                                  auth=os.getenv("SNMP_AUTH", "sha"))
        except ValueError as err:
            raise SystemExit(f"SNMPv3: {err}")
    else:
        client = SnmpClient(host, port, community=args.community, version=args.snmp_version)
    return NtcipAdapter(lambda tls, oid, value: client.set(oid, value), {}, backup_time=args.backup_time)


class Unit:
    """One real URBAN Edge unit: wall clock, sensors from a file, a controller adapter, one session."""

    def __init__(self, args):
        pki = Path(args.pki)
        self.id = args.unit
        key = crypto.load_private(Path(args.key or pki / "edge" / f"{self.id}.key").read_text())
        gw_path = Path(args.gateway_pub or pki / "gateway.pub")
        gateway_public = crypto.load_public(gw_path.read_text()) if gw_path.exists() else None
        root_path = Path(args.root or pki / "root.pub")
        root_public = crypto.load_public(root_path.read_text()) if root_path.exists() else None
        if gateway_public is None and root_public is None:
            raise SystemExit(f"neither {root_path} nor {gw_path}: the unit would not know whose commands to obey")
        self.adapter = make_adapter(args)
        self.dev = EdgeDevice(self.id, args.junction, key, gateway_public, self.adapter,
                              state_path=Path(args.state or pki / "edge" / f"{self.id}.seq"),
                              root_public=root_public, gateway_state=pki / "edge" / f"{self.id}.gateway.json")
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
                    if not isinstance(challenge, dict):
                        raise ConnectionError("no challenge")
                    await ws.send(json.dumps(self.dev.hello(challenge["nonce"])))
                    welcome = json.loads(await ws.recv())
                    if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                        raise ConnectionError("not welcome")
                    if not self.dev.on_welcome(welcome):
                        why = getattr(self.dev, "refused", None) or (
                            f"the platform enrols {self.id} for junction {welcome.get('tls')}, "
                            f"this unit is configured for {self.dev.tls}")
                        print(f"edge: {why}: refusing to work", flush=True)
                        raise SystemExit(1)  # a unit that refuses to work says so to systemd
                    print(f"edge: {self.id} online at {self.dev.tls}, approved plans "
                          f"{sorted(self.adapter.plans(self.dev.tls))}, gateway key epoch {self.dev.trust.epoch}", flush=True)
                    self.ws = ws
                    async for text in ws:
                        msg = json.loads(text)
                        if isinstance(msg, dict) and msg.get("type") == "cmd":
                            self.dev.on_command(msg)
            except (OSError, websockets.WebSocketException, ConnectionError, json.JSONDecodeError, KeyError,
                    TypeError, AttributeError) as err:
                print(f"edge: platform unreachable ({err.__class__.__name__}), retrying", flush=True)
            finally:
                self.ws = None
            await asyncio.sleep(RETRY)

    async def run(self):
        print(f"edge: unit {self.id}, platform {self.url}, adapter {self.adapter.__class__.__name__}", flush=True)
        await asyncio.gather(self.ticker(), self.session())


def keygen(args):
    """Make this unit's key on the unit. Refuses to overwrite one: a new key means re-enrolment."""
    if not args.unit:
        raise SystemExit("--keygen needs --unit <device id>, e.g. edge-J-21")
    path = Path(args.key or Path(args.pki) / "edge" / f"{args.unit}.key")
    if path.exists():
        raise SystemExit(f"{path} exists; remove it on purpose if this unit really needs a new key")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = crypto.new_key()
    path.write_text(crypto.private_hex(key))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    public = crypto.public_hex(key)
    print(f"private key: {path} (stays on this unit)")
    print(f"public key:  {public}")
    print(f"fingerprint: {crypto.fingerprint(public)}")
    print(f"on the platform: python -m urban.provision enroll {args.unit} --junction <id> --public {public}")
    print("this unit also needs the platform's ca.pem and root.pub next to its key")


def main():
    ap = argparse.ArgumentParser(description="URBAN Edge units")
    ap.add_argument("--keygen", action="store_true", help="make this unit's key and print its public half, then exit")
    ap.add_argument("--only", help="demo fleet: run a single unit, by junction id (for example J-21)")
    ap.add_argument("--keep-running", action="store_true", help="demo fleet: do not stop when the field link disappears")
    ap.add_argument("--unit", help="one real unit: its device id (for example edge-J-21)")
    ap.add_argument("--platform", default=f"127.0.0.1:{c.DEVICE_PORT}", help="host:port of the platform's device listener")
    ap.add_argument("--server-name", default="localhost", help="name on the platform's certificate")
    ap.add_argument("--junction", help="tls id this unit is installed at; checked against the platform")
    ap.add_argument("--adapter", choices=("none", "relay", "ntcip"), default="none")
    ap.add_argument("--controller", help="relay: serial port of the relay board; ntcip: host[:port] of the controller")
    ap.add_argument("--relay-unit", type=int, default=1, help="relay: Modbus unit id of the board")
    ap.add_argument("--relays", type=int, default=3, help="relay: coils used for the plan code")
    ap.add_argument("--baud", type=int, default=9600, help="relay: serial speed")
    ap.add_argument("--community", default="private", help="ntcip with --snmp-version 0/1: community with write access")
    ap.add_argument("--snmp-version", type=int, choices=(0, 1, 3), default=3,
                    help="ntcip: 3 = SNMPv3 authPriv (default; credentials from SNMP_* env), 1 = v2c, 0 = v1")
    ap.add_argument("--backup-time", type=int, default=180, help="ntcip: unitBackupTime set before the first plan, seconds")
    ap.add_argument("--sensors", help="JSON file {approach id: metres} rewritten by the camera pipeline")
    ap.add_argument("--pki", default=str(c.PKI), help="folder with ca.pem, gateway.pub and edge/<unit>.key")
    for name in ("--key", "--ca", "--gateway-pub", "--root", "--state"):
        ap.add_argument(name)
    args = ap.parse_args()
    if args.keygen:
        keygen(args)
        return
    runner = Unit(args) if args.unit else Fleet(args.only)  # configuration errors stop here, with their message
    try:
        asyncio.run(runner.run() if args.unit else runner.run(args.keep_running))
    except KeyboardInterrupt:
        pass
    except SystemExit as stop:
        if stop.code not in (None, 0):
            raise


if __name__ == "__main__":
    main()
