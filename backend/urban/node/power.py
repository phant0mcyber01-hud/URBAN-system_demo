"""Power of an URBAN OS machine, sized to its role and its work.

    python -m urban.node.power [--once] [--root /] [--plan /run/urban/power.json]

One OS runs on three kinds of machine (docs/edge-os.md):
  unit      the box in the junction's cabinet: a closed metal box in the sun, 55-65 C inside in summer. It must
            not overheat. As it heats it analyses fewer camera frames and lowers its clock, and before it gets
            dangerous it pauses vision altogether. The control path (the unit's link to the controller and to the
            platform, its leases and fall-backs) is never touched: that work is tiny and must always run.
  district  the district's server cabinet: cooled, does the camera work for many junctions. It keeps full work
            longer, gives vision every core it can spare and asks the CPU for more speed when it is busy.
  center    the control centre in the office: like a district server, with cores kept for the platform.
A machine may have two roles (the pilot's district server and centre are one machine).

Every few seconds the manager reads the temperature (thermal zones, hwmon), the CPU load (/proc/stat) and the
camera load (what vision reports), decides a plan, applies the CPU part of it through cpufreq (clock ceiling,
energy preference, governor) and writes the plan to /run/urban/power.json, where vision reads its frame rate and
thread count. Levels go up at once and come down only 5 degrees below their threshold, so the plan does not
flap. Without a temperature sensor (some boards, virtual machines) it assumes "cool" and says so; without
cpufreq it decides but has nothing to write. Nothing here can stop the unit's control service.
"""
import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

ROLES = ("unit", "district", "center")
LEVELS = ("cool", "warm", "hot", "critical")
HYSTERESIS = 5.0  # degrees below a level's threshold before the plan steps back down
EPP = ("power", "balance_power", "balance_performance", "performance")  # energy_performance_preference, low to high
BUDGET = 0.8  # share of the detector's capacity that vision may plan to use
CONSOLE = "/usr/local/bin/urban-console"


@dataclass(frozen=True)
class Profile:
    warm: float  # degrees C where each level starts
    hot: float
    critical: float
    clock: dict  # level -> ceiling as a share of the CPU's top clock
    fps: dict  # level -> share of the base frame rate; 0 pauses vision
    top_epp: str  # the most speed this kind of machine may ask for
    max_threads: int  # vision threads at most (0: as many as the cores allow)

    def threshold(self, level: str) -> float:
        return {"warm": self.warm, "hot": self.hot, "critical": self.critical}.get(level, float("-inf"))


# a fanless box in a hot cabinet: an N100-class CPU runs to about 105 C, the cabinet's other parts much less
UNIT = Profile(warm=70, hot=80, critical=90,
               clock={"cool": 1.0, "warm": 0.8, "hot": 0.6, "critical": 0.5},
               fps={"cool": 1.0, "warm": 0.5, "hot": 0.25, "critical": 0.0},
               top_epp="balance_performance", max_threads=2)
# a cooled server: full work until it is really hot, never a full stop of vision
SERVER = Profile(warm=80, hot=90, critical=98,
                 clock={"cool": 1.0, "warm": 1.0, "hot": 0.8, "critical": 0.6},
                 fps={"cool": 1.0, "warm": 1.0, "hot": 0.5, "critical": 0.25},
                 top_epp="performance", max_threads=0)


def parse_roles(text: str | None) -> list[str]:
    """ROLE=unit | district | center | district,center; anything unknown is refused, nothing given means unit."""
    roles = [r.strip() for r in (text or "unit").split(",") if r.strip()]
    if not roles or any(r not in ROLES for r in roles) or ("unit" in roles and len(roles) > 1):
        raise ValueError(f"role {text!r}: unit, district, center or district,center")
    return sorted(set(roles), key=ROLES.index)


def profile_for(roles: list[str]) -> Profile:
    return UNIT if "unit" in roles else SERVER


def reserve_for(roles: list[str]) -> int:
    """Cores vision leaves alone: the unit's own control work, or the platform on the centre."""
    return 2 if "center" in roles else 1


def level_for(temp: float | None, profile: Profile, previous: str | None = None) -> str:
    if temp is None:
        return "cool"
    raw = "cool"
    for level in LEVELS[1:]:
        if temp >= profile.threshold(level):
            raw = level
    if previous not in LEVELS or LEVELS.index(raw) >= LEVELS.index(previous):
        return raw
    level = previous  # cooling down: one level at a time, each only well below its threshold
    while LEVELS.index(level) > LEVELS.index(raw) and temp <= profile.threshold(level) - HYSTERESIS:
        level = LEVELS[LEVELS.index(level) - 1]
    return level


