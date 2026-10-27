"""The program of one pump station's unit, on the machine in the station's cabinet. It only reads:

    python -m urban.modules.water --unit pump-S-01 --platform city.example:8443 --server-name city.example \\
        --pki /etc/urban/pki [--site S-01] [--adapter emulated|modbus-tcp|modbus-rtu] [--map station-map.json] \\
        [--host 10.0.0.5 --port 502 --slave 1] [--serial /dev/ttyUSB0 --baud 9600 --slave 1] \\
        [--lon 71.78 --lat 40.39] [--status /run/urban/water/water.json]

The site is the id after "pump-" unless --site says otherwise. Trust and enrolment are those of any unit
(python -m urban.edge --keygen --unit pump-S-01, then the kit; the key is <pki>/edge/<unit>.key). It reads the station
about once a second (modbus-*: through ReadOnlyModbus and the register map, docs/water-module.md), reports every
5 seconds, refuses every command of the centre and takes only signed OS updates. What it works out (kWh per m3, the
night minimum) is kept in <pki>/edge/<unit>.water.json. See urban/modules/water/device.py.

An empty value (MAP=, HOST= in the unit's env file) counts as not given, so the service can pass every option always.
"""
import argparse
import asyncio
import ssl
from pathlib import Path

from ... import config as c
from ...devices.signer import SignerError
from ...edge.update import make_updater
from .adapters import EmulatedStation, ModbusStation
from .device import WaterStation
from .observe import Observations
from .readonly import ReadOnlyModbus, RtuTransport, TcpTransport
from .regmap import load_map
from .session import StationSession, build_tls, load_trust


def _optional(kind):
    """An argparse type for which an empty string is None (an unset line of the env file)."""
    def parse(text: str):
        text = text.strip()
        return None if text == "" else kind(text)
    parse.__name__ = kind.__name__
    return parse


def make_adapter(args):
    if args.adapter == "emulated":
        return EmulatedStation(fault_pump=args.fault_pump)
    if not args.map:
        raise SystemExit(f"--adapter {args.adapter} needs --map (the station's register map, JSON; a template is "
                         "urban/modules/water/map-template.json)")
    try:
        regmap = load_map(args.map)
    except ValueError as err:
        raise SystemExit(str(err))
    slave = 1 if args.slave is None else args.slave
    try:
        if args.adapter == "modbus-tcp":
            if not args.host:
                raise SystemExit("--adapter modbus-tcp needs --host (the automation's address)")
            transport = TcpTransport(args.host, 502 if args.port is None else args.port, unit=slave)
        else:
            if not args.serial:
                raise SystemExit("--adapter modbus-rtu needs --serial (e.g. /dev/ttyUSB0)")
            transport = RtuTransport(args.serial, unit=slave, baud=9600 if args.baud is None else args.baud)
    except ValueError as err:
        raise SystemExit(str(err))
    return ModbusStation(ReadOnlyModbus(transport), regmap)


def build(args) -> StationSession:
    unit = args.unit
    site = args.site or (unit[len("pump-"):] if unit.startswith("pump-") else "")
    if not site:
        raise SystemExit("--site is needed when the unit id does not look like pump-<site>")
    pki = Path(args.pki)
    adapter = make_adapter(args)  # a wrong map stops here, before anything else
    try:
        key, gateway_public, root_public = load_trust(pki, unit, args.key, args.gateway_pub, args.root)
    except (OSError, ValueError, SignerError) as err:
        raise SystemExit(f"{unit}: the device key or the centre's keys cannot be read ({err}); enrol first: "
                         f"python -m urban.edge --keygen --unit {unit}")
    try:
        context = build_tls(pki, Path(args.ca) if args.ca else None)
    except (OSError, ssl.SSLError) as err:
        raise SystemExit(f"the platform's CA certificate cannot be read: {err}")
    try:
        st = WaterStation(unit, site, key, gateway_public, adapter,
                          state_path=Path(args.state or pki / "edge" / f"{unit}.seq"), root_public=root_public,
                          gateway_state=pki / "edge" / f"{unit}.gateway.json",
                          updater=make_updater(args.platform, args.server_name, context),
                          observations=Observations(pki / "edge" / f"{unit}.water.json"), lon=args.lon, lat=args.lat)
    except ValueError as err:
        raise SystemExit(str(err))
    return StationSession(st, f"wss://{args.platform}/device", context, args.server_name,
                          Path(args.status) if args.status else None)


def parser() -> argparse.ArgumentParser:
    text = _optional(str)
    ap = argparse.ArgumentParser(description="URBAN pump station unit (reads only)")
    ap.add_argument("--unit", required=True, help="this device's id, e.g. pump-S-01")
    ap.add_argument("--platform", default=f"127.0.0.1:{c.DEVICE_PORT}", help="host:port of the platform's device listener")
    ap.add_argument("--server-name", default="localhost", help="name on the platform's certificate")
    ap.add_argument("--pki", default=str(c.PKI), help="folder with ca.pem, root.pub and edge/<unit>.key")
    ap.add_argument("--site", type=text, help="the station this device serves (default: the id after pump-)")
    ap.add_argument("--adapter", choices=("emulated", "modbus-tcp", "modbus-rtu"), default="emulated",
                    help="emulated: a station in software (demo, checks); modbus-tcp / modbus-rtu: the station's "
                         "automation, read only")
    ap.add_argument("--map", type=text, help="modbus-*: the station's register map (JSON)")
    ap.add_argument("--host", type=text, help="modbus-tcp: the automation's address")
    ap.add_argument("--port", type=_optional(int), help="modbus-tcp: its port (502)")
    ap.add_argument("--slave", type=_optional(int), help="modbus-*: the Modbus unit id (1)")
    ap.add_argument("--serial", type=text, help="modbus-rtu: the serial port, e.g. /dev/ttyUSB0")
    ap.add_argument("--baud", type=_optional(int), help="modbus-rtu: line speed (9600)")
    ap.add_argument("--fault-pump", type=_optional(int), help="emulated: this pump (1, 2) is in fault (checks)")
    ap.add_argument("--lon", type=_optional(float), help="longitude of the station, degrees east (for its screen)")
    ap.add_argument("--lat", type=_optional(float), help="latitude of the station, degrees north")
    ap.add_argument("--status", type=text, help="file rewritten at every report with the station's state (the "
                                                "machine's screen; on URBAN OS /run/urban/water/water.json)")
    for name in ("--key", "--ca", "--gateway-pub", "--root", "--state"):
        ap.add_argument(name, type=text)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    runner = build(args)  # configuration errors stop here, with their message
    print(f"water: {args.unit} station {runner.st.site}, platform {runner.url}, "
          f"adapter {runner.st.adapter.__class__.__name__}, read only", flush=True)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
