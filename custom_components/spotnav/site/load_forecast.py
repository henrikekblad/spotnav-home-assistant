"""The house load forecast (SpotNav 3.0, step 1): what the house will draw, hour by hour, for the next 48 hours.

Pure: the site's hour rows, the clock, and optionally an outdoor temperature forecast and the holiday dates go in;
`{hour_start: kW}` comes out. No Home Assistant state (the shell is `site/house_forecast.py`). It decides nothing a
charger is told: fuse protection and the import target's hold stay on measurements; a forecast only changes what a
plan expects (a later release).

* **History**: the measured hours' `house_kw` (the site's import less the chargers', `site/demand_tracker.py`) of the
  last `WINDOW_DAYS`, never an hour read from Home Assistant's statistics (`imported`) and never an hour without the
  house's share (skipped, not counted as 0). The tracker keeps the running month and the month before, so the window
  holds between four and six weeks.
* **Weight**: an hour `age` days old weighs `0.5 ** (age / HALF_LIFE_DAYS)`, so a changed habit shows within ~2 weeks.
* **Profile**: per day type (weekday, weekend) x local hour, the weighted upper quantile `QUANTILE` (p75, owner
  2026-11-04): counting on too little room costs a late car, counting on too much a slightly dearer plan. A public
  holiday is a weekend day when a holiday calendar gives the dates; without one, weekday/weekend only. The key is the
  local hour, never the UTC offset, so DST days have 23 or 25 hours and the repeated hour counts under its local hour.
* **Fallback ladder** per hour: the cell (`MIN_CELL_SAMPLES` or more) -> the same local hour over every day type ->
  the flat figure the site has today (`SiteCapacityController._typical_house_kw`) -> `None` (a plan then uses the
  car's own power, as today).
* **Temperature**: one site-wide coefficient `b` (kW per °C below `BALANCE_C`), fitted by weighted least squares on
  the residuals from each cell's weighted mean against how much colder than its cell's mean the hour was (the
  backtest's method, `plans/load_forecast_backtest/backtest.py`). Applied as `p75 + b x (T_cell - T_forecast)`, both
  capped at `BALANCE_C`, never below 0. Off by itself when it explains too little (`b <= 0` or `R² <= MIN_R2`, an
  electrically unheated house), when there are too few hours with a temperature, or for an hour without a forecast
  temperature.

The owner's backtest (16 months): p75 + temperature cuts the night's large under-forecasts (> 0.5 kW) from 10.1 % of
hours (today's flat 24-h mean) to 3.7 %.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any, Final

from .. import time_zones
from ..planning.demand_charge import hour_start, HourMean

#: How far back the profile reads (the tracker keeps at least the month before, so four weeks at the least).
WINDOW_DAYS: Final = 42
#: An hour this many days old weighs half as much as one just completed.
HALF_LIFE_DAYS: Final = 14.0
#: The upper quantile the profile takes.
QUANTILE: Final = 0.75
#: Fewer samples than this in a cell: the next rung of the ladder.
MIN_CELL_SAMPLES: Final = 3
#: The hours forecast, from the running hour's start.
HORIZON_H: Final = 48
#: Temperatures above this (°C) count as this: a house above it heats nothing.
BALANCE_C: Final = 15.0
#: The temperature term is used only when it explains more than this share of the residuals' variance.
MIN_R2: Final = 0.05
#: ...and only with at least this many hours that carry a temperature (a week).
MIN_TEMPERATURE_SAMPLES: Final = 7 * 24

SOURCE_PROFILE: Final = "profile"
SOURCE_HOUR: Final = "hour"
SOURCE_FLAT: Final = "flat"
SOURCE_NONE: Final = "none"

DAY_WEEKDAY: Final = "weekday"
DAY_WEEKEND: Final = "weekend"

TEMPERATURE_FITTED: Final = "fitted"
TEMPERATURE_TOO_FEW: Final = "too_few_hours"
TEMPERATURE_EXPLAINS_TOO_LITTLE: Final = "explains_too_little"


@dataclass(frozen=True, slots=True)
class Sample:
    """One measured hour of the window: its house load (kW), weight, day type, local hour and temperature (°C)."""

    kw: float
    weight: float
    day: str
    hour: int
    outdoor_c: float | None


@dataclass(frozen=True, slots=True)
class TemperatureFit:
    """The temperature term: the fitted coefficient (kW per °C colder), how much of the residuals it explains, over
    how many hours, and whether it is used (`reason` says why not)."""

    b_kw_per_c: float
    r2: float
    samples: int
    active: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "b_kw_per_c": round(self.b_kw_per_c, 4),
            "r2": round(self.r2, 4),
            "samples": self.samples,
            "active": self.active,
            "reason": self.reason,
        }


NO_TEMPERATURE: Final = TemperatureFit(0.0, 0.0, 0, False, TEMPERATURE_TOO_FEW)


@dataclass(frozen=True, slots=True)
class ForecastHour:
    """One forecast hour: its start (UTC), the house's expected load (kW, `None` when nothing is known), the rung it
    came from, the forecast temperature it used (°C) and the temperature term's part of `kw`."""

    start: datetime
    kw: float | None
    source: str
    outdoor_c: float | None = None
    temperature_kw: float = 0.0

    def as_row(self) -> list[Any]:
        """`[start, kW, source, forecast °C, temperature kW]`, as the diagnostics show it."""
        return [
            self.start.isoformat(),
            None if self.kw is None else round(self.kw, 3),
            self.source,
            None if self.outdoor_c is None else round(self.outdoor_c, 1),
            round(self.temperature_kw, 3),
        ]


