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
import ipaddress
import json
import re
from pathlib import Path

from . import crypto

PREFIX = "URBAN1"
KIT_VERSION = 1
SERIAL = re.compile(r"^[A-Za-z0-9._-]{0,40}$")
DEVICE = re.compile(r"^(edge|district|light)-[A-Za-z0-9._-]{1,54}$")  # a junction unit, a district server, a lighting cabinet's unit
LIGHT_DEVICE = re.compile(r"^light-[A-Za-z0-9._-]{1,54}$")
CABINET = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
DEFAULT_PORT = 8443
HOST_PORT = re.compile(r"[A-Za-z0-9.-]+(:\d{1,5})?")      # name or IPv4, optional port
IPV6_PORT = re.compile(r"\[([0-9A-Fa-f:]+)\](:\d{1,5})?")  # [address], optional port


def is_district(device: str) -> bool:
    return device.startswith("district-")


def is_lighting(device: str) -> bool:
    return device.startswith("light-")


def check_address(address) -> str:
    """The host name or IP (host, host:port or [ipv6]:port) a district's units connect to, trimmed; ValueError if not."""
    address = str(address).strip()
    m = HOST_PORT.fullmatch(address)
    port = m and m.group(1)
    if m is None and (m := IPV6_PORT.fullmatch(address)):
        try:
            ipaddress.IPv6Address(m.group(1))
        except ValueError:
            m = None
        port = m and m.group(2)
    if m is None or (port and not 1 <= int(port[1:]) <= 65535):
        raise ValueError("the district's address is the host name or IP its units connect to, "
                         "e.g. district-1.city, 10.1.0.2:8443 or [fd00::2]:8443")
    return address


def has_port(address: str) -> bool:
    return address.rpartition("]")[2].startswith(":") if address.startswith("[") else ":" in address


def _check(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:6].upper()


def make_code(device: str, serial: str, public: str) -> str:
    if not DEVICE.fullmatch(device) or not SERIAL.fullmatch(serial):
        raise ValueError("device id is edge-<junction>, district-<name> or light-<cabinet>, serial: letters, digits, . _ - (up to 40)")
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
    if not DEVICE.fullmatch(device) or not SERIAL.fullmatch(serial):
        raise ValueError("в коде неверный номер блока или серийный номер")
    try:
        crypto.load_public(public)
    except ValueError:
        raise ValueError("в коде нет открытого ключа блока")
    return {"device": device, "serial": serial, "public": public}


def make_kit(rec: dict, pki: Path, server_name: str, port: int, approaches: list[dict] | None = None,
             via: dict | None = None) -> dict:
    """What a device needs besides its own key: where to connect and whom to trust, and for a unit the approaches
    of its junction for the camera calibration page. All of it public.

    via: the district server record a unit connects through. Its address replaces the centre's, the certificate
    name stays the centre's: the district only carries the bytes, the unit still checks the centre."""
    root, gateway = pki / "root.pub", pki / "gateway.pub"
    platform = f"{server_name}:{port}"
    if via is not None:
        address = str(via.get("address", "")).strip()
        platform = address if has_port(address) else f"{address}:{DEFAULT_PORT}"
    return {
        "urban_kit": KIT_VERSION, "unit": rec["id"], "kind": rec.get("kind", "unit"), "junction": rec.get("junction"),
        "name": rec.get("name", ""), "via": via["id"] if via is not None else None,
        "serial": rec.get("serial", ""), "fingerprint": crypto.fingerprint(rec["public_key"]),
        "platform": platform, "server_name": server_name,
        "ca_pem": (pki / "ca.pem").read_text(),
        "root_pub": root.read_text().strip() if root.exists() else None,
        "gateway_pub": None if root.exists() else gateway.read_text().strip(),  # older setups without a root key
        "approaches": [{k: a.get(k) for k in ("id", "name", "from", "lanes", "zone")} for a in approaches or []],
        **({"cabinet": rec.get("cabinet"), "lon": rec.get("lon"), "lat": rec.get("lat")}
           if rec.get("kind") == "lighting" else {}),  # a lighting unit: the cabinet it controls and where it stands
    }


DISTRICT_ENV_TEMPLATE = """# URBAN district server {unit}: its own link to the control centre (urban-district.service)
# made by `python -m urban.edge --setup`; PLATFORM and SERVER_NAME are rewritten by the next setup, the rest is kept.
PLATFORM={platform}
SERVER_NAME={server_name}
# queue lengths of the district's cameras (python -m urban.vision run), keyed by approach id
SENSORS=/run/urban/q/queues.json
# cabinet temperature sensor; empty: the computer's own board
TEMP_SENSOR=
"""

LIGHTING_ENV_TEMPLATE = """# URBAN lighting unit {unit}, cabinet {cabinet}: the unit of a street-lighting cabinet (urban-lighting@{unit}.service)
# made by `python -m urban.edge --setup`; PLATFORM, SERVER_NAME, CABINET, LON and LAT are rewritten by the next setup,
# the rest is kept. systemd reads this file: comments only on their own lines, never after a value.
PLATFORM={platform}
SERVER_NAME={server_name}
CABINET={cabinet}
{place}# emulated (demo, checks) | relay (the contactor output of the cabinet)
ADAPTER=emulated
"""

