"""Bringing an URBAN Edge unit into service without copying keys or certificates by hand.

On the unit, once:
    python -m urban.edge --keygen --unit edge-J-07 --serial URB-0042
        the key is born on the unit; it prints an enrolment code: unit id, serial and the public half
        of its key, with a check sum that catches a mangled copy, and the key's fingerprint
In the UI, an administrator (Устройства -> Подключить блок):
        pastes the code, picks the junction, compares the fingerprint with the one the unit printed
        (by phone, by eye), and downloads the unit's kit
On the unit:
    python -m urban.edge --setup urban-edge-J-07.json --unit edge-J-07
        puts the kit in place - the platform's address, its ca.pem and root.pub - and refuses a kit
        that was made for another key. Nothing in the kit is secret; nothing secret leaves the unit.
"""
import hashlib
import json
import re
from pathlib import Path

from . import crypto

PREFIX = "URBAN1"
KIT_VERSION = 1
SERIAL = re.compile(r"^[A-Za-z0-9._-]{0,40}$")
DEVICE = re.compile(r"^edge-[A-Za-z0-9._-]{1,54}$")


def _check(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:6].upper()


def make_code(device: str, serial: str, public: str) -> str:
    if not DEVICE.match(device) or not SERIAL.match(serial):
        raise ValueError("unit id is edge-<junction>, serial: letters, digits, . _ - (up to 40)")
    body = f"{PREFIX}:{device}:{serial}:{public.lower()}"
    return f"{body}:{_check(body)}"


def parse_code(code: str) -> dict:
    """The code as the unit printed it -> {device, serial, public}. Errors are worded for the administrator."""
    text = "".join(str(code).split())  # line breaks and spaces picked up while copying
    parts = text.split(":")
    if len(parts) != 5 or parts[0].upper() != PREFIX:
        raise ValueError("это не код подключения URBAN: он начинается с URBAN1: и печатается командой --keygen на блоке")
    body = ":".join([PREFIX, *parts[1:4]])
    if _check(body) != parts[4].upper():
        raise ValueError("код повреждён при копировании: не сходится контрольная сумма")
    device, serial, public = parts[1], parts[2], parts[3].lower()
    if not DEVICE.match(device) or not SERIAL.match(serial):
        raise ValueError("в коде неверный номер блока или серийный номер")
    try:
        crypto.load_public(public)
    except ValueError:
        raise ValueError("в коде нет открытого ключа блока")
    return {"device": device, "serial": serial, "public": public}


def make_kit(rec: dict, pki: Path, server_name: str, port: int) -> dict:
    """What a unit needs besides its own key: where the platform is and whom to trust. All of it public."""
    root, gateway = pki / "root.pub", pki / "gateway.pub"
    return {
        "urban_kit": KIT_VERSION, "unit": rec["id"], "junction": rec["junction"], "name": rec.get("name", ""),
        "serial": rec.get("serial", ""), "fingerprint": crypto.fingerprint(rec["public_key"]),
        "platform": f"{server_name}:{port}", "server_name": server_name,
        "ca_pem": (pki / "ca.pem").read_text(),
        "root_pub": root.read_text().strip() if root.exists() else None,
        "gateway_pub": None if root.exists() else gateway.read_text().strip(),  # older setups without a root key
    }


ENV_TEMPLATE = """# URBAN Edge unit {unit} at {junction} {name}
# made by `python -m urban.edge --setup`; PLATFORM and SERVER_NAME are rewritten by the next setup, the rest is kept.
# systemd reads this file: comments only on their own lines, never after a value.
PLATFORM={platform}
SERVER_NAME={server_name}
# none | relay (Modbus relay board) | ntcip (SNMP to the controller)
ADAPTER=none
# relay: serial port, e.g. /dev/ttyUSB0; ntcip: controller address, e.g. 192.168.10.2:161
CONTROLLER=
# queue lengths written by the camera pipeline
SENSORS=/run/urban/queues.json
# cabinet temperature (DS18B20: /sys/bus/w1/devices/28-.../w1_slave); empty: the computer's own board
TEMP_SENSOR=
# door switch (a GPIO value file); empty: no switch wired, the door shows as unknown
DOOR_SENSOR=
# ntcip, SNMPv3 authPriv: SNMP_USER, SNMP_AUTH_PASS, SNMP_PRIV_PASS, SNMP_AUTH=sha
"""


def install_kit(kit: dict, unit: str, key_path: Path, pki: Path, env_path: Path) -> list[str]:
    """Put a kit in place on the unit. Returns what was written. Refuses a kit for another unit or key."""
    if not isinstance(kit, dict) or kit.get("urban_kit") != KIT_VERSION:
        raise ValueError("this is not an URBAN unit kit (download it in the UI: Устройства -> the unit -> Комплект)")
    if kit.get("unit") != unit:
        raise ValueError(f"the kit is for {kit.get('unit')}, this unit is {unit}")
    if not key_path.exists():
        raise ValueError(f"no key at {key_path}: make it first with --keygen")
    mine = crypto.fingerprint(crypto.public_hex(crypto.load_private(key_path.read_text())))
    if mine != kit.get("fingerprint"):
        raise ValueError(f"the kit was made for key {kit.get('fingerprint')}, this unit's key is {mine}: "
                         "enrol this unit's code again and download a new kit")
    if not str(kit.get("ca_pem", "")).startswith("-----BEGIN CERTIFICATE-----"):
        raise ValueError("the kit has no platform certificate")
    pki.mkdir(parents=True, exist_ok=True)
    written = []
    (pki / "ca.pem").write_text(kit["ca_pem"])
    written.append(str(pki / "ca.pem"))
    for name, field in (("root.pub", "root_pub"), ("gateway.pub", "gateway_pub")):
        if kit.get(field):
            crypto.load_public(kit[field])  # a key, not anything else
            (pki / name).write_text(kit[field] + "\n")
            written.append(str(pki / name))
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        values = {"PLATFORM": kit["platform"], "SERVER_NAME": kit["server_name"]}
        out = [f"{k}={values.pop(k)}" if (k := line.split("=", 1)[0].strip()) in values else line for line in lines]
        out += [f"{k}={v}" for k, v in values.items()]
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(ENV_TEMPLATE.format(**{k: kit.get(k, "") for k in
                                                   ("unit", "junction", "name", "platform", "server_name")}),
                            encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    written.append(str(env_path))
    return written


def load_kit(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ValueError(f"cannot read the kit {path}: {err}")
