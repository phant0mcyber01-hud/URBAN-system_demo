"""The power manager of URBAN OS: a unit keeps cool, servers size their work, and the CPU gets what was decided."""
import json
from pathlib import Path

import pytest

from urban.node import power as p


def test_roles():
    assert p.parse_roles(None) == ["unit"]
    assert p.parse_roles("center,district") == ["district", "center"]
    for bad in ("boss", "unit,center", "", ","):
        with pytest.raises(ValueError):
            p.parse_roles(bad if bad else "x,")


@pytest.mark.parametrize("temp,level,fps,clock", [
    (55, "cool", 4.0, 100), (72, "warm", 2.0, 80), (85, "hot", 1.0, 60), (95, "critical", 0.0, 50)])
def test_unit_backs_off_as_it_heats(temp, level, fps, clock):
    plan = p.decide(["unit"], temp, busy=30, cores=4)
    assert (plan["level"], plan["vision"]["fps"], plan["clock_pct"]) == (level, fps, clock)
    assert plan["vision"]["paused"] == (fps == 0)


@pytest.mark.parametrize("temp,level,fps,clock", [
    (75, "cool", 4.0, 100), (85, "warm", 4.0, 100), (92, "hot", 2.0, 80), (99, "critical", 1.0, 60)])
def test_server_keeps_working_longer_and_never_stops_vision(temp, level, fps, clock):
    plan = p.decide(["district"], temp, busy=30, cores=16)
    assert (plan["level"], plan["vision"]["fps"], plan["clock_pct"]) == (level, fps, clock)
    assert not plan["vision"]["paused"]


def test_levels_come_down_only_well_below_their_threshold():
    unit = p.UNIT
    assert p.level_for(78, unit, "hot") == "hot"  # 2 degrees under: stays
    assert p.level_for(74, unit, "hot") == "warm"  # 6 under: one step down
    assert p.level_for(70, unit, "critical") == "warm"  # 85 and 75 crossed, 65 not
    assert p.level_for(50, unit, "critical") == "cool"
    assert p.level_for(91, unit, "cool") == "critical"  # up at once


def test_the_screen_line_is_written_next_to_the_plan(tmp_path):
    plan = p.decide(["unit"], 72.5, busy=30, cores=4)
    p.write_plan(plan, tmp_path / "power.json")
    assert json.loads((tmp_path / "power.json").read_text())["level"] == "warm"
    assert (tmp_path / "power.txt").read_text() == "warm - 72.5 C, clock up to 80%, vision 2.0 frames/s per camera, 2 thread(s)\n"
    light = p.decide(["unit"], None, busy=1, cores=2, vision_off=True)
    assert p.screen_line(light) == "cool - no temperature sensor, clock up to 100%, no vision (light unit)"


def test_a_light_unit_plans_no_vision_work_whatever_is_configured():
    plan = p.decide(["unit"], 40, busy=30, cores=4, cameras=3, ms_per_frame=50, vision_off=True)
    assert plan["vision"] == p.NO_VISION and plan["vision"]["off"] and plan["vision"]["cameras"] == 0
    assert "vision=off" in p.summary(plan)
    assert not p.decide(["unit"], 40, busy=30, cores=4)["vision"]["off"]


def test_vision_off_comes_from_the_unit_conf(tmp_path):
    conf = tmp_path / "unit.conf"
    conf.write_text("UNIT=edge-J-07\nROLE=unit\nVISION=off\n")
    assert p.vision_off(conf)
    conf.write_text("UNIT=edge-J-07\nROLE=unit\n")
    assert not p.vision_off(conf)


def test_no_sensor_means_cool_and_says_so():
    plan = p.decide(["unit"], None, busy=10, cores=4)
    assert plan["level"] == "cool" and plan["sensor"] is False and plan["temp_c"] is None


def test_energy_preference():
    assert p.epp_for(90, p.UNIT, "cool") == "balance_performance"  # a unit never asks for full speed
    assert p.epp_for(90, p.SERVER, "cool") == "performance"
    assert p.epp_for(50, p.SERVER, "cool") == "balance_performance"
    assert p.epp_for(10, p.SERVER, "cool") == "balance_power"
    assert p.epp_for(90, p.UNIT, "warm") == "balance_power"
    assert p.epp_for(90, p.SERVER, "hot") == "power"


def test_vision_threads_follow_the_role_and_the_cores():
    assert p.decide(["unit"], 50, 0, cores=8)["vision"]["threads"] == 2
    assert p.decide(["district"], 50, 0, cores=16)["vision"]["threads"] == 15
    assert p.decide(["center"], 50, 0, cores=16)["vision"]["threads"] == 14  # two cores for the platform
    assert p.decide(["district", "center"], 50, 0, cores=16)["vision"]["threads"] == 14
    assert p.decide(["center"], 50, 0, cores=1)["vision"]["threads"] == 1


