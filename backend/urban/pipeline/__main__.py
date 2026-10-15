"""Data pipeline: OSM -> SUMO network and demand -> GeoJSON layers in data/out.

    python -m urban.pipeline          full run (download, network, demand, layers)
    python -m urban.pipeline --geo    only re-export the map layers
    python -m urban.pipeline --links  only add the downstream links to the existing junction files
"""
import sys

from .. import config as c
from .export import export_network
from .network import build_demand, build_network
from .osm import download, export_osm_layers


def add_links():
    """Downstream links for junction files made before they existed, without rebuilding anything else."""
    import json

    from ..junctions import _junction
    from ..sources.sumo import network
    from .links import downstream
    path = c.OUT / "signals.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    junctions = {j["id"]: j for j in (_junction(p) for p in raw)}
    found = downstream(network(), list(junctions.values()))
    for p in raw:
        p["phases"] = junctions[p["id"]]["phases"]
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    cams = c.OUT / "cameras.geojson"
    layer = json.loads(cams.read_text(encoding="utf-8"))
    for f in layer["features"]:
        if f["properties"]["id"] in junctions:
            f["properties"]["phases"] = junctions[f["properties"]["id"]]["phases"]
    cams.write_text(json.dumps(layer, ensure_ascii=False), encoding="utf-8")
    print(f"downstream links: {found}")


def main():
    c.OUT.mkdir(parents=True, exist_ok=True)
    if "--links" in sys.argv:
        add_links()
        return
    if "--geo" not in sys.argv:
        download()
        build_network()
        build_demand()
    print(">> geojson")
    export_osm_layers()
    export_network()
    print("done:", c.OUT)


if __name__ == "__main__":
    main()
