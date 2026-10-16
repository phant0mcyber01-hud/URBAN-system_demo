"""Controllers described by a file instead of code: which SNMP objects or Modbus registers select a plan.

Most network controllers select a plan by writing one number somewhere, sometimes after arming a
loss-of-communication timer, and go back to local control by writing another. A profile says exactly that:

    {"name": "NTCIP 1202 pattern control", "transport": "snmp",
     "select":    [{"set": "1.3.6.1.4.1.1206.4.2.1.3.3.0", "value": "{backup}"},
                   {"set": "1.3.6.1.4.1.1206.4.2.1.4.14.0", "value": "{pattern}"}],
     "keepalive": "select",
     "base":      [{"set": "1.3.6.1.4.1.1206.4.2.1.4.14.0", "value": 0}],
     "confirm":   {"get": "1.3.6.1.4.1.1206.4.2.1.4.14.0", "equals": "{pattern}"},
     "patterns":  {"1": 1, "2": 2}}

transport  snmp (any version the unit's SNMP options give) or modbus-tcp ("register" instead of "set"/"get")
value      a number, "{pattern}" (the controller's plan number for our plan id) or "{backup}" (the timer, seconds)
patterns   our plan id -> the controller's plan number; plans not listed keep their own number
confirm    optional read-back after selecting: a controller that does not show the plan is sent back to base

Profiles are checked before use and refused when they could not be safe: no way back to base, a base step that
depends on the plan, a write outside the allowed value range, or anything but the documented keys. A profile of
a controller nobody has tried says so in its "status". Built-in profiles are in equipment/profiles/.
"""
import json
import re
from pathlib import Path

from .adapter import AdapterError, ControllerAdapter

BUILTIN = Path(__file__).with_name("profiles")
OID = re.compile(r"^1(\.\d+){4,40}$")
PLACEHOLDERS = {"{pattern}", "{backup}"}
TRANSPORTS = {"snmp": ("set", "get"), "modbus-tcp": ("register", "register")}
KEYS = {"name", "transport", "select", "keepalive", "base", "confirm", "patterns", "status", "notes", "vendor",
        "pattern_max", "unit"}


class ProfileError(ValueError):
    pass


def _step(step, transport: str, where: str, allow_pattern: bool):
    write_key = TRANSPORTS[transport][0]
    if not isinstance(step, dict) or set(step) != {write_key, "value"}:
        raise ProfileError(f"{where}: each step is {{\"{write_key}\": ..., \"value\": ...}}")
    target = step[write_key]
    if transport == "snmp" and not (isinstance(target, str) and OID.match(target)):
        raise ProfileError(f"{where}: {target!r} is not a numeric OID")
    if transport == "modbus-tcp" and not (isinstance(target, int) and 0 <= target <= 0xFFFF):
        raise ProfileError(f"{where}: register must be 0..65535")
    value = step["value"]
    if isinstance(value, str):
        if value not in PLACEHOLDERS:
            raise ProfileError(f"{where}: value must be a number, {{pattern}} or {{backup}}")
        if value == "{pattern}" and not allow_pattern:
            raise ProfileError(f"{where}: the way back to base cannot depend on the plan")
    elif not (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFF):
        raise ProfileError(f"{where}: values are whole numbers 0..65535")
    return {"target": target, "value": value}