def epp_for(busy: float, profile: Profile, level: str) -> str:
    want = "performance" if busy >= 75 else "balance_performance" if busy >= 40 else "balance_power"
    if level in ("hot", "critical"):
        want = "power"
    elif level == "warm":
        want = min(want, "balance_power", key=EPP.index)
    return min(want, profile.top_epp, key=EPP.index)


def vision_plan(profile: Profile, roles: list[str], level: str, cores: int, cameras: int, base_fps: float,
                ms_per_frame: float | None) -> dict:
    threads = max(1, cores - reserve_for(roles))
    if profile.max_threads:
        threads = min(threads, profile.max_threads)
    fps = base_fps * profile.fps[level]
    overloaded = False
    if fps > 0 and cameras and ms_per_frame:  # the work must fit the detector: cameras x fps frames a second
        fits = BUDGET * 1000.0 / ms_per_frame / cameras
        if fps > fits:
            fps, overloaded = max(fits, 0.2), True
    return {"fps": round(fps, 2), "threads": threads, "cameras": cameras, "overloaded": overloaded,
            "paused": fps == 0, "off": False}


NO_VISION = {"fps": 0.0, "threads": 0, "cameras": 0, "overloaded": False, "paused": False, "off": True}


def decide(roles: list[str], temp: float | None, busy: float, cores: int, cameras: int = 0,
           ms_per_frame: float | None = None, base_fps: float = 4.0, previous: str | None = None,
           vision_off: bool = False) -> dict:
    """vision_off: a light unit (VISION=off): no camera work is planned, whatever is configured."""
    profile = profile_for(roles)
    level = level_for(temp, profile, previous)
    return {
        "roles": roles, "level": level, "temp_c": None if temp is None else round(temp, 1), "sensor": temp is not None,
        "busy_pct": round(busy, 1), "cores": cores, "clock_pct": round(profile.clock[level] * 100),
        "epp": epp_for(busy, profile, level),
        "vision": dict(NO_VISION) if vision_off else vision_plan(profile, roles, level, cores, cameras, base_fps, ms_per_frame),
    }


# ---------------------------------------------------------------- reading the machine

def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def temperature(root: Path) -> float | None:
    """The hottest CPU or board sensor, in degrees C; None when the machine has none."""
    found = []
    for zone in sorted((root / "sys/class/thermal").glob("thermal_zone*")):
        found.append(_read(zone / "temp"))
    for mon in sorted((root / "sys/class/hwmon").glob("hwmon*")):
        if _read(mon / "name") in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            found += [_read(p) for p in sorted(mon.glob("temp*_input"))]
    temps = []
    for raw in found:
        try:
            t = int(raw) / 1000.0
        except (TypeError, ValueError):
            continue
        if 0 < t < 150:  # sensors that are not wired report 0 or nonsense
            temps.append(t)
    return max(temps) if temps else None


def cpu_times(root: Path) -> tuple[int, int] | None:
    """(busy, total) jiffies from the first line of /proc/stat."""
    line = (_read(root / "proc/stat") or "").splitlines()[:1]
    if not line or not line[0].startswith("cpu "):
        return None
    values = [int(v) for v in line[0].split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    return sum(values) - idle, sum(values)


def busy_pct(before: tuple[int, int] | None, after: tuple[int, int] | None) -> float:
    if not before or not after or after[1] <= before[1]:
        return 0.0
    return 100.0 * (after[0] - before[0]) / (after[1] - before[1])


def cores(root: Path) -> int:
    n = len([p for p in (root / "sys/devices/system/cpu").glob("cpu[0-9]*") if p.name[3:].isdigit()])
    return n or os.cpu_count() or 1


def camera_load(run: Path, etc: Path) -> tuple[int, float | None]:
    """How many cameras vision serves and how long a frame takes, as vision reports it; before vision runs,
    the cameras configured."""
    try:  # vision writes its stats next to its queue file, in its own directory
        stats = json.loads((run / "q" / "vision.json").read_text())
        return int(stats.get("cameras", 0)), stats.get("ms_per_frame")
    except (OSError, ValueError):
        pass
    try:
        return len(json.loads((etc / "cameras.json").read_text())), None
    except (OSError, ValueError, TypeError):
        return 0, None


# ---------------------------------------------------------------- applying the CPU part

def apply(plan: dict, root: Path) -> list[tuple[str, str]]:
    """Writes the clock ceiling, energy preference and governor into every cpufreq policy. Returns what it wrote."""
    written = []

    def put(path: Path, value: str):
        if _read(path) == value:
            return
        try:
            path.write_text(value)
            written.append((path.relative_to(root).as_posix(), value))
        except OSError:
            pass  # a driver that refuses a value: the plan still stands for vision

    for policy in sorted((root / "sys/devices/system/cpu/cpufreq").glob("policy*")):
        top, low = _read(policy / "cpuinfo_max_freq"), _read(policy / "cpuinfo_min_freq")
        if top and top.isdigit():
            ceiling = int(int(top) * plan["clock_pct"] / 100)
            if low and low.isdigit():
                ceiling = max(ceiling, int(low))
            put(policy / "scaling_max_freq", str(ceiling))
        governors = (_read(policy / "scaling_available_governors") or "").split()
        epp_file = policy / "energy_performance_preference"
        if epp_file.exists():  # intel_pstate, amd-pstate-epp: the preference is the knob, under "powersave"
            if "powersave" in governors:
                put(policy / "scaling_governor", "powersave")
            prefs = (_read(policy / "energy_performance_available_preferences") or " ".join(EPP)).split()
            if plan["epp"] in prefs:
                put(epp_file, plan["epp"])
        elif governors:  # acpi-cpufreq and the like: the governor is the knob
            want = "performance" if plan["epp"] == "performance" else None
            want = want if want in governors else next((g for g in ("schedutil", "ondemand", "powersave") if g in governors), None)
            if want:
                put(policy / "scaling_governor", want)
    return written


# ---------------------------------------------------------------- the service

def roles_from(conf: Path) -> list[str]:
    text = _read(conf) or ""
    role = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("ROLE=")), None)
    return parse_roles(role)


