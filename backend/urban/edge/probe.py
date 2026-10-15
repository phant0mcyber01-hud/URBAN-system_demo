"""What a real unit knows about its cabinet, from sensors that are actually there.

Nothing is made up: a sensor that is not connected reports None, and the UI shows a dash.
    temperature  a file with a number: a Linux thermal zone (/sys/class/thermal/thermal_zone0/temp,
                 millidegrees: the computer's own board) or a 1-Wire DS18B20 in the cabinet
                 (/sys/bus/w1/devices/28-*/w1_slave, "t=23125" on its second line)
    door         a file whose content tells the switch position: a GPIO value file, or one written by a
                 small helper; DOOR_OPEN (default "1") means open
The demo fleet does not use this: its temperatures and doors are a script, and the demo says so.
"""
import os
import re
from pathlib import Path

BOARD_TEMP = Path("/sys/class/thermal/thermal_zone0/temp")


def read_temperature(path: Path) -> float | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if (m := re.search(r"t=(-?\d+)", text)) is not None:  # DS18B20: millidegrees after "t="
        return round(int(m.group(1)) / 1000, 1)
    if "NO" in text.split("\n")[0]:  # DS18B20 with a failed CRC
        return None
    try:
        value = float(text.split()[0])
    except (ValueError, IndexError):
        return None
    value = value / 1000 if abs(value) > 200 else value  # sysfs gives millidegrees
    return round(value, 1) if -60 <= value <= 150 else None


def read_door(path: Path, open_value: str = "1") -> str | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    return "open" if text == open_value else "closed"


class Probe:
    def __init__(self, temp: str | None = None, door: str | None = None, door_open: str = "1"):
        """temp: path of the temperature file, "" or None for the board's own thermal zone when there is one;
        door: path of the door switch file, "" or None when no switch is wired."""
        if temp:
            self.temp_path = Path(temp)
        else:
            self.temp_path = BOARD_TEMP if os.name == "posix" and BOARD_TEMP.exists() else None
        self.door_path = Path(door) if door else None
        self.door_open = door_open

    def __call__(self) -> dict:
        return {"temp": read_temperature(self.temp_path) if self.temp_path else None,
                "door": read_door(self.door_path, self.door_open) if self.door_path else None}

    def describe(self) -> str:
        return (f"temperature {self.temp_path or 'not connected'}, "
                f"door switch {self.door_path or 'not connected'}")
