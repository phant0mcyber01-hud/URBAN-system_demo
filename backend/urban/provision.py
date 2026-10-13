"""Demo provisioning: TLS material, the gateway signing key and one identity per junction.

    python -m urban.provision

Writes to data/pki (not in git). Safe to run again: existing keys and statuses are kept.
"""
from . import config as c
from .devices.registry import provision
from .junctions import load_all_junctions


def main():
    junctions = load_all_junctions()
    created = provision(junctions, c.PKI)
    print(f"pki: {c.PKI} ({len(junctions)} devices, {'created missing material' if created else 'nothing to do'})")


if __name__ == "__main__":
    main()