def vision_off(conf: Path) -> bool:
    """VISION=off in unit.conf: a light unit, no camera work ever."""
    text = _read(conf) or ""
    return any(line.strip().lower() == "vision=off" for line in text.splitlines())


def screen_line(plan: dict) -> str:
    """The plan as the machine's screen shows it (urban-console reads it as text: no interpreter per redraw)."""
    v = plan["vision"]
    temp = f"{plan['temp_c']} C" if plan["sensor"] else "no temperature sensor"
    work = ("no vision (light unit)" if v.get("off") else "vision paused (too hot)" if v["paused"]
            else f"vision {v['fps']} frames/s per camera, {v['threads']} thread(s)")
    return f"{plan['level']} - {temp}, clock up to {plan['clock_pct']}%, {work}"


def write_plan(plan: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    for target, text in ((path, json.dumps(plan, indent=1)), (path.with_suffix(".txt"), screen_line(plan) + "\n")):
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(target)
        try:
            target.chmod(0o644)  # vision runs as the urban user and only reads it
        except OSError:
            pass


def summary(plan: dict) -> str:
    v = plan["vision"]
    temp = f"{plan['temp_c']}C" if plan["sensor"] else "no-sensor"
    work = "off" if v.get("off") else "paused" if v["paused"] else f"{v['fps']}fps/{v['threads']}thr" + (" overloaded" if v["overloaded"] else "")
    return (f"URBAN-POWER roles={','.join(plan['roles'])} level={plan['level']} temp={temp} busy={plan['busy_pct']}% "
            f"clock={plan['clock_pct']}% epp={plan['epp']} vision={work} cameras={v['cameras']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="URBAN OS power manager")
    ap.add_argument("--root", default="/", help="file system root (tests use a fake one)")
    ap.add_argument("--conf", default="/var/lib/urban/etc/unit.conf", help="ROLE= is read from here")
    ap.add_argument("--plan", default="/run/urban/power.json")
    ap.add_argument("--base-fps", type=float, default=4.0, help="frames per second per camera when cool")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    root, plan_path = Path(args.root), Path(args.plan)
    roles = roles_from(Path(args.conf))
    light = vision_off(Path(args.conf))
    level, said, before = None, None, cpu_times(root)
    while True:
        time.sleep(0 if args.once else args.interval)
        after = cpu_times(root)
        cams, ms = (0, None) if light else camera_load(plan_path.parent, Path(args.conf).parent)
        plan = decide(roles, temperature(root), busy_pct(before, after), cores(root), cams, ms, args.base_fps, level,
                      vision_off=light)
        before, level = after, plan["level"]
        plan["applied"] = len(apply(plan, root))
        plan["at"] = round(time.time(), 1)
        write_plan(plan, plan_path)
        line = summary(plan)
        key = (plan["level"], plan["epp"], plan["vision"]["fps"], plan["vision"]["paused"])
        if key != said:  # say it when it changes, not every five seconds, and redraw the machine's screen
            print(line, flush=True)
            said = key
            if Path(CONSOLE).exists() and not args.once:
                subprocess.run([CONSOLE], check=False, timeout=30)
        if args.once:
            return plan


if __name__ == "__main__":
    main()
