"""A lighting cabinet's link to the centre: the same session a junction unit keeps (TLS pinned to the platform's CA,
the centre's challenge signed with the device key, a welcome, then a report every REPORT_EVERY seconds), with the
cabinet's clock running beside it, so a lease ends on time while the link is down.

The status file (--status) is rewritten atomically at every report, linked or not. It holds the `lighting` object of
the report plus: "unit" (device id), "t" (epoch seconds), "link" ("online"/"offline"), "os" (system version or null),
"fault" (controller fault or null), "last_refused" (the last refusal's reason or null).
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
import traceback
from pathlib import Path

import websockets

from ...devices import crypto
from .device import EmulatedAdapter, LightingCabinet

REPORT_EVERY = 5     # seconds between reports
SEND_TIMEOUT = 5     # seconds a report may take to leave before the link counts as dead
RETRY_FIRST, RETRY_MAX = 2.0, 30.0


def write_status(path: Path | None, report: dict, cab: LightingCabinet, online: bool):
    """The machine's screen reads this file; it must never be seen half-written."""
    if path is None:
        return
    health = report.get("health", {})
    data = {**report["lighting"], "unit": cab.id, "t": report["t"], "link": "online" if online else "offline",
            "os": health.get("os"), "fault": None if health.get("controller") == "ok" else health.get("controller"),
            "last_refused": cab.last_refusal}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        # the service runs with UMask=0077; the machine's screen (root without capabilities) must still read this
        # file, which holds no secret
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as err:
        print(f"lighting: cannot write {path}: {err}", flush=True)  # the lamps go on; the journal says why


class CabinetSession:
    def __init__(self, cab: LightingCabinet, url: str, context: ssl.SSLContext | None, server_name: str | None,
                 status: Path | None = None, report_every: float = REPORT_EVERY, log=print):
        self.cab, self.url, self.context, self.server_name = cab, url, context, server_name
        self.status, self.report_every, self.log = status, report_every, log
        self.ws = None

    # ---- the clock and the reports: never wait for the link
    async def ticker(self):
        """The cabinet's own clock: leases end and the schedule follows the sun, with or without a link."""
        while True:
            self.cab.tick()
            await asyncio.sleep(1.0 - (time.time() % 1.0))

    async def reporter(self):
        while True:
            await self.report()
            await asyncio.sleep(self.report_every)

    async def report(self):
        rep = self.cab.report()
        ws = self.ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.send(json.dumps(rep)), SEND_TIMEOUT)
                self.cab.mark_sent(rep)
            except Exception:
                self.ws = None
        write_status(self.status, rep, self.cab, self.ws is not None)

    # ---- one authenticated session, again and again
    async def session(self):
        delay = RETRY_FIRST
        while True:
            try:
                kwargs = {"ssl": self.context, "server_hostname": self.server_name} if self.context else {}
                async with websockets.connect(self.url, **kwargs) as ws:
                    challenge = json.loads(await ws.recv())
                    if not isinstance(challenge, dict):
                        raise ConnectionError("no challenge")
                    await ws.send(json.dumps(self.cab.hello(challenge["nonce"])))
                    welcome = json.loads(await ws.recv())
                    if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                        raise ConnectionError("not welcome")
                    if not self.cab.on_welcome(welcome):
                        # a forged centre: obey nothing from it, but the lamps keep their schedule
                        self.log(f"lighting: {self.cab.id}: {self.cab.refused}: not working with this centre", flush=True)
                        delay = RETRY_MAX
                        raise ConnectionError("centre not trusted")
                    self.log(f"lighting: {self.cab.id} ({self.cab.cabinet}) online, gateway key epoch "
                             f"{self.cab.trust.epoch}", flush=True)
                    delay = RETRY_FIRST
                    self.ws = ws
                    async for text in ws:
                        msg = json.loads(text)
                        if isinstance(msg, dict) and msg.get("type") == "cmd":
                            self.cab.on_command(msg)
            except websockets.ConnectionClosed as err:
                code = err.rcvd.code if err.rcvd is not None else None
                self.log(f"lighting: session closed ({code}), reconnecting", flush=True)
            except (OSError, websockets.WebSocketException, ConnectionError, json.JSONDecodeError, KeyError,
                    TypeError, AttributeError, ValueError) as err:
                self.log(f"lighting: centre unreachable ({err.__class__.__name__}), retrying", flush=True)
            except Exception:  # one cabinet's trouble stays its own
                traceback.print_exc()
            finally:
                self.ws = None
            await asyncio.sleep(delay)
            delay = min(RETRY_MAX, delay * 2)

    async def run(self):
        await asyncio.gather(self.ticker(), self.reporter(), self.session())


def build_tls(pki: Path, ca: Path | None = None) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca or pki / "ca.pem"))  # pin our CA, nothing else


def load_trust(pki: Path, unit: str, key_path: Path | None = None, gateway_pub: Path | None = None,
               root: Path | None = None):
    """(device key, gateway public or None, root public or None) from the PKI folder, as a unit does."""
    key = crypto.load_private(Path(key_path or pki / "edge" / f"{unit}.key").read_text())
    gw_path = Path(gateway_pub or pki / "gateway.pub")
    gateway_public = crypto.load_public(gw_path.read_text()) if gw_path.exists() else None
    root_path = Path(root or pki / "root.pub")
    root_public = crypto.load_public(root_path.read_text()) if root_path.exists() else None
    if gateway_public is None and root_public is None:
        raise SystemExit(f"neither {root_path} nor {gw_path}: the cabinet would not know whose commands to obey")
    return key, gateway_public, root_public


async def run_demo_cabinets(platform_addr: str, server_name: str, ca_path, pki_dir, cabinets: list[dict]):
    """The demo twin's emulated cabinets, in the platform's own process: one session each to the platform's device
    listener, like a real cabinet. `cabinets` are registry records ({"id", "cabinet", "lon", "lat"}); each needs
    its key in <pki_dir>/edge/<id>.key (the demo provisioning makes them). Runs until cancelled; a cabinet that
    cannot start (no key, bad position) is skipped with a message."""
    pki = Path(pki_dir)
    context = build_tls(pki, Path(ca_path))
    sessions = []
    for i, rec in enumerate(cabinets):
        try:
            unit = rec["id"]
            key, gateway_public, root_public = load_trust(pki, unit)
            cab = LightingCabinet(unit, rec["cabinet"], key, gateway_public,
                                  EmulatedAdapter(failed=1 if i == 1 else 0), rec["lon"], rec["lat"],
                                  state_path=pki / "edge" / f"{unit}.seq", root_public=root_public,
                                  gateway_state=pki / "edge" / f"{unit}.gateway.json")
        except (KeyError, OSError, ValueError, SystemExit) as err:
            print(f"lighting demo: cabinet {rec.get('id')} not started ({err.__class__.__name__}: {err})", flush=True)
            continue
        sessions.append(CabinetSession(cab, f"wss://{platform_addr}/device", context, server_name))
    if sessions:
        print(f"lighting demo: {len(sessions)} emulated cabinet(s) -> wss://{platform_addr}/device", flush=True)
    await asyncio.gather(*(s.run() for s in sessions))
