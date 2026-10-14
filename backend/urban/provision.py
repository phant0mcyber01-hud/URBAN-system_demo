"""Provisioning: TLS material, the gateway signing key and one identity per junction.

    python -m urban.provision                       for this machine (DEVICE_NAME from .env, default localhost)
    python -m urban.provision --host city.example   the name devices will connect to; the server certificate covers it

Writes to data/pki (not in git). Safe to run again: existing keys and statuses are kept; only a server
certificate that does not cover the host is reissued (needs ca.key in the folder).
"""
import argparse

from . import config as c
from .devices.registry import provision, server_names
from .junctions import load_all_junctions


def main():
    ap = argparse.ArgumentParser(description="URBAN provisioning")
    ap.add_argument("--host", default=c.DEVICE_NAME, help="host name on the device listener's certificate")
    args = ap.parse_args()
    junctions = load_all_junctions()
    created = provision(junctions, c.PKI, host=args.host)
    print(f"pki: {c.PKI} ({len(junctions)} devices, {'created missing material' if created else 'nothing to do'}); "
          f"server certificate for {sorted(server_names(c.PKI))}")


if __name__ == "__main__":
    main()
