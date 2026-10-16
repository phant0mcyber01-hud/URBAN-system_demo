"""What does the controller in this cabinet speak? Read-only answers for the survey and the installer.

    python -m urban.equipment probe 192.168.10.2 [--community public] [--snmp-version 1|0]
    python -m urban.equipment probe 192.168.10.2 --snmp-version 3      (SNMP_USER, SNMP_AUTH_PASS, SNMP_PRIV_PASS)

Asks over SNMP who the device is (sysDescr, sysObjectID) and whether the NTCIP 1202 objects URBAN uses exist,
and over Modbus TCP whether it answers and who made it. Never writes anything: a probe must not be able to change
what a junction does. It does not guess passwords either - it uses the community or SNMPv3 user it is given.
The answer ends with the adapter to use, or with contacts, which every controller with plan-selection inputs takes.
"""
import socket

from .modbus_tcp import ModbusTcp
from .ntcip import SYSTEM_PATTERN_CONTROL, UNIT_BACKUP_TIME

SYS_DESCR, SYS_OBJECT_ID, SYS_NAME = "1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.2.0", "1.3.6.1.2.1.1.5.0"
ENTERPRISES = "1.3.6.1.4.1."
KNOWN_ENTERPRISES = {1206: "NEMA (NTCIP)"}  # only numbers we are sure of; others are shown as a number to look up


def enterprise(object_id: str) -> int | None:
    """The private enterprise number in a sysObjectID (1.3.6.1.4.1.<n>...)."""
    if isinstance(object_id, str) and object_id.startswith(ENTERPRISES):
        try:
            return int(object_id[len(ENTERPRISES):].split(".")[0])
        except ValueError:
            return None
    return None


def probe_snmp(client) -> dict:
    out = {"answers": False}
    try:
        out["descr"] = client.read(SYS_DESCR)
        out["answers"] = True
    except OSError as err:
        out["error"] = str(err)
        return out
    for key, oid in (("object_id", SYS_OBJECT_ID), ("name", SYS_NAME)):
        try:
            out[key] = client.read(oid)
        except OSError:
            pass
    pen = enterprise(out.get("object_id"))
    if pen is not None:
        out["enterprise"] = pen
        out["enterprise_name"] = KNOWN_ENTERPRISES.get(pen, f"look up {pen} in the IANA enterprise numbers")
    ntcip = {}
    for key, oid in (("unit_backup_time", UNIT_BACKUP_TIME), ("system_pattern_control", SYSTEM_PATTERN_CONTROL)):
        try:
            ntcip[key] = client.read(oid)
        except OSError:
            pass
    out["ntcip_1202"] = len(ntcip) == 2 and all(isinstance(v, int) for v in ntcip.values())
    out["ntcip"] = ntcip
    return out


def probe_modbus(host: str, port: int = 502, unit: int = 1, timeout: float = 2.0, connect=socket.create_connection) -> dict:
    client = ModbusTcp(host, port, unit, timeout, connect=connect)
    try:
        client.connect((host, port), timeout=timeout).close()
    except OSError as err:
        return {"answers": False, "error": f"port {port}: {err}"}
    out = {"answers": True}
    try:
        out["identity"] = client.device_id()
    except OSError as err:
        out["identity_error"] = str(err)  # many devices do not implement identification: not a failure
    finally:
        client.close()
    return out


def recommend(snmp: dict | None, modbus: dict | None) -> dict:
    if snmp and snmp.get("ntcip_1202"):
        return {"adapter": "ntcip", "why": "the controller answers the NTCIP 1202 objects for pattern control",
                "next": "check unitBackupTime and the pattern numbers against its MIB; use SNMPv3 if it has it"}
    if modbus and modbus.get("answers"):
        return {"adapter": "profile", "why": "the device answers Modbus TCP",
                "next": "get its register map from the manufacturer and fill a profile "
                        "(backend/urban/equipment/profiles/modbus-register-template.json)"}
    if snmp and snmp.get("answers"):
        return {"adapter": "profile", "why": "the device answers SNMP but not the NTCIP objects",
                "next": "get its MIB from the manufacturer and write an SNMP profile; until then use contacts"}
    return {"adapter": "contacts", "why": "no network protocol answered",
            "next": "use the controller's plan-selection inputs (see docs/controllers.md): every controller with "
                    "external plan inputs works this way"}


def probe(host: str, snmp_client=None, modbus_port: int | None = 502, timeout: float = 2.0, connect=socket.create_connection) -> dict:
    snmp = probe_snmp(snmp_client) if snmp_client is not None else None
    modbus = probe_modbus(host, modbus_port, timeout=timeout, connect=connect) if modbus_port else None
    return {"host": host, "snmp": snmp, "modbus": modbus, "recommend": recommend(snmp, modbus)}
