"""The district server as a device of the control centre.

    python -m urban.node.district --unit district-1 --platform urban-center:8443 --server-name urban-center \\
        --pki /etc/urban/pki --sensors /run/urban/q/queues.json --relay /run/urban/relay.json

A district has its own key, enrols with a code like a junction unit (python -m urban.edge --keygen --unit
district-1) and talks to the centre over the same channel: the centre's challenge, a signed hello, then a
report every second. The report carries the queues its cameras measure (keyed by approach id; the centre takes
only those of the junctions routed through this district), its own health, and what its relay is doing.

It takes no commands: only junction units do, and the centre never addresses one to a district. Should one
arrive anyway it is refused and said so in the next report.

While the centre is unreachable the reports are kept (the last BUFFER seconds) and sent in order as soon as
the link is back, each with the time it was made, so the centre's history has no hole for the outage.
"""
import argparse
import asyncio
import json
import ssl
import time
from collections import deque
from pathlib import Path

import websockets

from ..devices import crypto
from ..devices.trust import GatewayTrust
from ..edge.__main__ import read_sensors
from ..edge.device import VERSION
from ..edge.probe import Probe

BUFFER = 900  # seconds of reports kept through an outage of the link to the centre
RETRY = 2     # seconds between connection attempts


class DistrictDevice:
    """Identity, reports and the outage buffer. No networking here (see District below), so it can be tested."""

    def __init__(self, device_id: str, key, root_public=None, gateway_public=None, gateway_state: Path | None = None,
                 probe=None, relay_stats=None, buffer: int = BUFFER):
        """relay_stats(): what the relay reports about itself (the kernel relay, os urban-relay, writes it), or None."""
        self.id, self.key = device_id, key
        self.trust = GatewayTrust(root_public, gateway_public, gateway_state)
        self.probe = probe
        self.relay_stats = relay_stats
        self.started = time.monotonic()
        self.buffer: deque[dict] = deque(maxlen=buffer)
        self.outbox: list[dict] = []
        self.refused = None

    def hello(self, nonce: str) -> dict:
        return {"type": "hello", "device": self.id, "version": VERSION, "epoch": self.trust.epoch,
                "sig": crypto.sign(self.key, {"device": self.id, "nonce": nonce})}

    def on_welcome(self, msg: dict) -> bool:
        """The centre presents its gateway certificate; a district follows rotations like a unit does, so a
        forged centre is refused the same way."""
        why = self.trust.offer(msg.get("gateway"))
        if why:
            self.refused = why
            return False
        return True

    def on_command(self, msg: dict):
        """A district obeys no command; one that arrives is reported, not executed."""
        self.outbox.append({"type": "edge", "status": "refused", "device": self.id,
                            "reason": "a district server takes no commands"})

    def health(self) -> dict:
        temp = self.probe().get("temp") if self.probe is not None else None
        relay = None
        if self.relay_stats is not None:
            try:
                relay = self.relay_stats()
            except Exception:
                relay = None
        return {"version": VERSION, "temp": temp, "uptime": round(time.monotonic() - self.started),
                "relay": relay, "buffered": len(self.buffer)}

    def make_report(self, queues: dict, now: int) -> dict:
        events, self.outbox = self.outbox, []
        return {"type": "report", "t": now, "queues": queues, "events": events, "epoch": self.trust.epoch,
                "health": self.health()}

    def tick(self, now: int, queues: dict, online: bool) -> list[dict]:
        """One second of the district's life: the reports to send now, oldest first. Offline, the report is
        kept instead and nothing is returned."""
        report = self.make_report(queues, now)
        if not online:
            self.buffer.append(report)
            return []
        out = list(self.buffer)
        self.buffer.clear()
        out.append(report)
        return out

    def keep(self, unsent: list[dict]):
        """The link broke while sending: these reports (the failed one and everything after it) go back in
        front of whatever was kept meanwhile, in order. If that is more than the buffer holds, the oldest go."""
        self.buffer = deque([*unsent, *self.buffer], maxlen=self.buffer.maxlen)


