"""Provisioning: TLS material, the gateway signing key and the registry of URBAN Edge units.

    python -m urban.provision                       for this machine (DEVICE_NAME from .env, default localhost)
    python -m urban.provision --host city.example   the name devices will connect to; the server certificate covers it
    python -m urban.provision enroll edge-J-21 --junction J-21 --public <hex> [--serial URB-...]
                                                    a real unit: its public key, printed by `python -m urban.edge --keygen`
    python -m urban.provision enroll-district district-1 --address 10.1.0.2 --public <hex> [--serial ...]
                                                    a district server: its key and the address its units connect to
    python -m urban.provision assign edge-J-21 --district district-1   (or --direct)
                                                    route a unit through a district: its next kit points there
    python -m urban.provision kit edge-J-21         the device's kit as JSON (what the UI's "download kit" gives)
    python -m urban.provision list                  units, districts, statuses and key fingerprints
    python -m urban.provision rotate-gateway [--root /media/usb/root.key]
                                                    a new gateway key with the next epoch; restart the platform after

Writes to data/pki (not in git). Safe to run again: existing keys and statuses are kept; only a server
certificate that does not cover the host is reissued (needs ca.key in the folder). In field mode
(URBAN_SOURCE=field) no unit keys are generated here: units are enrolled one by one, and a running
platform picks the enrolment up within seconds.
"""
import argparse

from . import config as c
from .devices import crypto, trust
from .devices.enrolment import make_kit
from .devices.registry import Registry, kind_of, provision, server_names
from .junctions import load_all_junctions


def main():
    ap = argparse.ArgumentParser(description="URBAN provisioning")
    ap.add_argument("--host", default=c.DEVICE_NAME, help="host name on the device listener's certificate")
    sub = ap.add_subparsers(dest="cmd")
    enroll = sub.add_parser("enroll", help="enrol a unit's public key")
    enroll.add_argument("device", help="edge-<junction>, e.g. edge-J-21")
    enroll.add_argument("--junction", required=True, help="junction id, e.g. J-21")
    enroll.add_argument("--public", required=True, help="the unit's public key (hex)")
    enroll.add_argument("--serial", default="", help="serial number printed on the box")
    district = sub.add_parser("enroll-district", help="enrol a district server's public key and address")
    district.add_argument("device", help="district-<name>, e.g. district-1")
    district.add_argument("--address", required=True, help="host name or IP (host[:port]) its units connect to")
    district.add_argument("--public", required=True, help="the district's public key (hex)")
    district.add_argument("--serial", default="", help="serial number printed on the box")
    assign = sub.add_parser("assign", help="route a unit through a district server, or directly")
    assign.add_argument("device", help="edge-<junction>")
    assign.add_argument("--district", help="district-<name>")
    assign.add_argument("--direct", action="store_true", help="straight to the centre")
    kit = sub.add_parser("kit", help="print a device's kit as JSON")
    kit.add_argument("device")
    sub.add_parser("list", help="show the registry")
    rot = sub.add_parser("rotate-gateway", help="new gateway key, signed by the root key with the next epoch")
    rot.add_argument("--root", help="root.key, if it is kept off the server (it should be)")
    args = ap.parse_args()
    junctions = load_all_junctions()
    created = provision(junctions, c.PKI, host=args.host, demo_devices=c.SOURCE != "field")
    if args.cmd == "enroll":
        junction = next((j for j in junctions if j["id"] == args.junction), None)
        if junction is None:
            raise SystemExit(f"unknown junction {args.junction}")
        try:
            rec = Registry(c.PKI / "devices.json").enroll(args.device, junction, args.public, args.serial)
        except ValueError as err:
            raise SystemExit(str(err))
        print(f"enrolled {rec['id']} at {rec['junction']} ({rec['serial']}), key {crypto.fingerprint(rec['public_key'])}")
        print("compare this fingerprint with the one the unit printed; a running platform picks it up within seconds")
        return
    if args.cmd == "enroll-district":
        try:
            rec = Registry(c.PKI / "devices.json").enroll_district(args.device, args.address, args.public, args.serial)
        except ValueError as err:
            raise SystemExit(str(err))
        print(f"enrolled district {rec['id']} at {rec['address']} ({rec['serial']}), key {crypto.fingerprint(rec['public_key'])}")
        return
    if args.cmd == "assign":
        if bool(args.district) == bool(args.direct):
            raise SystemExit("say --district <id> or --direct")
        try:
            rec = Registry(c.PKI / "devices.json").assign(args.device, None if args.direct else args.district)
        except ValueError as err:
            raise SystemExit(str(err))
        if rec is None:
            raise SystemExit(f"unknown unit {args.device}")
        print(f"{rec['id']}: {'via ' + rec['district'] if rec.get('district') else 'direct to the centre'} (its next kit says so)")
        return
    if args.cmd == "kit":
        import json
        reg = Registry(c.PKI / "devices.json")
        rec = reg.devices.get(args.device)
        if rec is None:
            raise SystemExit(f"unknown device {args.device}")
        junction = next((j for j in junctions if j["id"] == rec.get("junction")), {})
        via = reg.devices.get(rec["district"]) if rec.get("district") else None
        print(json.dumps(make_kit(rec, c.PKI, args.host, c.DEVICE_PORT, junction.get("approaches"), via=via), indent=1))
        return
    if args.cmd == "rotate-gateway":
        try:
            cert = trust.rotate(c.PKI, args.root)
        except (FileNotFoundError, ValueError) as err:
            raise SystemExit(str(err))
        print(f"gateway key: epoch {cert['epoch']}, {crypto.fingerprint(cert['gateway'])}")
        print("restart the platform; every unit adopts the new key at its next session and refuses the old one from then on")
        return
    if args.cmd == "list":
        for d in Registry(c.PKI / "devices.json").devices.values():
            where = d.get("address") if kind_of(d) == "district" else (d.get("junction") or "-")
            via = f" via {d['district']}" if d.get("district") else ""
            print(f"{d['id']:16} {kind_of(d):8} {where:16} {d['status']:8} {d.get('serial', ''):12} "
                  f"{crypto.fingerprint(d['public_key'])}{via}")
        return
    print(f"pki: {c.PKI} ({len(junctions)} junctions, {'created missing material' if created else 'nothing to do'}); "
          f"server certificate for {sorted(server_names(c.PKI))}")


if __name__ == "__main__":
    main()