ENV_TEMPLATE = """# URBAN Edge unit {unit} at {junction} {name}
# made by `python -m urban.edge --setup`; PLATFORM and SERVER_NAME are rewritten by the next setup, the rest is kept.
# systemd reads this file: comments only on their own lines, never after a value.
PLATFORM={platform}
SERVER_NAME={server_name}
# none | contacts (plan-selection inputs, any controller that has them) | ntcip | profile
# which one: python -m urban.equipment probe <controller>, docs/controllers.md
ADAPTER=none
# contacts: serial port, host:port of an Ethernet relay module or /dev/gpiochipN; ntcip, profile: controller address
CONTROLLER=
# contacts and profile options go here, e.g. --bus gpio --gpio-lines 17,27,22,23 --enable-contact 3 --feedback-input 0
EXTRA=
# queue lengths written by the camera pipeline
SENSORS=/run/urban/q/queues.json
# cabinet temperature (DS18B20: /sys/bus/w1/devices/28-.../w1_slave); empty: the computer's own board
TEMP_SENSOR=
# door switch (a GPIO value file); empty: no switch wired, the door shows as unknown
DOOR_SENSOR=
# ntcip, SNMPv3 authPriv: SNMP_USER, SNMP_AUTH_PASS, SNMP_PRIV_PASS, SNMP_AUTH=sha
"""


def _lighting_env(kit: dict, unit: str, env_path: Path) -> None:
    """The env file of a lighting unit: made on first setup, then only the lines the kit decides are rewritten
    (PLATFORM, SERVER_NAME, CABINET and, if the kit has them, LON and LAT); ADAPTER and anything else stay."""
    values = {"PLATFORM": kit["platform"], "SERVER_NAME": kit["server_name"], "CABINET": kit["cabinet"]}
    for name in ("lon", "lat"):
        if kit.get(name) is not None:
            values[name.upper()] = repr(float(kit[name]))
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out = [f"{k}={values.pop(k)}" if (k := line.split("=", 1)[0].strip()) in values else line for line in lines]
        out += [f"{k}={v}" for k, v in values.items()]
        if not any(line.split("=", 1)[0].strip() == "ADAPTER" for line in out):
            out.append("ADAPTER=emulated")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return
    place = "".join(f"{k}={values[k]}\n" for k in ("LON", "LAT") if k in values)
    env_path.write_text(LIGHTING_ENV_TEMPLATE.format(unit=unit, cabinet=kit["cabinet"], platform=kit["platform"],
                                                     server_name=kit["server_name"], place=place), encoding="utf-8")


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
    lighting = kit.get("kind") == "lighting"
    if lighting:
        if not isinstance(kit.get("cabinet"), str) or not CABINET.fullmatch(kit["cabinet"]) or not LIGHT_DEVICE.fullmatch(unit):
            raise ValueError("the kit of a lighting unit needs its cabinet (letters, digits, . _ -) and a unit id light-<cabinet>")
        for field, limit in (("lon", 180), ("lat", 90)):
            v = kit.get(field)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not -limit <= v <= limit):
                raise ValueError(f"the kit's {field} is not a coordinate")
    for field in ("platform", "server_name", "unit", "junction"):  # these land in the env file: one line each
        value = kit.get(field)
        if value is not None and (not isinstance(value, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
            raise ValueError(f"the kit's {field} has a control character or a line break: not a kit this platform made")
    title = "".join(ch for ch in str(kit.get("name") or "") if ch.isprintable())  # free text: cleaned, not refused
    pki.mkdir(parents=True, exist_ok=True)
    written = []
    (pki / "ca.pem").write_text(kit["ca_pem"])
    written.append(str(pki / "ca.pem"))
    for name, field in (("root.pub", "root_pub"), ("gateway.pub", "gateway_pub")):
        if kit.get(field):
            crypto.load_public(kit[field])  # a key, not anything else
            (pki / name).write_text(kit[field] + "\n")
            written.append(str(pki / name))
    if lighting:
        _lighting_env(kit, unit, env_path)
    elif env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        values = {"PLATFORM": kit["platform"], "SERVER_NAME": kit["server_name"]}
        out = [f"{k}={values.pop(k)}" if (k := line.split("=", 1)[0].strip()) in values else line for line in lines]
        out += [f"{k}={v}" for k, v in values.items()]
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    else:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        template = DISTRICT_ENV_TEMPLATE if kit.get("kind") == "district" else ENV_TEMPLATE
        env_path.write_text(template.format(**{**{k: kit.get(k) or "" for k in
                                                  ("unit", "junction", "platform", "server_name")}, "name": title}),
                            encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    written.append(str(env_path))
    if kit.get("kind") in ("district", "lighting"):
        return written  # a district and a lighting cabinet have no junction of their own
    junction = env_path.parent / f"{unit}.junction.json"  # for the camera calibration page
    junction.write_text(json.dumps({"junction": kit.get("junction"), "name": title,
                                    "approaches": kit.get("approaches") or []}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    written.append(str(junction))
    return written


def load_kit(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ValueError(f"cannot read the kit {path}: {err}")
