"""The own solar forecast's candidates and the sun-position factor (SpotNav 3.0, shadow): pure arithmetic over the
kept inputs (`site/solar_shadow_store.py`), ported from the solar forecast experiment (`plans/benchmark/solar_models/
models.py`, round 2). Nothing here plans; the shell (`site/solar_shadow.py`) logs the candidates and reviews them.

**Candidates** (each hour in kWh; the review scores them side by side):

* `own` (OWN): the forecast irradiance transposed onto the fitted planes (`site/solar_planes.py`, a horizontal 1 kWp
  stand-in before the first fit), times a learned ratio per weather run × cloud class (<25, 25-60, 60-90, ≥90 %) ×
  sun elevation band (<12, 12-30, ≥30°) over the last `RATIO_WINDOW` days. The experiment's OWN.
* `own_sunpos`: the same transposed forecast times the **sun-position factor** instead of the ratio (the experiment's
  round-2 variant, 0.650 against 0.693 kW MAE over a year): per weather run × clear/cloudy (60 %) × sun elevation
  (0/5/10/15/20/30/40°) × sun azimuth (30° sectors, edges 60-300°), over the last `POS_WINDOW` days. Its cells
  include the ratio's clear/cloudy split and a finer elevation, so it replaces the ratio rather than stacking on it.
* `ghi`: the forecast GHI times the measured production over the forecast GHI of the last `GHI_WINDOW` days, per
  weather run (no plane fit).
* `ghi_sunpos`: `ghi` times a sun-position factor learned against it (each past day's `ghi` as it would have been).
* `cloud_only`: the planes' clear-sky output times a learned ratio per weather run × 10 % cloud bin × elevation band
  over `RATIO_WINDOW` days. The fallback when no irradiance could be fetched: then fed the weather entity's cloud
  cover.
* `provider_sunpos`: the provider's forecast (Solcast, Forecast.Solar, Open-Meteo Solar Forecast: what the Hybrid
  strategy reads) times a sun-position factor learned against it; cloud class from the fetched cloud cover (unknown
  without it).

**Learning** (`learn`): from the completed local days up to `upto`, every hour with measured production, per weather
run as kept, except the hours of a dead day (measured about zero under a forecast of real sun: snow on the panels or
a broken statistic, `solar_shadow_store.dead_days`), which would teach the factors that the sun makes nothing.
The history itself never holds an implausible hour (see `solar_shadow_store`), so `cap_kwh` is a real one. Each cell's ratio is shrunk toward its learner's window-wide ratio with `SHRINK_KWH` kWh of
pseudo-forecast; hours whose reference is under `MIN_REF_KWH` are skipped. **The weather run** of a forecast hour is
1 for leads up to `LEAD_RUN1_MAX_H` hours, else 2 (the experiment's rule). A learned forecast is capped at the
larger of its unscaled reference and the highest hour ever measured.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final

from .. import time_zones
from .open_meteo import Weather
from .solar_shadow_store import RUNS, SolarHistory, dead_days
from .sun_geometry import HourSun, Plane, array_kw, hour_key, hour_sun, key_start

CANDIDATE_OWN: Final = "own"
CANDIDATE_OWN_SUNPOS: Final = "own_sunpos"
CANDIDATE_GHI: Final = "ghi"
CANDIDATE_GHI_SUNPOS: Final = "ghi_sunpos"
CANDIDATE_CLOUD_ONLY: Final = "cloud_only"
CANDIDATE_PROVIDER: Final = "provider"
CANDIDATE_PROVIDER_CORRECTED: Final = "provider_corrected"
CANDIDATE_PROVIDER_SUNPOS: Final = "provider_sunpos"
OWN_CANDIDATES: Final = (
    CANDIDATE_OWN,
    CANDIDATE_OWN_SUNPOS,
    CANDIDATE_GHI,
    CANDIDATE_GHI_SUNPOS,
    CANDIDATE_CLOUD_ONLY,
)
PROVIDER_CANDIDATES: Final = (CANDIDATE_PROVIDER, CANDIDATE_PROVIDER_CORRECTED, CANDIDATE_PROVIDER_SUNPOS)
CANDIDATES: Final = (*PROVIDER_CANDIDATES, *OWN_CANDIDATES)

CLOUD_EDGES: Final = (25.0, 60.0, 90.0)
ELEV_EDGES: Final = (12.0, 30.0)
CLOUD_BINS: Final = tuple(float(v) for v in range(10, 100, 10))
ELEV_FINE: Final = (5.0, 10.0, 15.0, 20.0, 30.0, 40.0)
AZ_EDGES: Final = tuple(float(v) for v in range(60, 330, 30))
CLOUDY_PCT: Final = 60.0
#: Cloud cover assumed where a fetched hour has none (the experiment's).
CLOUD_UNKNOWN_PCT: Final = 50.0
RATIO_WINDOW: Final = 60
POS_WINDOW: Final = 365
GHI_WINDOW: Final = 30
SHRINK_KWH: Final = 3.0
MIN_REF_KWH: Final = 0.05
LEAD_RUN1_MAX_H: Final = 20.0
#: Before the first plane fit: a horizontal 1 kWp stand-in (the ratios learn the scale).
STAND_IN_PLANES: Final = ((0.0, 180.0, 1.0),)


def cls(edges: Iterable[float], value: float) -> int:
    return sum(value >= edge for edge in edges)


def run_for(start: datetime, issued_at: datetime) -> int:
    return 1 if (start - issued_at).total_seconds() / 3600 <= LEAD_RUN1_MAX_H else 2


def pos_cell(run: int, cloud: float | None, hs: HourSun) -> tuple[Any, ...]:
    """The sun-position factor's cell (see the module); `None` for the cloud class when the cover is unknown."""
    return (
        run,
        None if cloud is None else cloud >= CLOUDY_PCT,
        cls(ELEV_FINE, hs.elevation),
        cls(AZ_EDGES, hs.azimuth),
    )


