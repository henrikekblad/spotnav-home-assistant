"""The array's planes fitted from its own history (SpotNav 3.0, shadow): the one or two tilted planes whose clear-sky
output best fits the measured clear-sky envelope.

Pure Python, ported from the solar forecast experiment (`plans/benchmark/solar_models/solar.py`, `envelope` and
`fit_planes`). The fit is a grid search (tilts `TILTS`, compass azimuths every `AZ_STEP`°) with least squares on the
kWp (never negative); two planes from `TWO_PLANE_MIN_DAYS` days of history on, one before (two planes over a few
winter weeks extrapolate badly to spring). It is not an orientation check: on a low-tilt array the azimuth is barely
identified (the experiment's round 2), it only shapes the own forecast. A full year's fit takes a few seconds, so it
runs in the executor (`site/solar_shadow.py`), once a month.

* **Envelope**: per daylight hour of the window (mean clear-sky GHI at least `ENVELOPE_MIN_CLEAR_W`), the second
  highest measured output of the same UTC hour within ±7 days inside the window, where at least 8 such days exist.
* Fewer than `MIN_ROWS` envelope hours: no fit (`None`).
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from operator import mul
from typing import Final

from .sun_geometry import ALBEDO, Plane, clear_beam_share, clear_ghi, hour_sun, key_start

TILTS: Final = (10, 20, 30, 40, 50, 60)
AZ_STEP: Final = 15
AZIMUTHS: Final = tuple(range(0, 360, AZ_STEP))
TWO_PLANE_MIN_DAYS: Final = 120
ENVELOPE_MIN_CLEAR_W: Final = 30.0
#: Envelope hours needed for a fit (a week of eight daylight hours).
MIN_ROWS: Final = 7 * 8
#: Points per hour in the fit (quarter-hour mid-points: four, enough for a clear-sky envelope and a third of the cost).
FIT_SUBSTEPS: Final = 4


def envelope(pv: Mapping[int, float], first: int, end: int, lat: float, lon: float) -> list[tuple[int, float]]:
    """`(hour key, envelope kW)` for the daylight hours in `[first, end)` (hour keys: hours since the epoch, UTC)."""
    rows: list[tuple[int, float]] = []
    for key in range(first, end):
        values = [pv[other] for other in range(key - 7 * 24, key + 8 * 24, 24) if first <= other < end and other in pv]
        if len(values) < 8:
            continue
        hs = hour_sun(key_start(key), lat, lon, FIT_SUBSTEPS)
        if hs.mean_clear < ENVELOPE_MIN_CLEAR_W:
            continue
        values.sort()
        rows.append((key, values[-2]))
    return rows


def _features(keys: list[int], lat: float, lon: float) -> list[list[tuple[float, float, float, float, float, float]]]:
    """Per hour, per point: (cos z, sin z cos az, sin z sin az, beam factor, diffuse GHI, GHI) of the clear sky."""
    out = []
    for key in keys:
        hs = hour_sun(key_start(key), lat, lon, FIT_SUBSTEPS)
        points = []
        for (cz, saz), _ in zip(hs.points, hs.clear):
            g = clear_ghi(cz)
            if g <= 0:
                continue
            share = clear_beam_share(cz)
            sz = math.sqrt(max(0.0, 1 - cz * cz))
            a = math.radians(saz)
            points.append((cz, sz * math.cos(a), sz * math.sin(a), share * g / max(cz, 0.087), (1 - share) * g, g))
        out.append(points)
    return out


def _plane_series(features: list[list[tuple[float, ...]]], tilt: float, paz: float) -> list[float]:
    """The hour-mean clear-sky plane-of-array irradiance (W/m2) of one plane at each envelope hour."""
    b = math.radians(tilt)
    cb, sb = math.cos(b), math.sin(b)
    cp, sp = math.cos(math.radians(paz)), math.sin(math.radians(paz))
    sky, ground = (1 + cb) / 2, ALBEDO * (1 - cb) / 2
    out = []
    for points in features:
        total = 0.0
        for cz, szc, szs, beam, diffuse, g in points:
            ci = cz * cb + sb * (szc * cp + szs * sp)
            total += (beam * ci if ci > 0 else 0.0) + diffuse * sky + g * ground
        out.append(total / FIT_SUBSTEPS)
    return out


def _dot(a: list[float], b: list[float]) -> float:
    return sum(map(mul, a, b, strict=True))


def fit_planes(rows: list[tuple[int, float]], lat: float, lon: float, two: bool) -> tuple[Plane, ...] | None:
    """The one or two planes `(tilt, azimuth, kWp)` whose clear-sky output best fits `rows` (see the module)."""
    if len(rows) < MIN_ROWS:
        return None
    keys = [key for key, _ in rows]
    y = [kw for _, kw in rows]
    features = _features(keys, lat, lon)
    grid = [(tilt, az) for tilt in TILTS for az in AZIMUTHS]
    series = [_plane_series(features, tilt, az) for tilt, az in grid]
    xy = [_dot(x, y) for x in series]
    xx = [_dot(x, x) for x in series]
    yy = _dot(y, y)
    count = len(y)
    best: tuple[float, tuple[Plane, ...]] | None = None
    for a, (tilt, az) in enumerate(grid):
        k = xy[a] / max(xx[a], 1e-9)
        if k < 0:
            continue
        residual = (yy - 2 * k * xy[a] + k * k * xx[a]) / count
        if best is None or residual < best[0]:
            best = (residual, ((float(tilt), float(az), k * 1000),))
    if two:
        for a in range(len(grid)):
            xa = series[a]
            for b in range(a + 1, len(grid)):
                xb = series[b]
                ab = _dot(xa, xb)
                aa, bb = xx[a], xx[b]
                det = aa * bb - ab * ab
                if det <= 1e-9:
                    continue
                ya, yb = xy[a], xy[b]
                wa, wb = (bb * ya - ab * yb) / det, (aa * yb - ab * ya) / det
                if wa < 0 or wb < 0:
                    continue
                residual = (yy - 2 * (wa * ya + wb * yb) + wa * wa * aa + 2 * wa * wb * ab + wb * wb * bb) / count
                if best is None or residual < best[0]:
                    best = (
                        residual,
                        (
                            (float(grid[a][0]), float(grid[a][1]), wa * 1000),
                            (float(grid[b][0]), float(grid[b][1]), wb * 1000),
                        ),
                    )
    return None if best is None else best[1]
