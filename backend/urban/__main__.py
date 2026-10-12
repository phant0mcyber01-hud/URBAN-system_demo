"""Run the platform server:  python -m urban"""
import uvicorn

from . import config as c


def main():
    uvicorn.run("urban.api.app:app", host="127.0.0.1", port=c.PORT)


if __name__ == "__main__":
    main()