def ratio_cell(run: int, cloud: float | None, hs: HourSun) -> tuple[Any, ...]:
    return (run, cls(CLOUD_EDGES, CLOUD_UNKNOWN_PCT if cloud is None else cloud), cls(ELEV_EDGES, hs.elevation))


def cloud_cell(run: int, cloud: float | None, hs: HourSun) -> tuple[Any, ...]:
    return (run, cls(CLOUD_BINS, CLOUD_UNKNOWN_PCT if cloud is None else cloud), cls(ELEV_EDGES, hs.elevation))


@dataclass(slots=True)
class Learner:
    """Binned ratio of the outcome to a reference over the last `window` days (see the module)."""

    window: int
    rows: list[tuple[date, tuple[Any, ...], float, float]] = field(default_factory=list)

    def add(self, day: date, cell: tuple[Any, ...], ref: float, actual: float) -> None:
        if ref >= MIN_REF_KWH:
            self.rows.append((day, cell, ref, actual))

    def factors(self, upto: date) -> CellFactors:
        lo = upto - timedelta(days=self.window)
        ref_sum = actual_sum = 0.0
        cells: dict[tuple[Any, ...], list[float]] = {}
        for day, cell, ref, actual in self.rows:
            if lo < day <= upto:
                ref_sum += ref
                actual_sum += actual
                sums = cells.setdefault(cell, [0.0, 0.0])
                sums[0] += ref
                sums[1] += actual
        overall = actual_sum / ref_sum if ref_sum > 1.0 else 1.0
        return CellFactors(
            overall=overall,
            cells={cell: (a + SHRINK_KWH * overall) / (r + SHRINK_KWH) for cell, (r, a) in cells.items()},
            ref_kwh={cell: r for cell, (r, _a) in cells.items()},
        )


@dataclass(frozen=True, slots=True)
class CellFactors:
    """A learner's window-wide ratio (`overall`) and each cell's shrunk ratio, with the reference kWh behind it."""

    overall: float = 1.0
    cells: Mapping[tuple[Any, ...], float] = field(default_factory=dict)
    ref_kwh: Mapping[tuple[Any, ...], float] = field(default_factory=dict)

    def get(self, cell: tuple[Any, ...]) -> float:
        return self.cells.get(cell, self.overall)

    def as_dict(self, strongest: int = 0) -> dict[str, Any]:
        out: dict[str, Any] = {"overall": round(self.overall, 3), "cells": len(self.cells)}
        if strongest:
            # The cells that move the forecast most (with at least a few kWh behind them): where the sun-position
            # factor sees shading.
            backed = [(f, cell) for cell, f in self.cells.items() if self.ref_kwh.get(cell, 0.0) >= 3 * SHRINK_KWH]
            backed.sort(key=lambda item: abs(item[0] - self.overall), reverse=True)
            out["strongest"] = [
                {"cell": _cell_label(cell), "factor": round(f, 3), "ref_kwh": round(self.ref_kwh[cell], 1)}
                for f, cell in backed[:strongest]
            ]
        return out