@dataclass(frozen=True, slots=True)
class LoadForecast:
    """The forecast made at `made_at`: `HORIZON_H` hours from the running hour's start."""

    made_at: datetime
    hours: tuple[ForecastHour, ...]
    samples: int
    temperature: TemperatureFit
    holidays: bool

    def by_hour(self) -> dict[datetime, float]:
        """`{hour_start: kW}`, the hours with a figure only."""
        return {hour.start: hour.kw for hour in self.hours if hour.kw is not None}

    def kw_at(self, moment: datetime) -> float | None:
        """The figure for the hour `moment` falls in, `None` outside the horizon or when nothing is known."""
        for hour in self.hours:
            if hour.start <= moment < hour.start + timedelta(hours=1):
                return hour.kw
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "samples": self.samples,
            "quantile": QUANTILE,
            "half_life_days": HALF_LIFE_DAYS,
            "day_types": "weekday_weekend_holiday" if self.holidays else "weekday_weekend",
            "temperature": self.temperature.as_dict(),
            "hours": [hour.as_row() for hour in self.hours],
        }


def day_type(local_day: date, holidays: Collection[date] | None) -> str:
    """Weekend for Saturday, Sunday and (when a holiday source is there) a public holiday; else weekday."""
    if local_day.weekday() >= 5 or (holidays is not None and local_day in holidays):
        return DAY_WEEKEND
    return DAY_WEEKDAY


