"""Data pipeline: OSM -> SUMO network and demand -> GeoJSON layers in data/out.

    python -m urban.pipeline          full run (download, network, demand, layers)
    python -m urban.pipeline --geo    only re-export the map layers
"""
import sys

from .. import config as c
from .export import export_network
from .network import build_demand, build_network
from .osm import download, export_osm_layers


def main():
    c.OUT.mkdir(parents=True, exist_ok=True)
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
