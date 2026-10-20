"""The program of one street-lighting cabinet, on the machine in the cabinet:

    python -m urban.modules.lighting --unit light-C-01 --platform city.example:8443 --server-name city.example \\
        --pki /etc/urban/pki [--adapter emulated|relay] [--relay-device /sys/class/gpio/gpio17/value] \\
        [--lon 71.78 --lat 40.39] [--status /run/urban/lighting.json] [--cabinet C-01]

The cabinet is the id after "light-" unless --cabinet says otherwise. Trust and enrolment are those of any unit
(python -m urban.edge --keygen --unit light-C-01, then the kit; the key is <pki>/edge/<unit>.key). It obeys only
commands signed by the centre for its own cabinet, and lives by its astronomical schedule (or its photocell) when
no command holds: with the link down, the lease of a command simply runs out. See urban/modules/lighting/device.py.
"""
import argparse
import asyncio
import ssl
from pathlib import Path

from ... import config as c
from ...edge.update import make_updater
from .device import EmulatedAdapter, LightingCabinet, RelayAdapter
from .session import CabinetSession, build_tls, load_trust

FERGANA = (71.78, 40.39)  # the pilot city: used, with a warning, when a cabinet is not told where it stands


def make_adapter(args):
    if args.adapter == "relay":
        if not args.relay_device:
            raise SystemExit("--adapter relay needs --relay-device (a GPIO value file such as "
                             "/sys/class/gpio/gpio17/value, or the file a relay board exposes)")
        return RelayAdapter(args.relay_device, dimming=args.dimming, lamps=args.lamps or 0, watts=args.lamp_watts,
                            door_path=args.door, door_open=args.door_open, photocell_path=args.photocell,
                            photocell_dark=args.photocell_dark)
    return EmulatedAdapter(lamps=args.lamps or 24, failed=args.failed, seed=args.seed, watts=args.lamp_watts)


def build(args) -> CabinetSession:
    unit = args.unit
    cabinet = args.cabinet or (unit[len("light-"):] if unit.startswith("light-") else "")
    if not cabinet:
        raise SystemExit("--cabinet is needed when the unit id does not look like light-<cabinet>")
    pki = Path(args.pki)
    try:
        key, gateway_public, root_public = load_trust(pki, unit, args.key, args.gateway_pub, args.root)
    except (OSError, ValueError) as err:
        raise SystemExit(f"{unit}: the device key or the centre's keys cannot be read ({err}); enrol first: "
                         f"python -m urban.edge --keygen --unit {unit}")
    try:
        context = build_tls(pki, Path(args.ca) if args.ca else None)
    except (OSError, ssl.SSLError) as err:
        raise SystemExit(f"the platform's CA certificate cannot be read: {err}")
    if args.lon is None or args.lat is None:
        print(f"lighting: no --lon/--lat: using Fergana {FERGANA}; the schedule is only as right as the position",
              flush=True)
    lon = FERGANA[0] if args.lon is None else args.lon
    lat = FERGANA[1] if args.lat is None else args.lat
    try:
        cab = LightingCabinet(unit, cabinet, key, gateway_public, make_adapter(args), lon, lat,
                              state_path=Path(args.state or pki / "edge" / f"{unit}.seq"), root_public=root_public,
                              gateway_state=pki / "edge" / f"{unit}.gateway.json",
                              updater=make_updater(args.platform, args.server_name, context))
    except ValueError as err:
        raise SystemExit(str(err))
    return CabinetSession(cab, f"wss://{args.platform}/device", context, args.server_name,
                          Path(args.status) if args.status else None)


def main():
    ap = argparse.ArgumentParser(description="URBAN street-lighting cabinet")
    ap.add_argument("--unit", required=True, help="this device's id, e.g. light-C-01")
    ap.add_argument("--platform", default=f"127.0.0.1:{c.DEVICE_PORT}", help="host:port of the platform's device listener")
    ap.add_argument("--server-name", default="localhost", help="name on the platform's certificate")
    ap.add_argument("--pki", default=str(c.PKI), help="folder with ca.pem, root.pub and edge/<unit>.key")
    ap.add_argument("--cabinet", help="the cabinet this device serves (default: the id after light-)")
    ap.add_argument("--adapter", choices=("emulated", "relay"), default="emulated",
                    help="emulated: lamps in software (demo, checks); relay: the cabinet's contactor through a file")
    ap.add_argument("--relay-device", help="relay: file the output is written to ('1'/'0', or the level with --dimming)")
    ap.add_argument("--dimming", action="store_true", help="relay: write the level 0-100 instead of 1/0")
    ap.add_argument("--lamps", type=int, help="lamps in the cabinet's circuit (emulated: 24; relay: unknown = 0)")
    ap.add_argument("--lamp-watts", type=float, default=90.0, help="power of one lamp, watts")
    ap.add_argument("--failed", type=int, help="emulated: lamps that do not light")
    ap.add_argument("--seed", type=int, help="emulated: a seed that may give the cabinet one failed lamp")
    ap.add_argument("--door", help="relay: door switch file; without it the door is 'unknown', not 'closed'")
    ap.add_argument("--door-open", default="1", help="relay: content of the door file that means open")
    ap.add_argument("--photocell", help="relay: photocell input file; without it the schedule is astronomical")
    ap.add_argument("--photocell-dark", default="1", help="relay: content of the photocell file that means dark")
    ap.add_argument("--lon", type=float, help="longitude of the cabinet, degrees east")
    ap.add_argument("--lat", type=float, help="latitude of the cabinet, degrees north")
    ap.add_argument("--status", help="file rewritten at every report with the cabinet's state (the machine's screen)")
    for name in ("--key", "--ca", "--gateway-pub", "--root", "--state"):
        ap.add_argument(name)
    args = ap.parse_args()
    runner = build(args)  # configuration errors stop here, with their message
    print(f"lighting: {args.unit} cabinet {runner.cab.cabinet}, platform {runner.url}, "
          f"adapter {runner.cab.adapter.__class__.__name__}, at {runner.cab.lon}E {runner.cab.lat}N", flush=True)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
