"""Tools for the controllers in the cabinets.

    python -m urban.equipment probe HOST [--community public] [--snmp-version 1|0|3] [--no-modbus] [--json]
        what the controller speaks, read-only; ends with the adapter to use (docs/controllers.md)
    python -m urban.equipment profiles
        the built-in controller profiles
    python -m urban.equipment check-profile FILE
        is a profile file usable, and if not, why
"""
import argparse
import json
import os
import sys

from . import profiles
from .endpoint import parse_endpoint
from .probe import probe
from .snmp import SnmpClient, SnmpV3Client


def main():
    ap = argparse.ArgumentParser(prog="python -m urban.equipment", description="URBAN tools for signal controllers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="what a controller speaks (never writes)")
    p.add_argument("host", help="controller address, host or host:port for SNMP")
    p.add_argument("--community", default="public", help="SNMPv1/v2c read community (default public)")
    p.add_argument("--snmp-version", type=int, choices=(0, 1, 3), default=1, help="0 = v1, 1 = v2c, 3 = v3 (SNMP_* env)")
    p.add_argument("--modbus-port", type=int, default=502)
    p.add_argument("--no-modbus", action="store_true")
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--json", action="store_true")
    sub.add_parser("profiles", help="built-in controller profiles")
    c = sub.add_parser("check-profile", help="validate a profile file")
    c.add_argument("file")
    args = ap.parse_args()

    if args.cmd == "profiles":
        for name in profiles.builtin():
            pr = profiles.load(name)
            print(f"{name:28} {pr['transport']:11} {pr['name']} - {pr['status']}")
        return
    if args.cmd == "check-profile":
        try:
            pr = profiles.load(args.file)
        except profiles.ProfileError as err:
            print(f"refused: {err}")
            sys.exit(1)
        print(f"ok: {pr['name']} ({pr['transport']}), {len(pr['select'])} select step(s), "
              f"{len(pr['base'])} base step(s){', with read-back' if pr.get('confirm') else ''}")
        return

    host, port = parse_endpoint(args.host, 161)
    if args.snmp_version == 3:
        client = SnmpV3Client(host, port, user=os.getenv("SNMP_USER", ""), auth_password=os.getenv("SNMP_AUTH_PASS", ""),
                              priv_password=os.getenv("SNMP_PRIV_PASS", ""), auth=os.getenv("SNMP_AUTH", "sha"),
                              timeout=args.timeout)
    else:
        client = SnmpClient(host, port, community=args.community, version=args.snmp_version, timeout=args.timeout, retries=1)
    report = probe(host, client, None if args.no_modbus else args.modbus_port, args.timeout)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return
    s, m, r = report["snmp"], report["modbus"], report["recommend"]
    print(f"controller {host}")
    if s and s.get("answers"):
        print(f"  SNMP:    {s.get('descr', '')!s:.70}")
        if "enterprise" in s:
            print(f"           enterprise {s['enterprise']} ({s['enterprise_name']}), sysObjectID {s.get('object_id')}")
        print(f"  NTCIP:   {'pattern control present' if s['ntcip_1202'] else 'not found'}")
    else:
        print(f"  SNMP:    no answer{(' (' + s['error'] + ')') if s and s.get('error') else ''}")
    if m is not None:
        ident = m.get("identity") or {}
        print(f"  Modbus:  {'answers' if m['answers'] else 'no answer'}"
              + (f", {ident.get('vendor', '')} {ident.get('product', '')} {ident.get('revision', '')}".rstrip() if ident else ""))
    print(f"  adapter: {r['adapter']} - {r['why']}")
    print(f"  next:    {r['next']}")


if __name__ == "__main__":
    main()
