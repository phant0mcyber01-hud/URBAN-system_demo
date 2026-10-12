"""Message shapes exchanged over the bus (documentation of the contract)."""
from typing import NamedTuple, TypedDict


class Vehicle(NamedTuple):
    id: str
    lon: float
    lat: float
    angle: float   # degrees clockwise from north
    speed: float   # m/s
    kind: str      # car | bus | emg
    edge: str      # road segment id, ":"-prefixed inside junctions


class SourceFrame(TypedDict, total=False):
    t: int                  # source time, seconds
    vehicles: list[Vehicle]
    bus_loss: list[float]   # seconds lost per bus on the line
    events: list[dict]      # incident / dispatch events from city services
    telemetry: list[dict]   # utility asset readings (every few seconds)