def _relay_reader(path: Path | None):
    if path is None:
        return None

    def read():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None
    return read


class District:
    """The district's link to the centre: one session, a report a second, the buffer through outages."""

    def __init__(self, args):
        pki = Path(args.pki)
        self.id = args.unit
        key = crypto.load_private(Path(args.key or pki / "edge" / f"{self.id}.key").read_text())
        gw_path = Path(args.gateway_pub or pki / "gateway.pub")
        gateway_public = crypto.load_public(gw_path.read_text()) if gw_path.exists() else None
        root_path = Path(args.root or pki / "root.pub")
        root_public = crypto.load_public(root_path.read_text()) if root_path.exists() else None
        if gateway_public is None and root_public is None:
            raise SystemExit(f"neither {root_path} nor {gw_path}: the district would not know the centre's key")
        self.dev = DistrictDevice(self.id, key, root_public, gateway_public, pki / "edge" / f"{self.id}.gateway.json",
                                  probe=Probe(args.temp, None), relay_stats=_relay_reader(Path(args.relay) if args.relay else None))
        self.sensors = Path(args.sensors) if args.sensors else None
        self.url = f"wss://{args.platform}/device"
        self.server_name = args.server_name
        self.tls_context = ssl.create_default_context(cafile=str(args.ca or pki / "ca.pem"))
        self.ws = None

    async def flush(self, ws, reports: list[dict]):
        """Send in order. When one fails, it and all that follow it are kept for the next session."""
        for i, report in enumerate(reports):
            try:
                await ws.send(json.dumps(report))
            except Exception:
                self.ws = None
                self.dev.keep(reports[i:])
                return

    async def ticker(self):
        while True:
            now = int(time.time())
            ws = self.ws
            await self.flush(ws, self.dev.tick(now, read_sensors(self.sensors), ws is not None))
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
                        print(f"district: {self.dev.refused}: refusing to work", flush=True)
                        raise SystemExit(1)
                    print(f"district: {self.id} online at the centre, gateway key epoch {self.dev.trust.epoch}, "
                          f"{len(self.dev.buffer)} report(s) kept through the outage", flush=True)
                    self.ws = ws
                    async for text in ws:
                        msg = json.loads(text)
                        if isinstance(msg, dict) and msg.get("type") == "cmd":
                            self.dev.on_command(msg)
            except websockets.ConnectionClosed as err:
                code = err.rcvd.code if err.rcvd is not None else None
                print(f"district: session closed ({code}), reconnecting", flush=True)
            except (OSError, websockets.WebSocketException, ConnectionError, json.JSONDecodeError, KeyError,
                    TypeError, AttributeError) as err:
                print(f"district: centre unreachable ({err.__class__.__name__}), retrying", flush=True)
            finally:
                self.ws = None
            await asyncio.sleep(RETRY)

    async def run(self):
        print(f"district: {self.id}, centre {self.url}", flush=True)
        await asyncio.gather(self.ticker(), self.session())


def main():
    ap = argparse.ArgumentParser(description="URBAN district server: its link to the control centre")
    ap.add_argument("--unit", required=True, help="the district's device id, e.g. district-1")
    ap.add_argument("--platform", required=True, help="host:port of the centre's device listener")
    ap.add_argument("--server-name", required=True, help="name on the centre's certificate")
    ap.add_argument("--pki", required=True, help="folder with edge/<id>.key, ca.pem and root.pub")
    ap.add_argument("--sensors", help="JSON file {approach id: metres} rewritten by the district's camera vision")
    ap.add_argument("--relay", help="the relay's stats file (/run/urban/relay.json, written by urban-relay)")
    ap.add_argument("--temp", help="temperature file; default: the board's own thermal zone")
    ap.add_argument("--key"), ap.add_argument("--ca"), ap.add_argument("--gateway-pub"), ap.add_argument("--root")
    args = ap.parse_args()
    try:
        asyncio.run(District(args).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