def _cell_label(cell: tuple[Any, ...]) -> str:
    run, cloudy, elev, az = cell
    elev_edges = (0.0, *ELEV_FINE, 90.0)
    az_edges = (0.0, *AZ_EDGES, 360.0)
    sky = "unknown" if cloudy is None else ("cloudy" if cloudy else "clear")
    return (
        f"run{run} {sky} elev {elev_edges[elev]:.0f}-{elev_edges[elev + 1]:.0f} "
        f"az {az_edges[az]:.0f}-{az_edges[az + 1]:.0f}"
    )


@dataclass(frozen=True, slots=True)
class Factors:
    """Everything learned up to `upto` (see the module)."""

    upto: date | None = None
    ratio: CellFactors = field(default_factory=CellFactors)
    cloud: CellFactors = field(default_factory=CellFactors)
    pos_own: CellFactors = field(default_factory=CellFactors)
    pos_ghi: CellFactors = field(default_factory=CellFactors)
    pos_provider: CellFactors = field(default_factory=CellFactors)
    #: Per weather run: measured kWh per forecast W/m2 over the last `GHI_WINDOW` days (`None` without any).
    kpg: Mapping[int, float | None] = field(default_factory=dict)
    cap_kwh: float | None = None
    hours: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "upto": None if self.upto is None else self.upto.isoformat(),
            "hours": self.hours,
            "cap_kwh": None if self.cap_kwh is None else round(self.cap_kwh, 2),
            "ghi_kwh_per_kw_m2": {str(r): None if k is None else round(k * 1000, 3) for r, k in self.kpg.items()},
            "ratio": self.ratio.as_dict(),
            "cloud_only": self.cloud.as_dict(),
            "sun_position_own": self.pos_own.as_dict(strongest=8),
            "sun_position_ghi": self.pos_ghi.as_dict(),
            "sun_position_provider": self.pos_provider.as_dict(strongest=8),
        }


def _capped(value: float, reference: float, cap: float | None) -> float:
    return max(0.0, value if cap is None else min(value, max(reference, cap)))


def learn(
    history: SolarHistory,
    planes: Iterable[Plane] | None,
    position: tuple[float, float],
    tz: str,
    upto: date,
) -> Factors:
    """Learn every factor from the kept inputs up to the local day `upto` (CPU: run it in the executor)."""
    planes = tuple(planes or STAND_IN_PLANES)
    lat, lon = position
    zone = time_zones.require_zone(tz)
    first_day = upto - timedelta(days=POS_WINDOW)
    first = hour_key(datetime.combine(first_day + timedelta(days=1), datetime.min.time(), zone))
    end = hour_key(datetime.combine(upto + timedelta(days=1), datetime.min.time(), zone))
    ratio, cloud = Learner(RATIO_WINDOW), Learner(RATIO_WINDOW)
    pos_own, pos_ghi, pos_provider = Learner(POS_WINDOW), Learner(POS_WINDOW), Learner(POS_WINDOW)
    daily: dict[tuple[int, date], list[float]] = {}  # (run, day) -> [pv, ghi] for the GHI factor
    ghi_rows: list[tuple[date, int, tuple[Any, ...], float, float]] = []
    hours = 0
    dead = dead_days(history, zone)
    for key in range(first, end):
        pv = history.pv_kwh(key)
        if pv is None:
            continue
        start = key_start(key)
        hs = hour_sun(start, lat, lon)
        if not hs.daylight:
            continue
        day = start.astimezone(zone).date()
        if day in dead:
            continue
        clear = array_kw(hs, planes)
        hours += 1
        for run in RUNS:
            weather = history.weather_at(run, key)
            if weather is not None:
                raw = array_kw(hs, planes, weather.ghi, weather.beam, weather.temp)
                cloud_pct = weather.cloud
                ratio.add(day, ratio_cell(run, cloud_pct, hs), raw, pv)
                cloud.add(day, cloud_cell(run, cloud_pct, hs), clear, pv)
                pos_own.add(day, pos_cell(run, CLOUD_UNKNOWN_PCT if cloud_pct is None else cloud_pct, hs), raw, pv)
                sums = daily.setdefault((run, day), [0.0, 0.0])
                sums[0] += pv
                sums[1] += weather.ghi
                ghi_rows.append(
                    (day, run, pos_cell(run, CLOUD_UNKNOWN_PCT if cloud_pct is None else cloud_pct, hs), weather.ghi, pv)
                )
            provider = history.provider[run].get(key)
            if provider is not None:
                pos_provider.add(
                    day, pos_cell(run, None if weather is None else weather.cloud, hs), provider / 100, pv
                )

    def kpg_before(run: int, day: date) -> float | None:
        """The GHI factor as known the day after `day - 1`: over the `GHI_WINDOW` days up to `day - 1`."""
        pv_sum = ghi_sum = 0.0
        for back in range(1, GHI_WINDOW + 1):
            sums = daily.get((run, day - timedelta(days=back)))
            if sums is not None:
                pv_sum += sums[0]
                ghi_sum += sums[1]
        return pv_sum / ghi_sum if ghi_sum > 0 else None

    known: dict[tuple[int, date], float | None] = {}
    for day, run, cell, ghi, pv in ghi_rows:
        if (run, day) not in known:
            known[(run, day)] = kpg_before(run, day)
        k = known[(run, day)]
        if k is not None:
            pos_ghi.add(day, cell, k * ghi, pv)
    observed = [value for key, value in history.pv.items() if key_start(key).astimezone(zone).date() not in dead]
    return Factors(
        upto=upto,
        ratio=ratio.factors(upto),
        cloud=cloud.factors(upto),
        pos_own=pos_own.factors(upto),
        pos_ghi=pos_ghi.factors(upto),
        pos_provider=pos_provider.factors(upto),
        kpg={run: kpg_before(run, upto + timedelta(days=1)) for run in RUNS},
        cap_kwh=max(observed) / 100 if observed else None,
        hours=hours,
    )


