"""Set up the real city the platform runs:

    python -m urban.city create --name "Фергана" --bbox 71.770,40.372,71.800,40.398
    python -m urban.city create --name "Фергана" --bbox ... --osm district.osm    from a file, no download
    python -m urban.city list                                                      junctions and their passports

`create` downloads OpenStreetMap for the area (west,south,east,north), writes the map layers to data/city
and finds the signalised junctions. Run it again after OSM changes: junctions keep their ids, and an
approved passport is never overwritten. Passports are approved in the UI (Перекрёстки) by an administrator.
Stop the platform first: it reads data/city at start.
"""
import argparse
import json
from pathlib import Path

from .. import config as c
from ..pipeline.osm import download
from .build import build


def main():
    ap = argparse.ArgumentParser(prog="python -m urban.city", description="the real city: map and junction passports")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cr = sub.add_parser("create", help="map and junctions from OpenStreetMap")
    cr.add_argument("--name", help="city or district name, shown in the UI (default: the one set up before)")
    cr.add_argument("--bbox", default=c.BBOX, help="west,south,east,north in degrees")
    cr.add_argument("--osm", help="an OSM XML file to use instead of downloading")
    sub.add_parser("list", help="junctions and the state of their passports")
    args = ap.parse_args()

    if args.cmd == "create":
        c.CITY.mkdir(parents=True, exist_ok=True)
        meta = c.CITY / "meta.json"
        if not args.name and meta.exists():
            args.name = json.loads(meta.read_text(encoding="utf-8")).get("name")
        if not args.name:
            raise SystemExit('name the city: python -m urban.city create --name "Фергана, центр" --bbox ...')
        osm = c.CITY / "city.osm"
        if args.osm:
            osm = Path(args.osm)
        else:
            download(args.bbox, osm)
        r = build(osm, c.CITY, args.name, args.bbox)
        print(f"{args.name}: {r['junctions']} signalised junctions ({r['new']} new, {r['kept']} kept as people left them), "
              f"{r['roads']} roads, {r['stops']} bus stops -> {c.CITY}")
        print("passports are drafts: check each against its controller and approve it in the UI (Перекрёстки)")
        return
    path = c.CITY / "signals.json"
    if not path.exists():
        raise SystemExit("no city yet: python -m urban.city create --name ... --bbox ...")
    for j in json.loads(path.read_text(encoding="utf-8")):
        p = j.get("passport", {})
        print(f"{j['id']:6} {p.get('status', '?'):9} v{p.get('version', 1):<3} {len(j['approaches'])} подъезда  {j['name']}")


if __name__ == "__main__":
    main()
