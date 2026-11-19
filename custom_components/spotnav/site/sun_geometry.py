"""Where the sun is, how much a clear sky gives, and what an array of tilted planes makes of an hour's irradiance.

Pure Python (math only), ported from the solar forecast experiment (`plans/benchmark/solar_models/geo.py` and
`solar.py`, round 2). Used by the own solar forecast in shadow (`site/solar_shadow.py`); nothing here plans.

* **Sun position**: NOAA's simplified equations, a fraction of a degree, plenty for hourly PV. Azimuths are compass
  degrees (0 = N, 90 = E).
* **Clear sky**: Haurwitz GHI (W/m2), and Erbs' diffuse share for the clear-sky beam share.
* **An hour** is `SUBSTEPS` points (the mid-points of five-minute steps). An hour's GHI is spread over them by the
  clear-sky shape (a clear-sky index that is flat in the hour).
* **Transposition**: isotropic sky (Liu-Jordan) with ground albedo `ALBEDO`; the beam's cos zenith floored at 5°
  elevation.
* **Array**: per plane `(tilt, azimuth, kWp)`, the plane-of-array irradiance times kWp / 1000, derated by 0.4 %/K of
  cell temperature over 25 °C (cell = air + 0.03 K per W/m2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Final

ALBEDO: Final = 0.2
#: Points per hour (five-minute mid-points).
SUBSTEPS: Final = 12
#: The clear-sky GHI (W/m2) an hour must average to count as daylight.
DAYLIGHT_W: Final = 5.0
#: Temperature derating: -0.4 % per K of cell temperature over 25 °C.
TEMP_COEFF: Final = 0.004
CELL_K_PER_W: Final = 0.03
#: A plane: (tilt degrees, compass azimuth degrees, kWp).
Plane = tuple[float, float, float]


def hour_key(moment: datetime) -> int:
    """An hour's key: the UTC hour `moment` falls in, as hours since the epoch."""
    return int(moment.timestamp()) // 3600


def key_start(key: int) -> datetime:
    """The UTC start of the hour `key`."""
    return datetime.fromtimestamp(key * 3600, timezone.utc)


def sun(moment: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(cos zenith, compass azimuth) at the UTC instant `moment`."""
    moment = moment.astimezone(timezone.utc)
    doy = moment.timetuple().tm_yday
    hour = moment.hour + moment.minute / 60 + moment.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqt = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    tst = hour * 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180)
    phi = math.radians(lat)
    cz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    cz = max(-1.0, min(1.0, cz))
    sz = math.sqrt(max(0.0, 1 - cz * cz))
    if sz < 1e-9 or abs(math.cos(phi)) < 1e-9:
        return cz, 180.0
    ca = (math.sin(phi) * cz - math.sin(decl)) / (math.cos(phi) * sz)
    az = math.degrees(math.acos(max(-1.0, min(1.0, ca))))
    az = (az + 180) % 360 if ha > 0 else (540 - az) % 360
    return cz, az


def clear_ghi(cz: float) -> float:
    """Haurwitz clear-sky GHI (W/m2)."""
    return 1098.0 * cz * math.exp(-0.057 / cz) if cz > 0.01 else 0.0


def clear_beam_share(cz: float) -> float:
    """The clear sky's beam share of GHI (1 - Erbs' diffuse fraction at the clear-sky index)."""
    if cz <= 0.01:
        return 0.0
    kt = clear_ghi(cz) / (1367.0 * cz)
    if kt <= 0.22:
        diffuse = 1 - 0.09 * kt
    elif kt <= 0.8:
        diffuse = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    else:
        diffuse = 0.165
    return 1 - diffuse


def cos_incidence(cz: float, saz: float, tilt: float, paz: float) -> float:
    sz = math.sqrt(max(0.0, 1 - cz * cz))
    b, a = math.radians(tilt), math.radians(saz - paz)
    return cz * math.cos(b) + sz * math.sin(b) * math.cos(a)


@dataclass(frozen=True, slots=True)
class HourSun:
    """The sun over one hour: its points `(cos zenith, azimuth)`, their clear-sky GHI, the hour's mean clear-sky GHI,
    its mean elevation (degrees) and its mean azimuth (circular mean of the points with the sun up)."""

    points: tuple[tuple[float, float], ...]
    clear: tuple[float, ...]
    mean_clear: float
    elevation: float
    azimuth: float

    @property
    def daylight(self) -> bool:
        return self.mean_clear > DAYLIGHT_W


def hour_sun(start: datetime, lat: float, lon: float, substeps: int = SUBSTEPS) -> HourSun:
    """The sun over the UTC hour starting at `start`."""
    step = 60 / substeps
    points = tuple(sun(start + timedelta(minutes=step * (k + 0.5)), lat, lon) for k in range(substeps))
    clear = tuple(clear_ghi(cz) for cz, _ in points)
    mean_cz = sum(cz for cz, _ in points) / len(points)
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, mean_cz))))
    up = [az for cz, az in points if cz > 0] or [az for _, az in points]
    x = sum(math.cos(math.radians(az)) for az in up)
    y = sum(math.sin(math.radians(az)) for az in up)
    azimuth = math.degrees(math.atan2(y, x)) % 360 if (x or y) else 180.0
    return HourSun(points, clear, sum(clear) / len(clear), elevation, azimuth)


def poa_points(hs: HourSun, ghi: float | None, beam_share: float | None, tilt: float, paz: float) -> list[float]:
    """Plane-of-array W/m2 at each point of the hour. `ghi` None: clear sky (with the clear sky's beam share)."""
    cb = math.cos(math.radians(tilt))
    if ghi is None:
        kt = 1.0
    else:
        if hs.mean_clear <= 0.5 or ghi <= 0:
            return [0.0] * len(hs.points)
        kt = ghi / hs.mean_clear
    out = []
    for (cz, saz), c in zip(hs.points, hs.clear):
        g = kt * c
        if g <= 0:
            out.append(0.0)
            continue
        share = clear_beam_share(cz) if beam_share is None else beam_share
        b = share * g
        d = g - b
        ci = cos_incidence(cz, saz, tilt, paz)
        beam = b * max(0.0, ci) / max(cz, 0.087)
        out.append(beam + d * (1 + cb) / 2 + ALBEDO * g * (1 - cb) / 2)
    return out


def array_kw(
    hs: HourSun,
    planes: Sequence[Plane],
    ghi: float | None = None,
    beam: float | None = None,
    temp_c: float | None = None,
) -> float:
    """The hour's mean output (kW) of `planes` from the hour's GHI and beam (W/m2) and air temperature; `ghi` None:
    clear sky, no derating."""
    if not hs.daylight:
        return 0.0
    if ghi is None:
        share = None
    else:
        if ghi <= 0:
            return 0.0
        share = max(0.0, min(1.0, (beam or 0.0) / ghi))
    total = 0.0
    poa_weighted = 0.0
    kwp_sum = 0.0
    for tilt, paz, kwp in planes:
        values = poa_points(hs, ghi, share, tilt, paz)
        mean = sum(values) / len(values)
        total += kwp / 1000 * mean
        poa_weighted += mean * kwp
        kwp_sum += kwp
    if ghi is not None:
        air = 10.0 if temp_c is None else temp_c
        cell = air + CELL_K_PER_W * (poa_weighted / max(1e-9, kwp_sum))
        total *= 1 - TEMP_COEFF * (cell - 25)
    return max(0.0, total)
