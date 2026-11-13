"""A learned error band around a forecast (SpotNav 3.0, wave 6): how far the house and the sun usually miss, per hour
of the day, from the site's own forecast log. Pure: no Home Assistant, the clock is passed in.

The plans count on a point forecast: the house's median and the sun as corrected. This learns how that figure has
missed on this site, so a low (p10) and a high (p90) figure can be shown beside it and a defensive plan can be made in
shadow (`battery/plan_shadow.py` `defensive_inputs`). No setting, no ML: the empirical quantiles of the misses.

* **Pairs**: `(hour start, forecast, outcome)` from the forecast log (`site/forecast_log.py`): the first forecast made
  for each scored hour of the last `WINDOW_DAYS` days (the log keeps 14) before today.
* **House** (`FORM_ADDITIVE`): the miss is `outcome - forecast` (kW). Per day type (weekday, weekend; a holiday
  counts as a weekend day when the dates are given) and local hour, the misses of that hour and its neighbours
  (`NEIGHBOUR_H` either side) are pooled; a cell with fewer than `MIN_SAMPLES` takes both day types.
* **Sun** (`FORM_RATIO`): the miss is `outcome / forecast`, only for hours whose forecast is above `SUN_MIN_KWH`
  (a ratio of next to nothing says nothing), clamped to `RATIO_MAX`; per local hour, pooled as above, with every
  hour that had sun together as the fallback (an hour that had none lately, say as the days grow longer). A dark
  hour's band is the forecast itself (0).
* **Quantiles**: `LOW_Q` and `HIGH_Q` (p10, p90) of the pooled misses, linear between order statistics. The band is
  learnt only once `MIN_DAYS` days have scored hours; before that there is none (`status` "learning").

The band decides nothing a charger is told and nothing that is written to a device: it is shown in the diagnostics
and the debug bundle, and the defensive battery plan built on it is a shadow reviewed beside the others.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import math
from typing import Any, Final

from .. import time_zones
from .load_forecast import day_type

#: The days of misses read (the forecast log keeps 14).
WINDOW_DAYS: Final = 14
#: A band is learnt only from this many days with scored hours on.
MIN_DAYS: Final = 7
#: The hours either side of a cell's hour pooled with it.
NEIGHBOUR_H: Final = 1
#: A cell with fewer misses than this takes the wider pool (both day types; for the sun, every daylight hour).
MIN_SAMPLES: Final = 12
#: The sun's misses are read only from hours whose forecast is above this (kWh in the hour).
SUN_MIN_KWH: Final = 0.2
#: A sun ratio above this is counted as this (a forecast of 0.3 kWh in a 2 kWh hour says little of the next).
RATIO_MAX: Final = 3.0
#: The band's quantiles.
LOW_Q: Final = 0.1
HIGH_Q: Final = 0.9

FORM_ADDITIVE: Final = "additive"
FORM_RATIO: Final = "ratio"
STATUS_LEARNT: Final = "learnt"
STATUS_LEARNING: Final = "learning"
#: The pool a cell's figures came from.
POOL_CELL: Final = "cell"
POOL_WIDE: Final = "wide"


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def quantile(values: list[float], q: float) -> float:
    """The `q` quantile of `values` (not empty), linear between the order statistics."""
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    below = math.floor(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)


@dataclass(frozen=True, slots=True)
class Spread:
    """One cell's misses: the low and high quantile (kW for the house, a ratio for the sun), and from how many."""

    low: float
    high: float
    samples: int
    pool: str

    def as_dict(self) -> dict[str, Any]:
        return {"low": round(self.low, 3), "high": round(self.high, 3), "samples": self.samples, "pool": self.pool}


@dataclass(frozen=True, slots=True)
class ErrorBand:
    """A learnt band (see the module): per `(day type or None, local hour)` its spread."""

    form: str
    tz: str
    days: int
    cells: Mapping[tuple[str | None, int], Spread]
    holidays: Collection[date] | None = None
    by_day_type: bool = True

    def spread_at(self, start: datetime) -> Spread | None:
        local = start.astimezone(time_zones.require_zone(self.tz))
        key = (day_type(local.date(), self.holidays) if self.by_day_type else None, local.hour)
        return self.cells.get(key)

    def bounds(self, value: float, start: datetime) -> tuple[float, float]:
        """The low and high figure for a forecast of `value` in the hour starting at `start`; the forecast itself
        for an hour without a spread. Never below 0."""
        spread = self.spread_at(start)
        if spread is None:
            return value, value
        if self.form == FORM_RATIO:
            return max(0.0, value * spread.low), max(0.0, value * spread.high)
        return max(0.0, value + spread.low), max(0.0, value + spread.high)

    def low_high(self, by_hour: Mapping[datetime, float]) -> tuple[dict[datetime, float], dict[datetime, float]]:
        """The low and high figure of every hour of a forecast (`{hour start: value}`)."""
        low: dict[datetime, float] = {}
        high: dict[datetime, float] = {}
        for start, value in by_hour.items():
            low[start], high[start] = self.bounds(value, start)
        return low, high

    def as_dict(self) -> dict[str, Any]:
        def name(key: tuple[str | None, int]) -> str:
            return f"{key[1]:02d}" if key[0] is None else f"{key[0]}/{key[1]:02d}"

        return {
            "form": self.form,
            "days": self.days,
            "quantiles": [LOW_Q, HIGH_Q],
            "cells": {name(key): spread.as_dict() for key, spread in sorted(self.cells.items(), key=lambda i: name(i[0]))},
        }


