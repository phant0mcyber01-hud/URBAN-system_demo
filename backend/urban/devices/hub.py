"""Device hub: the platform end of the channel to URBAN Edge devices.

A device connects out to us (never the other way round), over TLS. It is trusted only after
it signs our challenge with the key enrolled in the registry. Commands going down are signed
with the gateway key and carry a rising sequence number and a timestamp, so a device can
refuse forgeries and replays without trusting the network.

The centre may also keep a version floor (`python -m urban.release floor N`, or the admin's POST /api/os-floor):
a device that cannot show it runs at least system N gets no commands at all, only update offers, until it does.
The rule is DeviceHub.floor_verdict. It is a mitigation for a unit put back on an old, possibly vulnerable, signed
release by someone with its disk in hand; it relies on what the device reports, so it does not stop a device that
lies about its version (that needs measured boot, a later step).

The hub also runs the centre's end of the WireGuard tunnel between each junction unit and its district server
(urban/net/wireguard.py, docs/wireguard-design.md). A unit or district reports its tunnel public key in its health;
the first one is accepted by itself, a changed one waits for an admin (POST /api/devices/{id}/wg-accept). The hub
numbers the district and the unit's tunnel address once (devices/registry.py) and sends, inside an ordinary signed
command {"wg": <message signed by the gateway key>}, the district its peer list and the unit its link: after the first
report of each session, again whenever the content changes, and again while the device reports an older one applied.
Only a device whose current session reported a tunnel key gets anything, so an older system, the pilot and a lighting
cabinet see no change.
"""
import asyncio
import dataclasses
import ipaddress
import json
import secrets
import time
import traceback
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, StrictInt

from ..net import wireguard
from ..release import ReleaseError
from . import crypto
from .enrolment import has_port
from .health import clean
from .registry import ACTIVE, DISTRICT, REVOKED, UNIT, Registry, kind_of

HELLO_TIMEOUT = 10  # seconds a connection may stay unauthenticated
LINK_TTL = 3600     # seconds a download link for an update package stays valid
REOFFER = 600       # seconds before the same release is offered to the same device again (it may be downloading)
FLOOR_KINDS = ("unit", "district", "lighting")  # the devices that run URBAN OS: the version floor applies to them
WG_KINDS = (UNIT, DISTRICT)  # the records with a tunnel; a lighting cabinet and the centre have none
WG_PLAN = wireguard.Plan.of()  # 10.77.0.0/16: the district is 10.77.<number>.1, its units .2 to .254
WG_RESEND, WG_RESEND_MAX = 30, 960  # seconds: a tunnel message not applied goes again, the pause doubling up to 960


def wg_endpoint(address) -> str | None:
    """Where a unit finds its district's WireGuard listener: the district's enrolled address without its port, on UDP
    51820. Only an IPv4 literal outside the tunnel network qualifies (the helper on the unit holds the link to the same
    host as its kit); a host name or anything else is None: no link is built for it."""
    host = str(address or "").strip()
    if has_port(host):
        host = host.rpartition(":")[0]
    try:
        ipaddress.IPv4Address(host)
        return wireguard.check_endpoint(WG_PLAN, host)
    except ValueError:  # not an IPv4 literal, or inside 10.77.0.0/16 (wireguard.WireGuardError)
        return None


def wg_fingerprint(public) -> str | None:
    """wireguard.fingerprint of a tunnel key from the registry, or None (no key, or not one)."""
    try:
        return wireguard.fingerprint(public) if public else None
    except wireguard.WireGuardError:
        return None


def floor_words(floor: int, have: int | None) -> str:
    """Why a device below the floor gets no commands, in words for people (the Devices list, a refused action)."""
    if have is not None:
        return f"ниже допустимой версии (ОС {have} < {floor}): команды не отправляются"
    return f"версия ОС не подтверждена (допустимая от {floor}): команды не отправляются"


class FloorChange(BaseModel):
    floor: StrictInt | None  # the new minimum system version; null clears it


class WgMode(BaseModel):
    mode: str  # "allow": the district's relay still answers units outside the tunnel; "only": through the tunnel only