def weighted_quantile(values: list[float], weights: list[float], q: float) -> float:
    """The smallest value whose cumulative weight share reaches `q` (the backtest's `wquant`)."""
    pairs = sorted(zip(values, weights, strict=True))
    total = sum(weight for _value, weight in pairs)
    if total <= 0:
        return pairs[len(pairs) // 2][0]
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running / total >= q - 1e-12:
            return value
    return pairs[-1][0]


def samples_from(
    hours: Iterable[HourMean], now: datetime, tz: str, holidays: Collection[date] | None = None
) -> list[Sample]:
    """The window's usable hours (see the module), weighted by age."""
    zone = time_zones.require_zone(tz)
    now = now.astimezone(timezone.utc)
    current = hour_start(now, tz)
    oldest = now - timedelta(days=WINDOW_DAYS)
    samples: list[Sample] = []
    seen: set[datetime] = set()
    for hour in hours:
        if hour.imported or hour.house_kw is None or not math.isfinite(hour.house_kw) or hour.house_kw < 0:
            continue
        if hour.start < oldest or hour.start >= current or hour.start in seen:
            continue
        seen.add(hour.start)
        local = hour.start.astimezone(zone)
        age_days = (now - hour.start).total_seconds() / 86400.0
        samples.append(
            Sample(
                kw=float(hour.house_kw),
                weight=0.5 ** (age_days / HALF_LIFE_DAYS),
                day=day_type(local.date(), holidays),
                hour=local.hour,
                outdoor_c=hour.outdoor_c,
            )
        )
    return samples


def _capped(value: float) -> float:
    return min(value, BALANCE_C)


def fit_temperature(samples: list[Sample]) -> TemperatureFit:
    """The site-wide temperature coefficient (see the module), and whether it is used."""
    cells: dict[tuple[str, int], list[Sample]] = {}
    for sample in samples:
        cells.setdefault((sample.day, sample.hour), []).append(sample)
    xs: list[float] = []
    rs: list[float] = []
    ws: list[float] = []
    for cell in cells.values():
        weight = sum(sample.weight for sample in cell)
        if weight <= 0:
            continue
        mean_kw = sum(sample.kw * sample.weight for sample in cell) / weight
        temperatures = [_capped(sample.outdoor_c) for sample in cell if sample.outdoor_c is not None]
        if not temperatures:
            continue
        mean_c = sum(temperatures) / len(temperatures)
        for sample in cell:
            if sample.outdoor_c is None:
                continue
            xs.append(mean_c - _capped(sample.outdoor_c))
            rs.append(sample.kw - mean_kw)
            ws.append(sample.weight)
    if len(xs) < MIN_TEMPERATURE_SAMPLES:
        return TemperatureFit(0.0, 0.0, len(xs), False, TEMPERATURE_TOO_FEW)
    sxx = sum(w * x * x for x, w in zip(xs, ws, strict=True))
    srr = sum(w * r * r for r, w in zip(rs, ws, strict=True))
    if sxx <= 1e-9 or srr <= 1e-9:
        return TemperatureFit(0.0, 0.0, len(xs), False, TEMPERATURE_EXPLAINS_TOO_LITTLE)
    b = sum(w * x * r for x, r, w in zip(xs, rs, ws, strict=True)) / sxx
    sse = sum(w * (r - b * x) ** 2 for x, r, w in zip(xs, rs, ws, strict=True))
    r2 = 1.0 - sse / srr
    active = b > 0 and r2 > MIN_R2
    return TemperatureFit(b, r2, len(xs), active, TEMPERATURE_FITTED if active else TEMPERATURE_EXPLAINS_TOO_LITTLE)


def forecast_house_load(
    hours: Iterable[HourMean],
    now: datetime,
    tz: str,
    *,
    flat_kw: float | None = None,
    temperatures: Mapping[datetime, float] | None = None,
    holidays: Collection[date] | None = None,
    horizon_h: int = HORIZON_H,
) -> LoadForecast:
    """The house's expected load for `horizon_h` hours from the running hour's start (see the module).

    `temperatures` maps an hour's start (UTC) to its forecast outdoor temperature (°C); `holidays` are the local dates
    of public holidays (`None`: no source, weekday/weekend only); `flat_kw` is the site's flat figure for the ladder's
    third rung."""
    now = now.astimezone(timezone.utc)
    zone = time_zones.require_zone(tz)
    samples = samples_from(hours, now, tz, holidays)
    fit = fit_temperature(samples) if samples else NO_TEMPERATURE
    by_cell: dict[tuple[str, int], list[Sample]] = {}
    by_hour: dict[int, list[Sample]] = {}
    for sample in samples:
        by_cell.setdefault((sample.day, sample.hour), []).append(sample)
        by_hour.setdefault(sample.hour, []).append(sample)
    flat = flat_kw if flat_kw is not None and math.isfinite(flat_kw) and flat_kw >= 0 else None
    first = hour_start(now, tz)
    forecast: list[ForecastHour] = []
    for index in range(horizon_h):
        start = first + timedelta(hours=index)
        local = start.astimezone(zone)
        cell = by_cell.get((day_type(local.date(), holidays), local.hour), [])
        source = SOURCE_PROFILE
        if len(cell) < MIN_CELL_SAMPLES:
            cell, source = by_hour.get(local.hour, []), SOURCE_HOUR
        if len(cell) < MIN_CELL_SAMPLES:
            if flat is None:
                forecast.append(ForecastHour(start, None, SOURCE_NONE))
            else:
                forecast.append(ForecastHour(start, flat, SOURCE_FLAT))
            continue
        kw = weighted_quantile([sample.kw for sample in cell], [sample.weight for sample in cell], QUANTILE)
        outdoor = None if temperatures is None else temperatures.get(start)
        adjust = 0.0
        if fit.active and outdoor is not None and math.isfinite(outdoor):
            known = [_capped(sample.outdoor_c) for sample in cell if sample.outdoor_c is not None]
            if known:
                adjust = fit.b_kw_per_c * (sum(known) / len(known) - _capped(outdoor))
        value = max(0.0, kw + adjust)
        forecast.append(ForecastHour(start, value, source, outdoor, value - kw))
    return LoadForecast(
        made_at=now, hours=tuple(forecast), samples=len(samples), temperature=fit, holidays=holidays is not None
    )