@dataclass(slots=True)
class _Miss:
    day: date
    day_type: str
    hour: int
    miss: float


def _misses(
    pairs: Iterable[tuple[datetime, float, float]],
    now: datetime,
    tz: str,
    form: str,
    holidays: Collection[date] | None,
) -> list[_Miss]:
    zone = time_zones.require_zone(tz)
    today = now.astimezone(zone).date()
    first = today - timedelta(days=WINDOW_DAYS)
    out: list[_Miss] = []
    for start, forecast, actual in pairs:
        if not (_number(forecast) and _number(actual)):
            continue
        local = start.astimezone(zone)
        if not first <= local.date() < today:
            continue
        if form == FORM_RATIO:
            if forecast <= SUN_MIN_KWH or actual < 0:
                continue
            miss = min(RATIO_MAX, actual / forecast)
        else:
            miss = actual - forecast
        out.append(_Miss(local.date(), day_type(local.date(), holidays), local.hour, miss))
    return out


def _near(hour: int, other: int) -> bool:
    distance = abs(hour - other) % 24
    return min(distance, 24 - distance) <= NEIGHBOUR_H


def learn(
    pairs: Iterable[tuple[datetime, float, float]],
    now: datetime,
    tz: str,
    form: str,
    *,
    holidays: Collection[date] | None = None,
) -> ErrorBand | None:
    """The band of `pairs` (see the module), `None` while fewer than `MIN_DAYS` days have misses."""
    misses = _misses(pairs, now, tz, form, holidays)
    days = len({miss.day for miss in misses})
    if days < MIN_DAYS:
        return None
    by_day_type = form == FORM_ADDITIVE
    cells: dict[tuple[str | None, int], Spread] = {}
    types: tuple[str | None, ...] = tuple(sorted({miss.day_type for miss in misses})) if by_day_type else (None,)
    for kind in types:
        for hour in range(24):
            near = [miss for miss in misses if _near(hour, miss.hour)]
            pool, values = POOL_CELL, [miss.miss for miss in near if kind is None or miss.day_type == kind]
            if len(values) < MIN_SAMPLES:
                pool = POOL_WIDE
                # The house: both day types of the same hours; the sun: every hour that had sun.
                values = [miss.miss for miss in (near if by_day_type else misses)]
            if len(values) < MIN_SAMPLES:
                continue
            cells[(kind, hour)] = Spread(quantile(values, LOW_Q), quantile(values, HIGH_Q), len(values), pool)
    return ErrorBand(form, tz, days, cells, holidays, by_day_type)


def pairs_from_log(hours: Mapping[datetime, Any]) -> list[tuple[datetime, float, float]]:
    """A forecast log kind's scored hours (`ForecastLog.hours`) as `(start, forecast, outcome)`."""
    return [(start, hour.forecast, hour.actual) for start, hour in sorted(hours.items()) if hour.actual is not None]


@dataclass(slots=True)
class BandView:
    """What the diagnostics show of a band: its status, the band itself and the hours it was applied to."""

    status: str
    band: ErrorBand | None = None
    hours: dict[datetime, tuple[float, float, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "band": None if self.band is None else self.band.as_dict(),
            # Per hour: [p10, median (the forecast the plan counts on), p90].
            "hours": [
                [start.isoformat(), round(low, 3), round(mid, 3), round(high, 3)]
                for start, (low, mid, high) in sorted(self.hours.items())
            ],
        }


def view(band: ErrorBand | None, by_hour: Mapping[datetime, float]) -> BandView:
    """The band over a forecast's hours: `[p10, median, p90]` per hour."""
    if band is None:
        return BandView(STATUS_LEARNING)
    hours = {}
    for start, value in by_hour.items():
        low, high = band.bounds(value, start)
        hours[start] = (low, value, high)
    return BandView(STATUS_LEARNT, band, hours)