def parse(profile: dict) -> dict:
    """Check a profile and return it in the form the adapter uses. ProfileError says what is wrong."""
    if not isinstance(profile, dict):
        raise ProfileError("a profile is a JSON object")
    unknown = set(profile) - KEYS
    if unknown:
        raise ProfileError(f"unknown keys: {', '.join(sorted(unknown))}")
    transport = profile.get("transport")
    if transport not in TRANSPORTS:
        raise ProfileError(f"transport must be one of {', '.join(TRANSPORTS)}")
    out = {"name": str(profile.get("name") or "unnamed profile"), "transport": transport,
           "status": str(profile.get("status", "untested")), "unit": int(profile.get("unit", 1))}
    for part in ("select", "base"):
        steps = profile.get(part)
        if not isinstance(steps, list) or not steps:
            raise ProfileError(f"{part}: at least one step is needed")
        out[part] = [_step(s, transport, f"{part}[{i}]", part == "select") for i, s in enumerate(steps)]
    if not any(s["value"] == "{pattern}" for s in out["select"]):
        raise ProfileError("select never writes {pattern}: it could not choose a plan")
    keep = profile.get("keepalive", "select")
    out["keepalive"] = out["select"] if keep == "select" else (
        [_step(s, transport, f"keepalive[{i}]", True) for i, s in enumerate(keep)] if isinstance(keep, list) and keep
        else None)
    if out["keepalive"] is None:
        raise ProfileError("keepalive is \"select\" or a list of steps")
    confirm = profile.get("confirm")
    if confirm is not None:
        read_key = TRANSPORTS[transport][1]
        if not isinstance(confirm, dict) or set(confirm) != {read_key, "equals"}:
            raise ProfileError(f"confirm is {{\"{read_key}\": ..., \"equals\": ...}}")
        out["confirm"] = _step({TRANSPORTS[transport][0]: confirm[read_key], "value": confirm["equals"]}, transport,
                               "confirm", True)
    patterns = profile.get("patterns", {})
    if not isinstance(patterns, dict):
        raise ProfileError("patterns maps plan ids to controller plan numbers")
    try:
        out["patterns"] = {int(k): int(v) for k, v in patterns.items()}
    except (TypeError, ValueError):
        raise ProfileError("patterns: whole numbers only")
    out["pattern_max"] = int(profile.get("pattern_max", 255))
    if any(not 1 <= v <= out["pattern_max"] for v in out["patterns"].values()):
        raise ProfileError("patterns: a controller plan number outside 1..pattern_max")
    return out


def load(name_or_path: str) -> dict:
    """A built-in profile by name (ntcip-1202) or a profile file."""
    path = Path(name_or_path)
    if not path.suffix:
        path = BUILTIN / f"{name_or_path}.json"
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except OSError as err:
        raise ProfileError(f"cannot read profile {name_or_path}: {err}")
    except json.JSONDecodeError as err:
        raise ProfileError(f"{path.name}: not JSON ({err})")


def builtin() -> list[str]:
    return sorted(p.stem for p in BUILTIN.glob("*.json"))


class ProfileAdapter(ControllerAdapter):
    def __init__(self, profile: dict, client, tables: dict[str, set[int]], backup_time: int = 180):
        """client: for snmp an SnmpClient/SnmpV3Client (set, get); for modbus-tcp a ModbusTcp."""
        self.p, self.client, self.tables, self.backup_time = profile, client, tables, backup_time
        self.last_error = None

    def learn(self, tls, plans):
        self.tables[tls] = set(plans)

    def plans(self, tls):
        return set(self.tables.get(tls, ()))

    def pattern(self, plan: int) -> int:
        return self.p["patterns"].get(plan, plan)

    def select(self, tls, plan):
        if plan not in self.plans(tls):
            raise AdapterError("plan is not loaded in this controller")
        number = self.pattern(plan)
        if not 1 <= number <= self.p["pattern_max"]:
            raise AdapterError(f"controller plan number {number} is outside 1..{self.p['pattern_max']}")
        self._run(self.p["select"], number)
        self._confirm(number)

    def keepalive(self, tls, plan):
        self._run(self.p["keepalive"], self.pattern(plan))

    def base(self, tls):
        self._run(self.p["base"], None)

    def _value(self, value, number):
        return {"{pattern}": number, "{backup}": self.backup_time}.get(value, value) if isinstance(value, str) else value

    def _run(self, steps, number):
        try:
            for s in steps:
                v = self._value(s["value"], number)
                if self.p["transport"] == "snmp":
                    self.client.set(s["target"], v)
                else:
                    self.client.write_register(s["target"], v)
            self.last_error = None
        except (OSError, ValueError) as err:
            self.last_error = str(err)
            raise AdapterError(f"{self.p['name']}: {err}") from err

    def _confirm(self, number):
        c = self.p.get("confirm")
        if c is None:
            return
        want = self._value(c["value"], number)
        try:
            got = self.client.get(c["target"]) if self.p["transport"] == "snmp" else self.client.read_holding(c["target"])[0]
        except (OSError, ValueError) as err:
            got, why = None, str(err)
        else:
            why = f"shows {got}, expected {want}"
        if got != want:
            try:
                self.base("")
            except AdapterError:
                pass
            raise AdapterError(f"{self.p['name']}: the controller did not take the plan ({why})")