def test_camera_load_that_does_not_fit_lowers_the_frame_rate():
    plan = p.decide(["district"], 50, 0, cores=8, cameras=8, ms_per_frame=50)  # 8 cams x 4 fps x 50 ms = 1.6 s/s
    assert plan["vision"]["fps"] == 2.0 and plan["vision"]["overloaded"]
    light = p.decide(["district"], 50, 0, cores=8, cameras=2, ms_per_frame=20)
    assert light["vision"]["fps"] == 4.0 and not light["vision"]["overloaded"]


# ---------------------------------------------------------------- a fake machine

def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_temperature_takes_the_hottest_real_sensor(tmp_path):
    assert p.temperature(tmp_path) is None
    write(tmp_path / "sys/class/thermal/thermal_zone0/temp", "0")  # not wired
    write(tmp_path / "sys/class/thermal/thermal_zone1/temp", "61000")
    write(tmp_path / "sys/class/hwmon/hwmon0/name", "coretemp")
    write(tmp_path / "sys/class/hwmon/hwmon0/temp1_input", "66500")
    write(tmp_path / "sys/class/hwmon/hwmon1/name", "nvme")  # a disk, not the CPU
    write(tmp_path / "sys/class/hwmon/hwmon1/temp1_input", "80000")
    assert p.temperature(tmp_path) == 66.5


def test_cpu_load_from_proc_stat(tmp_path):
    write(tmp_path / "proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4\n")
    before = p.cpu_times(tmp_path)
    write(tmp_path / "proc/stat", "cpu  250 0 150 900 0 0 0 0 0 0\n")
    assert p.busy_pct(before, p.cpu_times(tmp_path)) == pytest.approx(200 / 300 * 100)
    assert p.busy_pct(None, before) == 0.0


def policy(root: Path, n: int, epp: bool, governors: str):
    d = root / f"sys/devices/system/cpu/cpufreq/policy{n}"
    write(d / "cpuinfo_max_freq", "3400000")
    write(d / "cpuinfo_min_freq", "400000")
    write(d / "scaling_max_freq", "3400000")
    write(d / "scaling_available_governors", governors)
    write(d / "scaling_governor", governors.split()[0])
    if epp:
        write(d / "energy_performance_preference", "balance_performance")
        write(d / "energy_performance_available_preferences", "default performance balance_performance balance_power power")
    return d


def test_apply_writes_what_was_decided_and_only_cpufreq(tmp_path):
    intel = policy(tmp_path, 0, epp=True, governors="performance powersave")
    acpi = policy(tmp_path, 1, epp=False, governors="conservative ondemand userspace powersave performance schedutil")
    hot = p.decide(["unit"], 85, busy=90, cores=4)  # 60 % clock, energy preference "power"
    written = p.apply(hot, tmp_path)
    assert (intel / "scaling_max_freq").read_text() == "2040000"
    assert (intel / "scaling_governor").read_text() == "powersave"
    assert (intel / "energy_performance_preference").read_text() == "power"
    assert (acpi / "scaling_max_freq").read_text() == "2040000"
    assert (acpi / "scaling_governor").read_text() == "schedutil"
    assert all(path.startswith("sys/devices/system/cpu/cpufreq/") for path, _ in written)
    busy = p.decide(["center"], 50, busy=90, cores=16)
    p.apply(busy, tmp_path)
    assert (acpi / "scaling_governor").read_text() == "performance"
    assert (intel / "energy_performance_preference").read_text() == "performance"
    assert (intel / "scaling_max_freq").read_text() == "3400000"
    assert p.apply(busy, tmp_path) == []  # nothing to change the second time


def test_the_clock_is_never_set_below_the_cpu_minimum(tmp_path):
    d = policy(tmp_path, 0, epp=False, governors="powersave")
    write(d / "cpuinfo_min_freq", "2000000")
    p.apply(p.decide(["unit"], 95, 0, 4), tmp_path)  # 50 % of 3.4 GHz is under the minimum
    assert (d / "scaling_max_freq").read_text() == "2000000"


def test_the_service_writes_a_plan_for_its_role(tmp_path):
    conf = tmp_path / "var/lib/urban/etc/unit.conf"
    write(conf, "UNIT=center\nSERIAL=\nROLE=district,center\n")
    write(conf.parent / "cameras.json", json.dumps([{"camera": "c1"}, {"camera": "c2"}]))
    write(tmp_path / "sys/class/thermal/thermal_zone0/temp", "45000")
    for n in range(4):
        (tmp_path / f"sys/devices/system/cpu/cpu{n}").mkdir(parents=True)
    plan_path = tmp_path / "run/urban/power.json"
    plan = p.main(["--root", str(tmp_path), "--conf", str(conf), "--plan", str(plan_path), "--once"])
    saved = json.loads(plan_path.read_text())
    assert saved["roles"] == ["district", "center"] and saved["level"] == "cool"
    assert saved["vision"]["cameras"] == 2 and saved["vision"]["threads"] == 2 and saved["cores"] == 4
    assert plan["temp_c"] == 45.0
