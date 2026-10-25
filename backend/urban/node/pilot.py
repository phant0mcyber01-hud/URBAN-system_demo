"""The pilot machine's own district: ROLE=district,center, the district server and the control centre on one machine.

A separate district server makes its key at first boot and an administrator enrols its code at the centre. The pilot's
district side gets the same: at first boot urban-prepare makes it a key of its own (python -m urban.edge --keygen
--unit district-<the machine's name>, inside the TPM when there is one, never replaced) and hands its enrolment code to
the centre on the same machine in /run/urban/own-district.code (root's, read-only for everyone else). Then:

  * the centre enrols it by itself when it starts (enroll_own below): the same Registry.enroll_district as
    `urban-center provision enroll-district` and Devices -> Enrol, audited as district-enrolled like any district,
    with the address the centre's units already use for the centre (DEVICE_NAME). Once only: a marker in the centre's
    data folder remembers the key it enrolled, so a restart never enrols it again, and a district the admin revoked or
    removed stays so. A record under that name with another key is left alone (said, not overwritten);
  * its kit is the kit of any district (make_kit), except that it reaches its centre over the machine's loopback
    (127.0.0.1:<device port>, TLS to the centre's device listener, the certificate checked for the centre's name):
    `urban-center provision own-kit --code FILE` prints it, urban-prepare puts it in place with
    `python -m urban.edge --setup`, as `urban-unit setup` does on a district server;
  * its reporter is urban-district@district-<name> (urban/node/district.py) with UPDATES=off: the machine's system is
    the centre's own and is updated only by `urban-center release self-update`, never by an offer to its district.
    Its reports therefore carry no system version; the centre knows it as its own (DeviceHub.own) and leaves it out of
    the version floor, the update offers and the release waves, as it leaves itself out;
  * it has no WireGuard tunnel (the pilot has none). A unit routed through it gets no tunnel link (its Devices row
    says "district has no tunnel key yet") and keeps connecting straight to the centre: the district's address is
    the centre's own name, so the unit's next kit points where it already pointed.

What the centre takes from its reports is what it takes from any district's: only the queues of the approaches of
junctions whose units the admin routed through it (`urban-center provision assign edge-J-07 --district
district-center`). Nothing here runs on a separate district server, a unit or a centre without the district role:
they have no /run/urban/own-district.code.
"""
import datetime
import json
import os
from pathlib import Path

from ..devices import crypto
from ..devices.enrolment import DEFAULT_PORT, check_address, is_district, make_kit, parse_code

LOOPBACK = "127.0.0.1"


def read_code(path: Path) -> dict | None:
    """{device, serial, public} from the code file urban-prepare wrote, or None when there is none (every machine but
    the pilot). ValueError when it is there but is not a district's enrolment code."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    code = parse_code(text)
    if not is_district(code["device"]):
        raise ValueError(f"{code['device']} is not a district (district-<name>): the pilot's own district only")
    return code


def address(name: str, port: int) -> str:
    """Where units routed through the pilot's district connect: the centre itself, by the name they already use for it
    (DEVICE_NAME, on the server certificate), with the device port when it is not the default. A name that is not a
    host name gives the loopback (no unit outside reaches it; the reports still do)."""
    want = name if port == DEFAULT_PORT else f"{name}:{port}"
    try:
        return check_address(want)
    except ValueError:
        return f"{LOOPBACK}:{port}"


def own_kit(code: dict, pki: Path, server_name: str, port: int) -> dict:
    """The kit of the pilot's district: what make_kit gives any district (the centre's CA, its root key, the key's
    fingerprint), pointed at the centre over the machine's loopback. Made from the code, so it can be put in place
    before the centre has started for the first time."""
    rec = {"id": code["device"], "kind": "district", "junction": None, "name": "", "serial": code["serial"],
           "public_key": code["public"]}
    kit = make_kit(rec, Path(pki), server_name, port)
    kit["platform"] = f"{LOOPBACK}:{port}"
    return kit


def _marker(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def enroll_own(hub, audit, code_path: Path, name: str, port: int, marker: Path) -> str | None:
    """At the centre's start: enrol the machine's own district once. What happened, for the platform's log, or None
    when this machine has none. audit(event, target, note, actor) is the platform's audit chain (on its event loop)."""
    try:
        code = read_code(code_path)
    except (OSError, ValueError) as err:
        return f"not enrolled: {code_path}: {err}"
    if code is None:
        return None
    device, public = code["device"], code["public"]
    fp = crypto.fingerprint(public)
    done = _marker(marker)
    rec = hub.registry.devices.get(device)
    if rec is not None and rec.get("public_key") == public:
        hub.own.add(device)  # its system is this centre's: no floor, no offers, no wave (DeviceHub.own)
    if done is not None and done.get("device") == device and done.get("fingerprint") == fp:
        if rec is None:
            return f"{device} (key {fp}) was removed by the admin: not enrolled again"
        return f"{device} (key {fp}), {rec['status']}, enrolled {rec.get('enrolled', '-')}"
    if rec is not None and rec.get("public_key") != public:
        return (f"not enrolled: {device} is enrolled with another key ({crypto.fingerprint(rec['public_key'])}); "
                f"this machine's key is {fp}")
    if rec is None:
        try:
            rec = await hub.enroll_district(device, address(name, port), public, code["serial"])
        except ValueError as err:  # the key is enrolled under another name
            return f"not enrolled: {device}: {err}"
        hub.own.add(device)
        audit("district-enrolled", device, f"{rec['address']} key {fp} (this machine's own district, enrolled by itself)",
              "platform")
        what = f"{device} (key {fp}) enrolled at {rec['address']}"
    else:  # enrolled with this key before (by hand, or before the marker was written)
        what = f"{device} (key {fp}), already enrolled {rec.get('enrolled', '-')}"
    # whole or not at all: a marker cut short would be no marker, and a district the admin removed enrolled again
    tmp = Path(marker).with_name(Path(marker).name + ".tmp")
    tmp.write_text(json.dumps({"device": device, "fingerprint": fp,
                               "at": datetime.datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    os.replace(tmp, marker)
    return what
