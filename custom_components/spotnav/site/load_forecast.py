"""The house load forecast (SpotNav 3.0, step 1): what the house will draw, hour by hour, for the next 48 hours.

Pure: the site's hour rows, the clock, and optionally an outdoor temperature forecast and the holiday dates go in;
`{hour_start: kW}` comes out. No Home Assistant state (the shell is `site/house_forecast.py`). It decides nothing a
charger is told: fuse protection and the import target's hold stay on measurements; a forecast only changes what a
plan expects (`SiteCapacityController.import_target_plan_kw_by_hour`, the target's departure shortfall and its
release reach).

* **History**: the measured hours' `house_kw` (the site's import less the chargers', `site/demand_tracker.py`) of the
  last `WINDOW_DAYS`, never an hour read from Home Assistant's statistics (`imported`) and never an hour without the
  house's share (skipped, not counted as 0). The shell reads them from the forecast's own eight-week history
  (`site/house_history.py`), so the window holds six weeks also early in a month.
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

* **In-day adjustment** (Predbat's, `plans/research_integrations/predbat_lowhanging_2026-11-13.md` item 3): from
  `INDAY_MIN_HOURS` completed hours of today on, the ratio of today's measured load to the raw model's median for those
  hours (the model as known at today's midnight, never the adjusted figure) scales the rest of today, damped
  (`INDAY_DAMPING`) and clamped (`INDAY_MIN`-`INDAY_MAX`). Tomorrow carries today's factor, falling linearly to none by
  tomorrow's end; before today has enough hours, yesterday's factor is carried the same way through today. It catches
  a sick day, guests or a cold snap the temperature term misses. The flat rung is never scaled, and it never lifts an
  hour above the highest hour measured in the window (the owner's import less the chargers, a battery grid-charging
  at the fuse's 13.7 kW, was scaled to 17.4 kW, more than the fuse lets through). The forecast log scores the raw and
  the adjusted figures apart, so its benefit is measured.

The owner's backtest (16 months): p75 + temperature cuts the night's large under-forecasts (> 0.5 kW) from 10.1 % of
hours (today's flat 24-h mean) to 3.7 %.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Any, Final

from .. import time_zones
from ..planning.demand_charge import hour_start, HourMean

#: How far back the profile reads (`site/house_history.py` keeps eight weeks).
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

#: The in-day adjustment: today's measured load against the raw model's median (p50) for today's completed hours,
#: damped towards 1 by this share, clamped to [INDAY_MIN, INDAY_MAX], only from `INDAY_MIN_HOURS` such hours on.
INDAY_QUANTILE: Final = 0.5
INDAY_DAMPING: Final = 0.95
INDAY_MIN: Final = 0.5
INDAY_MAX: Final = 2.0
INDAY_MIN_HOURS: Final = 3
#: Below this expected energy (kWh) so far the ratio says nothing (a house that draws next to nothing).
INDAY_MIN_EXPECTED_KWH: Final = 0.3

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
    #: The figure before the in-day adjustment (`None` on the flat rung or without a figure), and its factor.
    raw_kw: float | None = None
    inday_factor: float = 1.0

    def as_row(self) -> list[Any]:
        """`[start, kW, source, forecast °C, temperature kW, raw kW, in-day factor]`, as the diagnostics show it."""
        return [
            self.start.isoformat(),
            None if self.kw is None else round(self.kw, 3),
            self.source,
            None if self.outdoor_c is None else round(self.outdoor_c, 1),
            round(self.temperature_kw, 3),
            None if self.raw_kw is None else round(self.raw_kw, 3),
            round(self.inday_factor, 3),
        ]


@dataclass(frozen=True, slots=True)
class LoadForecast:
    """The forecast made at `made_at`: `HORIZON_H` hours from the running hour's start."""

    made_at: datetime
    hours: tuple[ForecastHour, ...]
    samples: int
    temperature: TemperatureFit
    holidays: bool
    inday: InDay = field(default_factory=lambda: NO_INDAY)

    def by_hour(self) -> dict[datetime, float]:
        """`{hour_start: kW}`, the hours with a figure only."""
        return {hour.start: hour.kw for hour in self.hours if hour.kw is not None}

    def raw_by_hour(self) -> dict[datetime, float]:
        """`{hour_start: kW}` before the in-day adjustment (the flat rung as it is), the hours with a figure only."""
        return {
            hour.start: hour.kw if hour.raw_kw is None else hour.raw_kw for hour in self.hours if hour.kw is not None
        }

    def known_by_hour(self) -> dict[datetime, float]:
        """`{hour_start: kW}` of the hours the profile knows (the flat rung and the unknown left out): what a plan
        counts on per hour, the flat figure staying the caller's own, live one."""
        return {
            hour.start: hour.kw
            for hour in self.hours
            if hour.kw is not None and hour.source not in (SOURCE_FLAT, SOURCE_NONE)
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "samples": self.samples,
            "quantile": QUANTILE,
            "half_life_days": HALF_LIFE_DAYS,
            "day_types": "weekday_weekend_holiday" if self.holidays else "weekday_weekend",
            "temperature": self.temperature.as_dict(),
            "inday": self.inday.as_dict(),
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


class _Profile:
    """The profile as known at one moment: its cells, its temperature fit and the flat figure (see the module)."""

    def __init__(self, samples: list[Sample], flat: float | None, holidays: Collection[date] | None, tz: str) -> None:
        self.zone = time_zones.require_zone(tz)
        self.holidays = holidays
        self.flat = flat
        self.fit = fit_temperature(samples) if samples else NO_TEMPERATURE
        self.by_cell: dict[tuple[str, int], list[Sample]] = {}
        self.by_hour: dict[int, list[Sample]] = {}
        for sample in samples:
            self.by_cell.setdefault((sample.day, sample.hour), []).append(sample)
            self.by_hour.setdefault(sample.hour, []).append(sample)

    def figure(self, start: datetime, quantile: float, outdoor: float | None) -> tuple[float | None, str, float]:
        """The figure for the hour starting at `start`: kW, the rung, and the temperature term's part of it."""
        local = start.astimezone(self.zone)
        cell = self.by_cell.get((day_type(local.date(), self.holidays), local.hour), [])
        source = SOURCE_PROFILE
        if len(cell) < MIN_CELL_SAMPLES:
            cell, source = self.by_hour.get(local.hour, []), SOURCE_HOUR
        if len(cell) < MIN_CELL_SAMPLES:
            return (None, SOURCE_NONE, 0.0) if self.flat is None else (self.flat, SOURCE_FLAT, 0.0)
        kw = weighted_quantile([sample.kw for sample in cell], [sample.weight for sample in cell], quantile)
        adjust = 0.0
        if self.fit.active and outdoor is not None and math.isfinite(outdoor):
            known = [_capped(sample.outdoor_c) for sample in cell if sample.outdoor_c is not None]
            if known:
                adjust = self.fit.b_kw_per_c * (sum(known) / len(known) - _capped(outdoor))
        value = max(0.0, kw + adjust)
        return value, source, value - kw


def _midnight(day: date, tz: str) -> datetime:
    """The start of the local day `day` as a UTC instant."""
    return datetime.combine(day, time.min, time_zones.require_zone(tz)).astimezone(timezone.utc)


def day_ratio(
    hours: list[HourMean], day: date, until: datetime, tz: str, holidays: Collection[date] | None
) -> tuple[float | None, int]:
    """How the local day `day` has run so far against the raw model as it was known at its midnight: the sum of the
    measured `house_kw` over the sum of the model's median (`INDAY_QUANTILE`, with the hour's recorded temperature)
    for its completed hours before `until`; `None` with fewer than `INDAY_MIN_HOURS` such hours. With how many."""
    midnight = _midnight(day, tz)
    end = min(until, _midnight(day + timedelta(days=1), tz))
    profile = _Profile(samples_from(hours, midnight, tz, holidays), None, holidays, tz)
    actual = expected = 0.0
    counted = 0
    for hour in hours:
        if hour.imported or hour.house_kw is None or not midnight <= hour.start < end:
            continue
        if hour.start + timedelta(hours=1) > until:
            continue
        figure, source, _adjust = profile.figure(hour.start, INDAY_QUANTILE, hour.outdoor_c)
        if figure is None or source == SOURCE_FLAT:
            continue
        actual += hour.house_kw
        expected += figure
        counted += 1
    if counted < INDAY_MIN_HOURS or expected <= INDAY_MIN_EXPECTED_KWH:
        return None, counted
    return actual / expected, counted


def inday_factor(ratio: float | None) -> float:
    """The damped, clamped factor a ratio gives (1 without one)."""
    if ratio is None or not math.isfinite(ratio):
        return 1.0
    return min(INDAY_MAX, max(INDAY_MIN, 1.0 + INDAY_DAMPING * (ratio - 1.0)))


@dataclass(frozen=True, slots=True)
class InDay:
    """The in-day adjustment: today's factor (from today's ratio), and yesterday's carried into today's first hours."""

    today_ratio: float | None
    today_hours: int
    today_factor: float
    yesterday_factor: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "today_ratio": None if self.today_ratio is None else round(self.today_ratio, 3),
            "today_hours": self.today_hours,
            "today_factor": round(self.today_factor, 3),
            "yesterday_factor": round(self.yesterday_factor, 3),
        }