def daylight_hours(hours: Iterable[datetime], position: tuple[float, float]) -> list[datetime]:
    lat, lon = position
    return [start for start in hours if hour_sun(start, lat, lon).daylight]


def own_forecast(
    factors: Factors,
    planes: Iterable[Plane] | None,
    position: tuple[float, float],
    issued_at: datetime,
    hours: Iterable[datetime],
    weather: Mapping[datetime, Weather],
    fallback_cloud: Mapping[datetime, float] | None = None,
) -> tuple[dict[str, dict[datetime, float]], dict[str, int]]:
    """The own candidates for the daylight hours among `hours` (UTC starts), as issued at `issued_at` from the fetched
    `weather`; an hour without it gets only `cloud_only`, from `fallback_cloud` (the weather entity's cover) when that
    has it. With how many hours came from each input (`open_meteo`, `weather_entity`)."""
    planes = tuple(planes or STAND_IN_PLANES)
    lat, lon = position
    cap = factors.cap_kwh
    out: dict[str, dict[datetime, float]] = {name: {} for name in OWN_CANDIDATES}
    used = {"open_meteo": 0, "weather_entity": 0}
    for start in hours:
        hs = hour_sun(start, lat, lon)
        if not hs.daylight:
            continue
        run = run_for(start, issued_at)
        clear = array_kw(hs, planes)
        w = weather.get(start)
        if w is None:
            cover = None if fallback_cloud is None else fallback_cloud.get(start)
            if cover is None:
                continue
            used["weather_entity"] += 1
            out[CANDIDATE_CLOUD_ONLY][start] = _capped(clear * factors.cloud.get(cloud_cell(run, cover, hs)), clear, cap)
            continue
        used["open_meteo"] += 1
        cloud_pct = CLOUD_UNKNOWN_PCT if w.cloud is None else w.cloud
        raw = array_kw(hs, planes, w.ghi, w.beam, w.temp)
        out[CANDIDATE_OWN][start] = _capped(raw * factors.ratio.get(ratio_cell(run, w.cloud, hs)), raw, cap)
        out[CANDIDATE_OWN_SUNPOS][start] = _capped(raw * factors.pos_own.get(pos_cell(run, cloud_pct, hs)), raw, cap)
        out[CANDIDATE_CLOUD_ONLY][start] = _capped(clear * factors.cloud.get(cloud_cell(run, w.cloud, hs)), clear, cap)
        k = factors.kpg.get(run)
        if k is not None:
            ghi = k * w.ghi
            out[CANDIDATE_GHI][start] = max(0.0, ghi)
            out[CANDIDATE_GHI_SUNPOS][start] = _capped(ghi * factors.pos_ghi.get(pos_cell(run, cloud_pct, hs)), ghi, cap)
    return {name: hours_ for name, hours_ in out.items() if hours_}, used


def provider_sunpos(
    factors: Factors,
    position: tuple[float, float],
    issued_at: datetime,
    provider: Mapping[datetime, float],
    clouds: Mapping[datetime, float | None],
) -> dict[datetime, float]:
    """The provider's forecast times the sun-position factor learned against it (daylight hours only)."""
    lat, lon = position
    out: dict[datetime, float] = {}
    for start, kwh in provider.items():
        hs = hour_sun(start, lat, lon)
        if not hs.daylight:
            continue
        cell = pos_cell(run_for(start, issued_at), clouds.get(start), hs)
        out[start] = _capped(kwh * factors.pos_provider.get(cell), kwh, factors.cap_kwh)
    return out
