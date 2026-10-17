"""Run the platform:

    python -m urban           the real system: the city from data/city, real URBAN Edge units
    python -m urban --demo    the demo twin for presentations: SUMO traffic, synthetic junctions, emulated units

Two listeners in one process: the UI and API on plain HTTP (for this machine, or for a reverse
proxy on a server), and the device endpoint on TLS. Addresses come from .env: HOST, PORT,
DEVICE_HOST, DEVICE_PORT. The Edge units are a separate process (see urban.edge).
"""
import asyncio
import os
import sys

import uvicorn

if "--demo" in sys.argv:  # before the settings are read
    os.environ["URBAN_SOURCE"] = "simulation"

from . import config as c  # noqa: E402


async def serve():
    tls = {"ssl_certfile": str(c.PKI / "server.pem"), "ssl_keyfile": str(c.PKI / "server.key")} if c.UI_TLS else {}
    ui = uvicorn.Server(uvicorn.Config("urban.api.app:app", host=c.HOST, port=c.PORT, **tls))
    devices = uvicorn.Server(uvicorn.Config(
        "urban.api.app:device_app", host=c.DEVICE_HOST, port=c.DEVICE_PORT, log_level="warning",
        ssl_certfile=str(c.PKI / "server.pem"), ssl_keyfile=str(c.PKI / "server.key")))
    await asyncio.gather(ui.serve(), devices.serve())


def main():
    from .api import app as _  # noqa: F401  (provisions the demo keys before the TLS listener needs them)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
