"""A pump station's link to the centre: the same session a lighting cabinet keeps (TLS pinned to the platform's CA, the
centre's challenge signed with the device key, a welcome, then a report every REPORT_EVERY seconds), with the station's
readings taken beside it about once a second, link or not.

The status file (--status; /run/urban/water/water.json on URBAN OS) is rewritten atomically at every report, linked
or not. It holds the `water` object of the report plus: "unit" (device id), "t" (epoch seconds), "link"
("online"/"offline"), "os" (system version or null), "fault" (the source error or null), "last_refused" (the last
refusal's reason or null), "lon", "lat" (as the unit was told, or null).

The TLS and trust helpers are this module's own copies of the lighting cabinet's: the water module imports nothing from
a module that can switch anything.
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
from ...devices.signer import SignerError, load_signer
from .adapters import EmulatedStation
from .device import WaterStation
from .observe import Observations

REPORT_EVERY = 5     # seconds between reports
POLL_EVERY = 1.0     # seconds between readings of the station: no more often
SEND_TIMEOUT = 5     # seconds a report may take to leave before the link counts as dead
RETRY_FIRST, RETRY_MAX = 2.0, 30.0


def write_status(path: Path | None, report: dict, st: WaterStation, online: bool):
    """The machine's screen reads this file; it must never be seen half-written."""
    if path is None:
        return
    health = report.get("health", {})
    data = {**report["water"], "unit": st.id, "t": report["t"], "link": "online" if online else "offline",
            "os": health.get("os"), "fault": st.source_error, "last_refused": st.last_refusal,
            "lon": st.lon, "lat": st.lat}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # the service runs with UMask=0077; the machine's screen (root without capabilities) must still read this
        # file, which holds no secret
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as err:
        print(f"water: cannot write {path}: {err}", flush=True)  # the readings go on; the journal says why


class StationSession:
    def __init__(self, st: WaterStation, url: str, context: ssl.SSLContext | None, server_name: str | None,
                 status: Path | None = None, report_every: float = REPORT_EVERY, poll_every: float = POLL_EVERY,
                 log=print):
        self.st, self.url, self.context, self.server_name = st, url, context, server_name
        self.status, self.report_every, self.poll_every, self.log = status, report_every, poll_every, log
        self.ws = None

    # ---- the readings and the reports: never wait for the link
    async def poll(self):
        # a Modbus read blocks for up to its timeout: in a thread, so the session and the reports go on meanwhile
        await asyncio.to_thread(self.st.poll)

    async def poller(self):
        while True:
            started = time.monotonic()
            await self.poll()
            await asyncio.sleep(max(0.0, self.poll_every - (time.monotonic() - started)))

    async def reporter(self):
        while True:
            await self.report()
            await asyncio.sleep(self.report_every)

    async def report(self):
        rep = self.st.report()
        ws = self.ws
        if ws is not None:
            try:
                await asyncio.wait_for(ws.send(json.dumps(rep)), SEND_TIMEOUT)
                self.st.mark_sent(rep)
            except Exception:
                self.ws = None
        write_status(self.status, rep, self.st, self.ws is not None)

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
                    await ws.send(json.dumps(await asyncio.to_thread(self.st.hello, challenge["nonce"])))  # a TPM signs in a subprocess
                    welcome = json.loads(await ws.recv())
                    if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                        raise ConnectionError("not welcome")
                    if not self.st.on_welcome(welcome):
                        # a forged centre: nothing goes to it, the station keeps reading for its own screen
                        self.log(f"water: {self.st.id}: {self.st.refused}: not working with this centre", flush=True)
                        delay = RETRY_MAX
                        raise ConnectionError("centre not trusted")
                    self.log(f"water: {self.st.id} ({self.st.site}) online, gateway key epoch {self.st.trust.epoch}",
                             flush=True)
                    delay = RETRY_FIRST
                    self.ws = ws
                    async for text in ws:
                        msg = json.loads(text)
                        if isinstance(msg, dict) and msg.get("type") == "cmd":
                            self.st.on_command(msg)  # refused, unless it is an OS update offer
            except websockets.ConnectionClosed as err:
                code = err.rcvd.code if err.rcvd is not None else None
                self.log(f"water: session closed ({code}), reconnecting", flush=True)
            except (OSError, websockets.WebSocketException, ConnectionError, json.JSONDecodeError, KeyError,
                    TypeError, AttributeError, ValueError) as err:
                self.log(f"water: centre unreachable ({err.__class__.__name__}), retrying", flush=True)
            except SignerError as err:  # the TPM did not sign (busy, stuck): try again, the readings go on
                self.log(f"water: cannot sign with the device key ({err}), retrying", flush=True)
            except Exception:  # one station's trouble stays its own
                traceback.print_exc()
            finally:
                self.ws = None
            await asyncio.sleep(delay)
            delay = min(RETRY_MAX, delay * 2)

    async def run(self):
        await self.poll()  # the first report already carries readings
        try:
            await asyncio.gather(self.poller(), self.reporter(), self.session())
        finally:
            self.st.close()


def build_tls(pki: Path, ca: Path | None = None) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca or pki / "ca.pem"))  # pin our CA, nothing else


def load_trust(pki: Path, unit: str, key_path: Path | None = None, gateway_pub: Path | None = None,
               root: Path | None = None):
    """(device signer, gateway public or None, root public or None) from the PKI folder, as a unit does."""
    key = load_signer(pki, unit, Path(key_path) if key_path else None)  # its TPM key, else its Ed25519 file
    gw_path = Path(gateway_pub or pki / "gateway.pub")
    gateway_public = crypto.load_public(gw_path.read_text()) if gw_path.exists() else None
    root_path = Path(root or pki / "root.pub")
    root_public = crypto.load_public(root_path.read_text()) if root_path.exists() else None
    if gateway_public is None and root_public is None:
        raise SystemExit(f"neither {root_path} nor {gw_path}: the station would not know whose updates to take")
    return key, gateway_public, root_public


async def run_demo_stations(platform_addr: str, server_name: str, ca_path, pki_dir, stations: list[dict]):
    """The demo twin's emulated pump stations, in the platform's own process: one session each to the platform's device
    listener, like a real station. `stations` are registry records ({"id", "site", "lon", "lat"}); each needs its key in
    <pki_dir>/edge/<id>.key (the demo provisioning makes them). The second one has a pump in fault. Runs until
    cancelled; a station that cannot start (no key) is skipped with a message."""
    pki = Path(pki_dir)
    context = build_tls(pki, Path(ca_path))
    sessions = []
    for i, rec in enumerate(stations):
        try:
            unit = rec["id"]
            key, gateway_public, root_public = load_trust(pki, unit)
            st = WaterStation(unit, rec["site"], key, gateway_public, EmulatedStation(fault_pump=1 if i == 1 else None),
                              state_path=pki / "edge" / f"{unit}.seq", root_public=root_public,
                              gateway_state=pki / "edge" / f"{unit}.gateway.json",
                              observations=Observations(pki / "edge" / f"{unit}.water.json"),
                              lon=rec.get("lon"), lat=rec.get("lat"))
        except (KeyError, OSError, ValueError, SystemExit, SignerError) as err:
            print(f"water demo: station {rec.get('id')} not started ({err.__class__.__name__}: {err})", flush=True)
            continue
        sessions.append(StationSession(st, f"wss://{platform_addr}/device", context, server_name))
    if sessions:
        print(f"water demo: {len(sessions)} emulated station(s) -> wss://{platform_addr}/device", flush=True)
    await asyncio.gather(*(s.run() for s in sessions))
