"""Health of URBAN Edge units: what the platform concludes from what a unit reports about itself.

Rules are plain thresholds on the self-report (temperature, door, controller link, software
version) plus what the hub knows (session, last report). Each alert has a level: `warn` is
something to plan around, `alert` means the unit cannot be trusted with commands right now.
"""
import math

EXPECTED_VERSION = "0.1.0"
TEXT_MAX = 80                        # longest string a unit may put into its health report
TEMP_WARN, TEMP_ALERT = 55.0, 65.0   # degrees in the cabinet; see docs/hardware.md, section 7.3
STALE_REPORT = 10                    # seconds online without a report before it counts as silent

BLOCKING = {"door", "controller"}    # alerts that stop commands at that junction


def clean(health) -> dict:
    """What a unit says about itself, reduced to numbers and short words. Everything else is dropped:
    a unit is trusted for measurements, not for markup, and this is what reaches every browser and the history."""
    if not isinstance(health, dict):
        return {}
    out = {}
    temp = health.get("temp")
    if isinstance(temp, (int, float)) and not isinstance(temp, bool) and math.isfinite(temp):
        out["temp"] = round(float(temp), 1)
    for key in ("leases", "uptime", "buffered"):
        v = health.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 9:
            out[key] = v
    relay = health.get("relay")  # a district server: what its relay is doing, counts only
    if isinstance(relay, dict):
        out["relay"] = {k: v for k, v in relay.items() if k in ("units", "sessions", "refused", "bytes_up", "bytes_down")
                        and isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 15}
    if health.get("door") in ("open", "closed"):
        out["door"] = health["door"]
    for key in ("controller", "version"):
        v = health.get(key)
        if isinstance(v, str) and v:
            out[key] = v[:TEXT_MAX]
    return out


def assess(row: dict) -> list[dict]:
    """row: one entry of DeviceHub.status(). Returns [{kind, level, text}] sorted by severity."""
    out = []
    h = row.get("health") or {}
    if row["status"] == "revoked":
        out.append({"kind": "revoked", "level": "alert", "text": "устройство отозвано"})
    elif not row["online"] and "first_seen" in row and not row["first_seen"]:
        pass  # enrolled, never connected with this key: the installer has not finished yet
    elif not row["online"]:
        age = row.get("age")
        out.append({"kind": "offline", "level": "warn",
                    "text": "нет связи" + (f" {age // 60} мин" if age and age >= 60 else "")})
    elif (row.get("age") or 0) > STALE_REPORT:
        out.append({"kind": "silent", "level": "warn", "text": f"нет отчётов {row['age']} с"})

    if h.get("door") == "open":
        out.append({"kind": "door", "level": "alert", "text": "вскрыт шкаф"})
    if h.get("controller") not in (None, "ok"):
        out.append({"kind": "controller", "level": "alert", "text": f"контроллер: {h['controller']}"})
    temp = h.get("temp")
    if isinstance(temp, (int, float)):
        if temp >= TEMP_ALERT:
            out.append({"kind": "heat", "level": "alert", "text": f"перегрев {temp:.0f} °C"})
        elif temp >= TEMP_WARN:
            out.append({"kind": "heat", "level": "warn", "text": f"жарко {temp:.0f} °C"})
    version = h.get("version")
    if version and version != EXPECTED_VERSION:
        out.append({"kind": "version", "level": "warn", "text": f"ПО {version}, ожидается {EXPECTED_VERSION}"})

    order = {"alert": 0, "warn": 1}
    return sorted(out, key=lambda a: order[a["level"]])


def blocking(alerts: list[dict]) -> bool:
    """True if any alert means the unit must not receive or keep commands."""
    return any(a["kind"] in BLOCKING for a in alerts)
