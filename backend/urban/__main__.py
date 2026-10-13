"""Run the platform:  python -m urban

Two listeners in one process: the UI and API on plain HTTP for this machine, and the device
endpoint on TLS. The Edge units are started as a separate process (see urban.edge).
"""
import asyncio

import uvicorn

from . import config as c


async def serve():
    ui = uvicorn.Server(uvicorn.Config("urban.api.app:app", host="127.0.0.1", port=c.PORT))
    devices = uvicorn.Server(uvicorn.Config(
        "urban.api.app:device_app", host="127.0.0.1", port=c.DEVICE_PORT, log_level="warning",
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