NO_INDAY: Final = InDay(None, 0, 1.0, 1.0)


def _decayed(factor: float, start: datetime, day: date, tz: str) -> float:
    """`factor` at full weight at `day`'s midnight, falling linearly to none at its end (23 or 25 hours on DST days)."""
    first, last = _midnight(day, tz), _midnight(day + timedelta(days=1), tz)
    share = (start - first).total_seconds() / (last - first).total_seconds()
    return 1.0 + (factor - 1.0) * max(0.0, 1.0 - share)


def forecast_house_load(
    hours: Iterable[HourMean],
    now: datetime,
    tz: str,
    *,
    flat_kw: float | None = None,
    temperatures: Mapping[datetime, float] | None = None,
    holidays: Collection[date] | None = None,
    horizon_h: int = HORIZON_H,
    inday: bool = True,
    quantile: float = QUANTILE,
) -> LoadForecast:
    """The house's expected load for `horizon_h` hours from the running hour's start (see the module).

    `temperatures` maps an hour's start (UTC) to its forecast outdoor temperature (°C); `holidays` are the local dates
    of public holidays (`None`: no source, weekday/weekend only); `flat_kw` is the site's flat figure for the ladder's
    third rung. `inday` applies the in-day adjustment; `quantile` is the profile's (the house battery's plan takes the
median without the in-day adjustment, `battery/plan_runtime.py`)."""
    now = now.astimezone(timezone.utc)
    zone = time_zones.require_zone(tz)
    rows = list(hours)
    samples = samples_from(rows, now, tz, holidays)
    flat = flat_kw if flat_kw is not None and math.isfinite(flat_kw) and flat_kw >= 0 else None
    profile = _Profile(samples, flat, holidays, tz)
    today = now.astimezone(zone).date()
    adjustment = NO_INDAY
    if inday and samples:
        ratio, counted = day_ratio(rows, today, now, tz, holidays)
        yesterday, _count = day_ratio(rows, today - timedelta(days=1), now, tz, holidays)
        adjustment = InDay(ratio, counted, inday_factor(ratio), inday_factor(yesterday))
    first = hour_start(now, tz)
    # The in-day adjustment's ceiling: the highest hour measured in the window.
    ceiling = max((sample.kw for sample in samples), default=0.0)
    forecast: list[ForecastHour] = []
    for index in range(horizon_h):
        start = first + timedelta(hours=index)
        outdoor = None if temperatures is None else temperatures.get(start)
        kw, source, adjust = profile.figure(start, quantile, outdoor)
        if kw is None or source == SOURCE_FLAT:
            forecast.append(ForecastHour(start, kw, source))
            continue
        day = start.astimezone(zone).date()
        if day == today:
            factor = (
                adjustment.today_factor
                if adjustment.today_ratio is not None
                else _decayed(adjustment.yesterday_factor, start, day, tz)
            )
        elif day == today + timedelta(days=1):
            factor = _decayed(adjustment.today_factor, start, day, tz)
        else:
            factor = 1.0
        adjusted = kw * factor
        if factor > 1.0:
            adjusted = min(adjusted, max(kw, ceiling))
        forecast.append(ForecastHour(start, adjusted, source, outdoor, adjust, raw_kw=kw, inday_factor=factor))
    return LoadForecast(
        made_at=now,
        hours=tuple(forecast),
        samples=len(samples),
        temperature=profile.fit,
        holidays=holidays is not None,
        inday=adjustment,
    )
