"""Sunset and sunrise from the NOAA solar equations (the spreadsheet of the NOAA Global Monitoring Laboratory,
after Jean Meeus, "Astronomical Algorithms"): the sun's declination and the equation of time from the Julian
century, then the hour angle. Accurate to about a minute at mid latitudes, which is plenty for street lighting.

A street lamp is wanted from sunset to sunrise: while the sun is lower than 0.833 degrees below the horizon
(the sun's radius and the refraction of the air included), the same limit the usual almanacs use.
"""
from __future__ import annotations

import calendar
import datetime
import math

HORIZON = -0.833  # degrees: the sun's centre when its upper edge touches the horizon


def _sun(t: float) -> tuple[float, float]:
    """(declination in radians, equation of time in minutes) at the UTC epoch second t."""
    jc = (t / 86400.0 + 2440587.5 - 2451545.0) / 36525.0
    l0 = math.radians((280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360)
    m = math.radians(357.52911 + jc * (35999.05029 - 0.0001537 * jc))
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    centre = (math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
              + math.sin(2 * m) * (0.019993 - 0.000101 * jc) + math.sin(3 * m) * 0.000289)
    true_long = math.degrees(l0) + centre
    omega = math.radians(125.04 - 1934.136 * jc)
    apparent = math.radians(true_long - 0.00569 - 0.00478 * math.sin(omega))
    obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    obliq = math.radians(obliq + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(apparent))
    y = math.tan(obliq / 2) ** 2
    eqtime = 4 * math.degrees(y * math.sin(2 * l0) - 2 * e * math.sin(m) + 4 * e * y * math.sin(m) * math.cos(2 * l0)
                              - 0.5 * y * y * math.sin(4 * l0) - 1.25 * e * e * math.sin(2 * m))
    return decl, eqtime


def elevation(t: float, lat: float, lon: float) -> float:
    """The sun's height above the horizon, degrees, at the UTC epoch second t for lat/lon (east positive)."""
    decl, eqtime = _sun(t)
    minutes = (t % 86400.0) / 60.0
    hour_angle = math.radians(((minutes + eqtime + 4 * lon) % 1440.0) / 4.0 - 180.0)
    la = math.radians(lat)
    s = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def is_dark(t: float, lat: float, lon: float) -> bool:
    """Between sunset and sunrise (also in polar night; never in polar day)."""
    return elevation(t, lat, lon) < HORIZON


def sun_times(day: datetime.date, lat: float, lon: float) -> tuple[float, float] | None:
    """(sunrise, sunset) as UTC epoch seconds for the solar day of `day` at lat/lon: the one whose solar noon falls
    on that UTC date. None in polar day or polar night."""
    midnight = calendar.timegm(day.timetuple())
    noon = midnight + (720 - 4 * lon) * 60.0
    for _ in range(2):  # the equation of time moves a little during the day: refine once
        _, eqtime = _sun(noon)
        noon = midnight + (720 - 4 * lon - eqtime) * 60.0
    decl, _ = _sun(noon)
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90 - HORIZON)) - math.sin(la) * math.sin(decl)) / (math.cos(la) * math.cos(decl))
    if not -1.0 <= cos_ha <= 1.0:
        return None
    half = math.degrees(math.acos(cos_ha)) * 4 * 60.0  # seconds from solar noon to sunset
    # recompute with the declination at the event itself, which differs by a few seconds from the noon one
    out = []
    for sign in (-1, 1):
        t = noon + sign * half
        decl_t, _ = _sun(t)
        c = (math.cos(math.radians(90 - HORIZON)) - math.sin(la) * math.sin(decl_t)) / (math.cos(la) * math.cos(decl_t))
        c = max(-1.0, min(1.0, c))
        out.append(noon + sign * math.degrees(math.acos(c)) * 4 * 60.0)
    return out[0], out[1]