class DeviceHub:
    def __init__(self, registry: Registry, gateway_key, on_report, clock=lambda: 0, wall=time.time,
                 plans: dict[str, set[int]] | None = None, gateway_cert: dict | None = None, releases=None,
                 audit=lambda event, target, note="", actor="platform": None):
        """on_report(device record, report dict); clock(): platform time sent in the welcome;
        wall(): wall-clock seconds stamped into every command, so a device can refuse stale ones;
        plans: tls id -> approved plan ids, handed to each unit at session start (its junction passport);
        gateway_cert: the root's certificate for gateway_key, presented at session start (devices/trust.py);
        releases: the ReleaseStore of OS update packages to offer and of the version floor (urban/release.py),
        or None to offer nothing and keep no floor;
        audit(event, target, note, actor): the platform's audit log, called on the event loop only."""
        self.releases = releases
        self.audit = audit
        self.proven: dict[str, int] = {}  # device id -> system version it reported in its current (or last) session
        self._refused: set[str] = set()   # devices whose refusal below the floor is already audited this session
        self._floor_audited: str | None = None  # id of the floor record last audited (also kept in the releases folder)
        self._floor_marker_read = False
        self.links: dict[str, dict] = {}  # download token -> {"device", "version", "until"}
        self._offered: dict[str, tuple[int, float]] = {}  # device id -> (release version, wall time of the offer)
        self.plans = plans or {}
        self.gateway_cert = gateway_cert
        self.registry, self.key, self.on_report, self.clock, self.wall = registry, gateway_key, on_report, clock, wall
        self.sessions: dict[str, WebSocket] = {}
        self.seen: dict[str, dict] = {}  # device id -> {"at": wall time, "health": {...}}
        self.connects: dict[str, int] = {}  # device id -> sessions since the platform started
        self._stale: set[str] = set()  # sessions opened on a key or status that has since changed
        self.epochs: dict[str, int | None] = {}  # device id -> gateway key epoch it trusted when it connected
        self._seq = time.time_ns() // 1_000_000  # survives restarts without going backwards
        # the tunnel, in memory only (the registry keeps keys and numbers): devices whose current session reported a
        # tunnel key; the last message built for each ({"content", "msg"}); what went to it in this session
        # ({"version", "at", "pause"}); devices that got a key set waiting in this session; keys ignored after that
        # (device -> (last such key, how many)); wg-key-refused audited this session; wg-link-refused audited
        self._wg_live: set[str] = set()
        self._wg_built: dict[str, dict] = {}
        self._wg_sent: dict[str, dict] = {}
        self._wg_pended: set[str] = set()
        self._wg_ignored: dict[str, tuple[str, int]] = {}
        self._wg_key_refused: set[str] = set()
        self._wg_link_refused: set[tuple[str, str]] = set()  # (unit, district address), once per process

    # ---- towards devices: this is gateway.send
    def online(self, tls: str) -> bool:
        """Has the unit of this junction a session now (whether it may be commanded is reachable())."""
        rec = self.registry.by_tls(tls)
        return rec is not None and rec["id"] in self.sessions

    def reachable(self, tls: str) -> bool:
        """Can the gateway command the unit of this junction now: it has a session and is not below the floor."""
        rec = self.registry.by_tls(tls)
        return rec is not None and rec["id"] in self.sessions and self.floor_verdict(rec["id"], rec) is None

    def why_unreachable(self, tls: str) -> str | None:
        """Why the unit of this junction cannot be commanded now, in words for people; None if it can."""
        rec = self.registry.by_tls(tls)
        if rec is None:
            return "у перекрёстка нет блока"
        if rec["id"] not in self.sessions:
            return "блок перекрёстка не на связи"
        verdict = self.floor_verdict(rec["id"], rec)
        return None if verdict is None else f"блок перекрёстка {rec['id']}: {floor_words(*verdict)}"

    def is_online(self, device: str) -> bool:
        """Has this device id a session now (by id, for the modules; online() is by junction)."""
        return device in self.sessions

    def devices_of(self, kind: str) -> list[dict]:
        """Copies of the registry records of one kind ("unit", "district", "lighting"), as the registry is now."""
        self._sync()
        return [dict(rec) for rec in self.registry.devices.values() if kind_of(rec) == kind]

    def envelope(self, device: str, body: dict) -> dict:
        self._seq += 1
        env = {"device": device, "seq": self._seq, "t": int(self.wall()), "body": body}
        return {**env, "sig": crypto.sign(self.key, env)}

    def send(self, msg: dict) -> bool:
        """Deliver a gateway message to the device of that junction. False if it was not sent.

        A device that is not connected simply does not get it: its lease runs out by itself. Neither does one below
        the version floor (refused, audited once per session); a lease it may hold runs out the same way.
        """
        rec = self.registry.by_tls(msg.get("tls", ""))
        if rec is None or rec["id"] not in self.sessions:
            return False
        if (verdict := self.floor_verdict(rec["id"], rec)) is not None:
            return self._refuse(rec["id"], verdict, str(msg.get("op") or "command"))
        text = json.dumps({"type": "cmd", **self.envelope(rec["id"], msg)}, ensure_ascii=False)
        asyncio.ensure_future(self._deliver(rec["id"], text))
        return True

    def send_device(self, device: str, body: dict) -> bool:
        """Deliver a signed command to one device by its id (a module's command is not about any junction).
        False if it is not connected or is below the version floor (refused, audited once per session)."""
        if device not in self.sessions:
            return False
        if (verdict := self.floor_verdict(device)) is not None:
            return self._refuse(device, verdict, str(next(iter(body), "command")))
        text = json.dumps({"type": "cmd", **self.envelope(device, body)}, ensure_ascii=False)
        asyncio.ensure_future(self._deliver(device, text))
        return True

    # ---- the version floor: the oldest system the centre still commands
    def floor(self) -> int | None:
        """The minimum system version (urban/release.py), or None: no floor, and the hub commands every version.
        A change made meanwhile (the CLI, another process) is audited here, the first time the event loop asks."""
        if self.releases is None:
            return None
        self._watch_floor()
        return self.releases.floor()

    def floor_verdict(self, device: str, rec: dict | None = None) -> tuple[int, int | None] | None:
        """(floor, the version it showed) when the centre must not command this device, else None. The rule:
        - no floor set: no device is below it (the default; nothing changes until an admin sets one);
        - only units, district servers and lighting cabinets are judged (FLOOR_KINDS: they run URBAN OS);
        - a device is judged by the system version it reported in its current session (`os` in its health report,
          a whole number), below the floor -> below it. A version from an earlier session does not count: the box may
          have been started from another disk since. Offline, the version of its last session stands;
        - a device that has not reported a version in this session (it sends none, or not yet) cannot prove it runs
          a new enough system: with a floor set it counts as below it;
        - as soon as it reports a version at or above the floor it is commanded again, no restart needed.
        Update offers are not commands in this sense: they still go to it, that is how it gets above the floor."""
        return self._verdict(device, rec if rec is not None else self.registry.devices.get(device), self.floor())

    def _verdict(self, device: str, rec: dict | None, floor: int | None) -> tuple[int, int | None] | None:
        if floor is None:
            return None
        if rec is not None and kind_of(rec) not in FLOOR_KINDS:
            return None  # an unknown record (rec None) is judged: refusing is the safe side
        have = self.proven.get(device)
        return None if have is not None and have >= floor else (floor, have)

    def floor_refusal(self, device: str) -> str | None:
        """Why the centre does not command this device now, in words for people, or None (Core.refusal)."""
        verdict = self.floor_verdict(device)
        return None if verdict is None else floor_words(*verdict)

    def _refuse(self, device: str, verdict: tuple[int, int | None], what: str) -> bool:
        """A command for a device below the floor is dropped; the first one in a session is audited. False."""
        if device not in self._refused:
            self._refused.add(device)
            floor, have = verdict
            shown = f"os {have} < floor {floor}" if have is not None else f"os not reported in this session, floor {floor}"
            self.audit("command-refused-below-floor", device, f"{shown}; first refused: {what[:40]}", actor="platform")
        return False

    def heard(self, device: str, health):
        """A health report from a device in its current session: what it says (cleaned), and the system version it
        shows now. A report without a version leaves it unproved (floor_verdict)."""
        self.seen[device] = {"at": time.time(), "health": clean(health)}
        os_version = self.seen[device]["health"].get("os")  # clean() keeps only a whole number
        if os_version is not None:
            self.proven[device] = os_version
        else:
            self.proven.pop(device, None)
        if self.floor_verdict(device) is None:
            self._refused.discard(device)  # commanded again: a later relapse is audited again

    def _watch_floor(self, anywhere: bool = False):
        """Audit each change of the floor once (os-floor-set), whoever made it: the admin's route at once, the CLI
        the next time the platform looks, also after a restart (the last audited record is kept on disk). Only on
        the event loop, which writes the audit chain; a worker thread leaves it to the loop (anywhere: the caller
        is on the loop)."""
        if not anywhere:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return
        rec = self.releases.floor_record()
        if rec is None:
            return
        if not self._floor_marker_read:
            self._floor_audited, self._floor_marker_read = self.releases.floor_audited(), True
        if rec["id"] == self._floor_audited:
            return
        self._floor_audited = rec["id"]
        self._refused.clear()  # a new floor: a refusal under it is news again
        try:
            self.releases.mark_floor_audited(rec["id"])
        except OSError:  # audited once in this process all the same
            print("hub: could not keep the audited floor record; it will be audited again after a restart", flush=True)
        was = rec.get("was")
        self.audit("os-floor-set", "os-floor", f"{rec['floor'] or 'none'} (was {was or 'none'})",
                   actor=str(rec.get("by") or "-"))

    def floor_view(self) -> dict:
        """The floor for the interface: its value, who set it and when, the newest release offered, and the active
        devices below it now."""
        floor = self.floor()
        rec = self.releases.floor_record() if self.releases is not None else None
        below = sorted(d for d, r in self.registry.devices.items()
                       if r.get("status") == ACTIVE and self._verdict(d, r, floor) is not None)
        return {"floor": floor, "set_by": rec.get("by") if rec else None, "at": rec.get("at") if rec else None,
                "newest": self.releases.newest() if self.releases is not None else None, "below": below}

    def set_floor(self, floor: int | None, who: str) -> dict:
        """The admin's change: written next to the releases and audited at once under the admin's name. Call it on
        the event loop. A floor above every release offered is allowed (the devices may already run it), with a
        warning: a device below it cannot be updated from the centre."""
        if self.releases is None:
            raise ReleaseError("this centre keeps no releases, so no floor")
        self.releases.set_floor(floor, by=who)
        self._watch_floor(anywhere=True)
        view = self.floor_view()
        if floor is not None and (view["newest"] is None or view["newest"] < floor):
            view["warning"] = (f"Ни один выпуск в центре не достигает ОС {floor} (новейший: {view['newest'] or 'нет'}): "
                               f"устройства ниже минимума нельзя обновить из центра")
        return view

    def floor_routes(self, viewer, admin) -> APIRouter:
        """GET /api/os-floor for everyone signed in, POST /api/os-floor {"floor": N | null} for the admin."""
        router = APIRouter()

        @router.get("/api/os-floor")
        async def get_floor(who: str = Depends(viewer)):
            return self.floor_view()

        @router.post("/api/os-floor")
        async def post_floor(body: FloorChange, who: str = Depends(admin)):
            try:
                return self.set_floor(body.floor, who)
            except ReleaseError:
                raise HTTPException(400, "минимальная версия ОС — целое число от 1 (или null, чтобы снять её)")
        return router

    # ---- over-the-air updates: the offer goes down the signed channel, the package comes over the same listener
    def pending_release(self, device: str, rec: dict) -> dict | None:
        """The release this device should be offered now, or None: it must be a unit, a district server or a
        lighting cabinet's unit that reported a system version (an integer) older than the newest release that is not withdrawn and targets it."""
        if self.releases is None or kind_of(rec) not in ("unit", "district", "lighting"):
            return None
        have = self.seen.get(device, {}).get("health", {}).get("os")
        rel = self.releases.newest_for(device)
        if rel is None or not isinstance(have, int) or isinstance(have, bool) or have >= rel["version"]:
            return None
        if not self.releases.path(rel).is_file():
            return None
        return rel

    def offer_update(self, device: str, rel: dict) -> dict | None:
        """Mint a download link bound to this device and release and build the signed offer for it."""
        now = self.wall()
        self.links = {t: v for t, v in self.links.items() if v["until"] > now and v["device"] != device}
        token = secrets.token_hex(32)
        self.links[token] = {"device": device, "version": rel["version"], "until": now + LINK_TTL}
        self._offered[device] = (rel["version"], now)
        return {"update": {"version": rel["version"], "sha256": rel["sha256"], "size": rel["size"],
                           "path": f"/update/{token}"}}

    async def _offer(self, device: str, rec: dict):
        """Offer the device the release it is due, unless the same one went out less than REOFFER ago."""
        try:
            rel = self.pending_release(device, rec)
            if rel is None:
                return
            last = self._offered.get(device)
            if last is not None and last[0] == rel["version"] and 0 <= self.wall() - last[1] < REOFFER:
                return
            body = self.offer_update(device, rel)
            text = json.dumps({"type": "cmd", **self.envelope(device, body)}, ensure_ascii=False)
            await self._deliver(device, text)
        except (KeyError, TypeError, OSError):  # a damaged release entry must not end the device's session
            print(f"hub: could not offer an update to {device}", flush=True)
            traceback.print_exc()

    def update_file(self, token: str) -> tuple[Path, str] | None:
        """The package a download link stands for: valid for one device and one release for LINK_TTL, and only
        while that release is still offered to that device. (path, file name), or None."""
        link = self.links.get(token)
        if link is None or self.releases is None:
            return None
        if link["until"] <= self.wall():
            self.links.pop(token, None)
            return None
        rec = self.registry.devices.get(link["device"])
        rel = self.releases.get(link["version"])
        if (rec is None or rec["status"] != ACTIVE or rel is None or rel.get("withdrawn")
                or (rel.get("devices") is not None and link["device"] not in rel["devices"])):
            return None
        path = self.releases.path(rel)
        return (path, path.name) if path.is_file() else None

    def update_routes(self) -> APIRouter:
        """GET /update/{token} for the device listener: the package a download link stands for. Whatever is wrong
        with the link (unknown, expired, withdrawn, another device's) is the same plain 404."""
        router = APIRouter()

        @router.get("/update/{token}")
        def update_package(token: str):
            found = self.update_file(token)
            if found is None:
                raise HTTPException(404)
            path, name = found
            return FileResponse(path, media_type="application/octet-stream", filename=name)
        return router

    # ---- the tunnel between a unit and its district: keys in from health reports, signed messages out
    def _wg_session(self, device: str):
        """A new session proves its tunnel key anew, gets its message again and starts a new resend schedule."""
        self._wg_live.discard(device)
        self._wg_sent.pop(device, None)
        self._wg_pended.discard(device)
        self._wg_ignored.pop(device, None)
        self._wg_key_refused.discard(device)

    def _wg_report(self, device: str, ws):
        """Key intake, after heard(): the tunnel key in this report goes to the registry (Registry.wg_report). The first
        key is accepted; a different one is set waiting for an admin once per session, and any further one in that
        session is only counted; a key another record holds is refused. Audited on the event loop. ws: the session
        the report came on; one that is no longer the device's session takes nothing."""
        wg = self.seen.get(device, {}).get("health", {}).get("wg")
        if wg is None:
            return
        self._sync()  # a change made with the CLI counts from this report on
        if self.sessions.get(device) is not ws:  # closed by that change: a re-enrolled box's first key is the new box's
            return
        rec = self.registry.devices.get(device)
        if rec is None or kind_of(rec) not in WG_KINDS:
            return
        self._wg_live.add(device)
        public = wg["public"]
        try:
            what, detail = self.registry.wg_report(device, public, may_pend=device not in self._wg_pended)
            if what == "accepted":
                self.audit("wg-key-accepted", device, wireguard.fingerprint(public), actor="platform")
                self._wg_number(device)
            elif what == "pending":
                self._wg_pended.add(device)
                self.audit("wg-key-pending", device, f"{wg_fingerprint(detail)} -> {wireguard.fingerprint(public)}",
                           actor="platform")
            elif what == "ignored":
                last, count = self._wg_ignored.get(device, (None, 0))
                if public != last:
                    self._wg_ignored[device] = (public, count + 1)
            elif what == "in use" and device not in self._wg_key_refused:
                self._wg_key_refused.add(device)
                self.audit("wg-key-refused", device, f"in use by {detail}", actor="platform")
        except (KeyError, TypeError, OSError):  # a damaged registry entry or a failed write must not end the session
            print(f"hub: could not take the tunnel key of {device}", flush=True)
            traceback.print_exc()

    def _wg_number(self, device: str):
        """The tunnel numbers this device is due now (Registry.wg_number); each one given is audited once, here."""
        for target, note in self.registry.wg_number(device):
            self.audit("wg-address-assigned", target, note, actor="platform")

    def _wg_problem(self, rec: dict) -> str | None:
        """Why a unit gets no tunnel link now, in the fixed words of its Devices row, or None."""
        if rec["status"] != ACTIVE:
            return "revoked"
        if not rec.get("district"):
            return "no district"
        district = self.registry.devices.get(rec["district"])
        if district is None or kind_of(district) != DISTRICT or district["status"] != ACTIVE:
            return "district not active"
        if not district.get("wg_public"):
            return "district has no tunnel key yet"
        if wg_endpoint(district.get("address")) is None:
            return "district address is not IPv4"
        if not rec.get("wg_public"):
            return "no tunnel key yet"
        return None

    def _wg_due(self, rec: dict) -> bool:
        """Whether this record should hold a tunnel message now: a district with an accepted key and a number, a unit
        with an accepted key and an address, routed through such a district at an IPv4 address."""
        if rec["status"] != ACTIVE or not rec.get("wg_public"):
            return False
        if kind_of(rec) == DISTRICT:
            return "wg_index" in rec
        if kind_of(rec) != UNIT or not rec.get("wg_ip") or self._wg_problem(rec) is not None:
            return False
        return "wg_index" in self.registry.devices.get(rec["district"], {})  # status() also runs in a worker thread

    def _wg_draft(self, device: str):
        """The PeerList or Link this device should hold, as the registry is now (version and time still 0), or None.
        Numbers it is due are given first: a district's units that have a key but no address yet get one here too."""
        self._wg_number(device)
        rec = self.registry.devices.get(device)
        if rec is None:
            return None
        if kind_of(rec) == UNIT:
            if self._wg_problem(rec) == "district address is not IPv4":
                address = str(self.registry.devices[rec["district"]].get("address"))
                if (device, address) not in self._wg_link_refused:
                    self._wg_link_refused.add((device, address))
                    self.audit("wg-link-refused", device, f"{rec['district']} address {address[:60]}: not an IPv4 "
                               f"address outside {WG_PLAN.base}", actor="platform")
            if not self._wg_due(rec):
                return None
            district = self.registry.devices[rec["district"]]
            return wireguard.make_link(WG_PLAN, device, district["id"], district["wg_index"], 0, 0, rec["wg_ip"],
                                       district["wg_public"], wg_endpoint(district["address"]))
        for d in self._wg_units(device):
            if not d.get("wg_ip"):
                self._wg_number(d["id"])
        rec = self.registry.devices.get(device)  # read again: numbering may have re-read the file
        if rec is None or kind_of(rec) != DISTRICT or not self._wg_due(rec):
            return None
        peers = [wireguard.Peer(d["id"], d["wg_public"], d["wg_ip"]) for d in self._wg_units(device) if d.get("wg_ip")]
        return wireguard.make_peer_list(WG_PLAN, device, rec["wg_index"], 0, 0, peers,
                                        plain_relay=rec.get("wg_mode", "allow") != "only")

    def _wg_units(self, district: str) -> list[dict]:
        """The active units routed through this district that have an accepted tunnel key: its peers once numbered.
        A key that waits for an admin never replaces the accepted one here."""
        return [d for d in self.registry.devices.values() if kind_of(d) == UNIT and d.get("district") == district
                and d["status"] == ACTIVE and d.get("wg_public")]

    def _wg_build(self, device: str) -> dict | None:
        """The signed tunnel message for this device now (wg-peers for a district, wg-link for a unit), or None. A new
        version (max(last + 1, wall clock in ms)) is minted only when the content changed; otherwise the message built
        last time is returned unchanged, signature and all. Versions live in memory: after a restart they continue
        from the wall clock, like the command sequence."""
        draft = self._wg_draft(device)
        if draft is None:
            return None
        content = {k: v for k, v in draft.body().items() if k not in ("version", "t")}
        built = self._wg_built.get(device)
        if built is not None and built["content"] == content:
            return built["msg"]
        now = self.wall()
        version = max(built["msg"]["version"] + 1 if built else 0, int(now * 1000))
        msg = wireguard.sign_message(self.key, dataclasses.replace(draft, version=version, issued=int(now)).body())
        self._wg_built[device] = {"content": content, "msg": msg}
        return msg

    def _wg_applied(self, device: str) -> int:
        """The version the device reported applied in its latest report, -1 if none."""
        applied = (self.seen.get(device, {}).get("health", {}).get("wg") or {}).get("applied")
        return applied if applied is not None else -1

    async def _wg_push(self, device: str, ws):
        """After every report: the device's tunnel message goes out if none went in this session or the content
        changed, and the same message goes again while the device reports an older one applied (WG_RESEND after the
        last send, then twice that, up to WG_RESEND_MAX). That covers a refusal on the device for a reason outside the
        message and a lost write. Like an update offer it bypasses the version floor: a revocation must reach a
        district that runs an old system. A device whose current session reported no tunnel key gets nothing, and
        neither does a session that is no longer the device's (ws: the session the report came on)."""
        if device not in self._wg_live or self.sessions.get(device) is not ws:
            return
        try:
            msg = self._wg_build(device)
        except (KeyError, TypeError, ValueError, OSError):  # a damaged entry or a failed write must not end the session
            print(f"hub: could not build the tunnel message for {device}", flush=True)
            traceback.print_exc()
            return
        if msg is None:
            return
        now, sent = self.wall(), self._wg_sent.get(device)
        if sent is not None and sent["version"] == msg["version"]:
            if self._wg_applied(device) >= msg["version"] or 0 <= now - sent["at"] < sent["pause"]:
                return
            pause = min(sent["pause"] * 2, WG_RESEND_MAX)
        else:
            pause = WG_RESEND
        self._wg_sent[device] = {"version": msg["version"], "at": now, "pause": pause}
        await self._deliver(device, json.dumps({"type": "cmd", **self.envelope(device, {"wg": msg})}, ensure_ascii=False))

    def _wg_proven(self, unit: str) -> bool:
        """The unit's current session says its tunnel is proven (it reaches the centre through it)."""
        wg = self.seen.get(unit, {}).get("health", {}).get("wg") or {}
        return unit in self.sessions and unit in self._wg_live and wg.get("proven") is True

    def _wg_unproven(self, district: str) -> list[str]:
        """The active units of this district that have not shown a proven tunnel in their current session (offline
        ones included): what closing its relay port (mode only) would cut off."""
        return sorted(d["id"] for d in self.registry.devices.values()
                      if kind_of(d) == UNIT and d.get("district") == district and d["status"] == ACTIVE
                      and not self._wg_proven(d["id"]))

    def _wg_row(self, rec: dict) -> dict | None:
        """The tunnel part of a Devices row (None for a lighting cabinet or the centre): the keys and numbers of the
        registry and what the device last reported. behind: its current session shows the message built for it not
        yet applied."""
        if kind_of(rec) not in WG_KINDS:
            return None
        device = rec["id"]
        wg = self.seen.get(device, {}).get("health", {}).get("wg") or {}
        built = self._wg_built.get(device)
        behind = bool(device in self.sessions and device in self._wg_live and wg and built is not None
                      and self._wg_due(rec) and self._wg_applied(device) < built["msg"]["version"])
        common = {"fingerprint": wg_fingerprint(rec.get("wg_public")), "pending": wg_fingerprint(rec.get("wg_pending")),
                  "state": wg.get("state"), "applied": wg.get("applied"), "epoch": wg.get("epoch"),
                  "reason": wg.get("reason"), "behind": behind,
                  "ignored_keys": self._wg_ignored.get(device, (None, 0))[1]}
        if kind_of(rec) == DISTRICT:
            index = rec.get("wg_index")
            try:
                address = WG_PLAN.server(index) if index is not None else None
            except wireguard.WireGuardError:
                address = None
            return {"index": index, "address": address, **common, "mode": rec.get("wg_mode", "allow"),
                    "peers": wg.get("peers"), "peers_up": wg.get("peers_up"), "unproven": self._wg_unproven(device)}
        handshake = wg.get("handshake")
        return {"address": rec.get("wg_ip"), **common, "proven": wg.get("proven"),
                "handshake_age": max(0, round(self.wall() - handshake)) if handshake else None,
                "district": rec.get("district"), "problem": self._wg_problem(rec)}

    def wg_routes(self, viewer, admin) -> APIRouter:
        """POST /api/devices/{id}/wg-accept: the admin confirms the tunnel key that waits; POST /api/devices/{id}/wg-mode
        {"mode": "allow" | "only"}: the admin opens or closes a district's relay port to units outside the tunnel. Both
        reach the devices at their next reports. Viewers see the tunnel in GET /api/devices (status())."""
        router = APIRouter()

        @router.post("/api/devices/{device}/wg-accept")
        async def wg_accept(device: str, who: str = Depends(admin)):
            self._sync()
            if device not in self.registry.devices:
                raise HTTPException(404, "unknown device")
            try:
                done = self.registry.wg_confirm(device)
            except ValueError as err:
                raise HTTPException(409, str(err))
            if done is None:
                raise HTTPException(409, "no tunnel key waits for confirmation")
            rec, old = done
            fingerprint = wireguard.fingerprint(rec["wg_public"])
            self.audit("wg-key-confirmed", device, f"{wg_fingerprint(old)} -> {fingerprint}", actor=who)
            return {"id": device, "fingerprint": fingerprint}

        @router.post("/api/devices/{device}/wg-mode")
        async def wg_mode(device: str, body: WgMode, who: str = Depends(admin)):
            self._sync()
            try:
                done = self.registry.wg_set_mode(device, body.mode)
            except ValueError as err:  # not a district, or not a mode
                raise HTTPException(400, str(err))
            if done is None:
                raise HTTPException(404, "unknown device")
            _, was = done
            self.audit("wg-mode-set", device, f"{body.mode} (was {was})", actor=who)
            return {"id": device, "mode": body.mode, "unproven": self._wg_unproven(device)}
        return router

    async def _deliver(self, device: str, text: str):
        ws = self.sessions.get(device)
        if ws is None:
            return
        try:
            await ws.send_text(text)
        except Exception:
            self.sessions.pop(device, None)

    # ---- from devices
    async def serve(self, ws: WebSocket):
        await ws.accept()
        nonce = secrets.token_hex(16)
        device = None
        try:
            await ws.send_text(json.dumps({"type": "challenge", "nonce": nonce}))
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT))
            if not isinstance(hello, dict):
                hello = {}
            rec = self.registry.authenticate(str(hello.get("device")), nonce, str(hello.get("sig")))
            if rec is None:
                await ws.close(code=4401)
                return
            device = rec["id"]
            self.registry.mark_seen(device)
            old = self.sessions.pop(device, None)
            if old is not None:  # the same unit reconnected: one session per device
                await _close(old)
            self.sessions[device] = ws
            self.connects[device] = self.connects.get(device, 0) + 1
            self.proven.pop(device, None)  # a new session proves its system version anew (floor_verdict)
            self._refused.discard(device)
            self.seen[device] = self.reconnected(device, hello.get("version"))
            epoch = hello.get("epoch")
            self.epochs[device] = epoch if isinstance(epoch, int) else None
            welcome = {"type": "welcome", "t": self.clock(), "tls": rec["tls"],
                       "plans": sorted(self.plans.get(rec["tls"], ())) if kind_of(rec) == "unit" else [],  # only a junction has plans
                       "gateway": self.gateway_cert}
            if kind_of(rec) == "lighting":
                welcome["cabinet"] = rec.get("cabinet")
            await ws.send_text(json.dumps(welcome))
            self._offered.pop(device, None)  # a new session may have a device that lost its download
            self._wg_session(device)  # its tunnel message goes again after its first report
            await self._offer(device, rec)
            while True:
                report = json.loads(await ws.receive_text())
                if isinstance(report, dict) and report.get("type") == "report":
                    self.heard(device, report.get("health"))
                    self._wg_report(device, ws)
                    if isinstance(report.get("epoch"), int) and not isinstance(report["epoch"], bool):
                        self.epochs[device] = report["epoch"]  # the gateway key epoch the unit obeys now
                    if kind_of(rec) == "district":  # which junctions it is trusted for, from the registry as it is now
                        rec = {**rec, "junctions": [d["junction"] for d in self.registry.devices.values()
                                                    if d.get("district") == rec["id"] and d.get("junction")
                                                    and d["status"] == ACTIVE]}  # a revoked unit is no one's junction
                    self.on_report(rec, report)
                    await self._offer(device, rec)
                    await self._wg_push(device, ws)
        except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError):
            pass  # the usual ends of a session: gone, silent, or talking nonsense
        except Exception:  # a bug in report handling must not look like a flaky network
            print(f"hub: dropping the session of {device}: report handling failed", flush=True)
            traceback.print_exc()
        finally:
            if device is not None and self.sessions.get(device) is ws:
                del self.sessions[device]

    def reconnected(self, device: str, version) -> dict:
        """The hub's view of a unit at session start: its last known health stays until the first report,
        so a short link outage does not clear its alerts and raise them again a second later."""
        last = self.seen.get(device, {}).get("health", {})
        return {"at": time.time(), "health": {**last, **clean({"version": version})}}

    # ---- registry operations
    def _sync(self):
        """Pick up enrolments made by another process; sessions of units whose key or status changed are closed
        (on the event loop; from a worker thread they are remembered and closed on the next call there)."""
        self._stale |= self.registry.refresh()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for device in list(self._stale):
            self._stale.discard(device)
            ws = self.sessions.pop(device, None)
            if ws is not None:
                asyncio.ensure_future(_close(ws))

    async def enroll(self, device: str, junction: dict, public: str, serial: str = "", district: str | None = None) -> dict:
        rec = self.registry.enroll(device, junction, public, serial, district)
        ws = self.sessions.pop(device, None)
        if ws is not None:  # a session on the previous key ends; the unit reconnects with its new one
            await _close(ws)
        return rec

    async def enroll_district(self, device: str, address: str, public: str, serial: str = "") -> dict:
        rec = self.registry.enroll_district(device, address, public, serial)
        ws = self.sessions.pop(device, None)
        if ws is not None:
            await _close(ws)
        return rec

    async def enroll_lighting(self, device: str, cabinet: str, public: str, serial: str = "",
                              lon: float | None = None, lat: float | None = None) -> dict:
        rec = self.registry.enroll_lighting(device, cabinet, public, serial, lon, lat)
        ws = self.sessions.pop(device, None)
        if ws is not None:
            await _close(ws)
        return rec

    def assign(self, device: str, district: str | None) -> dict | None:
        return self.registry.assign(device, district)

    async def set_plans(self, tls: str, plans: set[int] | None):
        """A junction's approved plans changed: its unit is reconnected so it learns them at session start."""
        if plans:
            self.plans[tls] = set(plans)
        else:
            self.plans.pop(tls, None)
        for rec in self.registry.devices.values():
            if tls and rec.get("tls") == tls and (ws := self.sessions.pop(rec["id"], None)) is not None:
                await _close(ws, CLOSED_PLANS)

    async def remove(self, device: str) -> dict | None:
        rec = self.registry.remove(device)
        ws = self.sessions.pop(device, None)
        if ws is not None:
            await _close(ws)
        self.seen.pop(device, None)
        self.proven.pop(device, None)
        self._refused.discard(device)
        self._wg_session(device)
        self._wg_built.pop(device, None)
        return rec

    async def set_status(self, device: str, status: str) -> dict | None:
        rec = self.registry.set_status(device, status)
        if rec is not None and status == REVOKED:
            ws = self.sessions.pop(device, None)
            if ws is not None:
                await _close(ws)
        return rec

    def status(self) -> list[dict]:
        """One row per device; below_floor: it gets no commands now (floor_verdict), floor: the minimum or None,
        os_proven: the system version it reported in its current (or last) session; wg: its tunnel (_wg_row), None
        for a lighting cabinet."""
        self._sync()
        now = time.time()
        floor = self.floor()
        out = []
        for rec in self.registry.devices.values():
            seen = self.seen.get(rec["id"], {})
            out.append({"id": rec["id"], "kind": kind_of(rec), "junction": rec.get("junction"), "tls": rec.get("tls"),
                        "name": rec.get("name", ""), "address": rec.get("address"), "cabinet": rec.get("cabinet"), "district": rec.get("district"),
                        "serial": rec.get("serial", ""), "enrolled": rec.get("enrolled", ""),
                        "first_seen": rec.get("first_seen"),
                        "fingerprint": crypto.fingerprint(rec["public_key"]),
                        "gateway_epoch": self.epochs.get(rec["id"]),
                        "status": rec["status"], "online": rec["id"] in self.sessions,
                        "connects": self.connects.get(rec["id"], 0),
                        "age": round(now - seen["at"]) if "at" in seen else None,
                        "health": seen.get("health", {}),
                        "floor": floor, "below_floor": self._verdict(rec["id"], rec, floor) is not None,
                        "os_proven": self.proven.get(rec["id"]), "wg": self._wg_row(rec)})
        return out


CLOSED_TRUST, CLOSED_PLANS = 4403, 4012  # key or status changed / the junction's plan table changed


async def _close(ws: WebSocket, code: int = CLOSED_TRUST):
    try:
        await ws.close(code=code)
    except Exception:
        pass
