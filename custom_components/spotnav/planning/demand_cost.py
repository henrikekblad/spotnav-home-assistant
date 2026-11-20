"""What a kW of a day's peak costs under a demand charge, and the soft ceiling plans keep under: pure, no Home Assistant.

`planning/demand_charge.py` measures what each part of a demand charge bills so far; this module prices it for the
plans (`plans/v3_power_tariff.md` §1). Nothing reads it yet: a later step makes a per-site ceiling from it.

* **Marginal price** (`marginal_price`, λ). A part bills `p · mean(top N day peaks)`, so raising one day's peak
  above the month's N-th highest costs `p / N` per kW. `p` is the part's price in the month's season
  (`demand_charge.is_winter`); a price of 0 or none given is λ = 0, and a part with λ = 0 changes nothing.
* **Threshold** (`threshold`, T). The N-th highest of the month's day peaks: the measured ones so far (each hour at
  its floor, `demand_charge.free_level_hours`) together with a cautious prediction of the site's own peak on each
  remaining counted day (`predicted_day_peaks`), never below the month's free level (`free_kw`, already paid).
  With fewer than N days in all, every day enters the mean and nothing above the free level is free.
* **Predicted day peaks**. Inside the house forecast's horizon (a day all of whose counted hours ahead it covers):
  the highest counted hour of the forecast the caller gives, which is to be the house **without** battery or
  SpotNav's heat changes at its `PREDICTED_QUANTILE` (p25: overestimating T costs λ per kW, underestimating only
  moves energy), with the cars' planned charge added (a fixed load the battery plan never moves: a day a car will
  peak anyway is known as such). Beyond it: the median house-only day peak (`HourMean.house_kw`) of the same day
  type (a working day, or a weekend day or holiday) over the last `HISTORY_DAYS` days, the hours counted as the
  part counts them in any month, raised where the cars' history predicts a charge that day (`planning/car_days.py`:
  the least power the charge must take in the counted hours, on the house's median for that hour of that day type).
  A day with neither is left out (a lower T: the cautious side).
* **Soft ceiling** (`soft_ceiling`, S_h and λ_h). Per hour: the lowest T among the priced parts that count it, and
  the sum of their λ (stacked parts add). An hour no priced part counts has no ceiling.
* **Power level** (`level_count`). Göteborg Energi's tidsindelad tariff sells a power level (6, 10, 14 … kW) in its
  hours that may be exceeded `ALLOWED_EXCEEDANCES` times in a rolling 12 months. An exceedance is a complete hour
  (measured, or imported and shown complete) in those hours whose floor is above the level; an hour with a gap
  never counts.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Final

from .. import time_zones
from .auto_settings import DemandComponent
from .car_days import car_kw_by_hour, Charge
from .demand_charge import (
    component_month,
    counted_day,
    day_peaks,
    free_level_hours,
    hour_start,
    HourMean,
    is_winter,
    month_of,
)

#: The house forecast's quantile a predicted day peak is taken at (the error band's p25, `site/forecast_band.py`).
PREDICTED_QUANTILE: Final = 0.25
#: The house quantile the battery plans counted hours on (it must keep energy in hand for the peak); the car keeps
#: the forecast's own p75 (`site/load_forecast.py:QUANTILE`).
BATTERY_QUANTILE: Final = 0.90
#: The days of house-only peaks a day beyond the forecast's horizon is predicted from.
HISTORY_DAYS: Final = 28
#: How many times a power level may be exceeded in a rolling 12 months.
ALLOWED_EXCEEDANCES: Final = 3

DAY_WORK: Final = "work"
DAY_OFF: Final = "off"


# ------------------------------------------------------------------------------------------------ one part


def marginal_price(component: DemandComponent, month: int) -> float:
    """λ: what a kW more of a day's peak above the month's threshold costs, in the area's major unit (0 when the
    part has no price, or 0, in `month`'s season)."""
    price = component.price_winter if is_winter(month) else component.price_summer
    if price is None or price <= 0:
        return 0.0
    return price / component.top_n


def nth_highest(values: Iterable[float], n: int) -> float | None:
    """The `n`-th highest of `values`, `None` with fewer."""
    ranked = sorted(values, reverse=True)
    return ranked[n - 1] if len(ranked) >= n else None


def threshold(
    measured: Mapping[date, float], predicted: Mapping[date, float], top_n: int, free_kw: float | None
) -> float:
    """T: the `top_n`-th highest day peak of the month, a day measured and predicted at the higher of the two, never
    below `free_kw`; with fewer than `top_n` days, `free_kw` (or 0)."""
    days = dict(measured)
    for day, kw in predicted.items():
        days[day] = max(kw, days.get(day, kw))
    floor = free_kw or 0.0
    found = nth_highest(days.values(), top_n)
    return floor if found is None else max(found, floor)


def _local_midnight(day: date, tz: str) -> datetime:
    zone = time_zones.require_zone(tz)
    return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)


def counted_hours(component: DemandComponent, day: date, tz: str) -> list[datetime]:
    """The hour starts (UTC) `component` counts under `day`: its own hours, and a night window's morning after."""
    start = _local_midnight(day, tz)
    end = _local_midnight(day + timedelta(days=2), tz)
    hours: list[datetime] = []
    moment = start
    while moment < end:
        if counted_day(component, moment, tz) == day:
            hours.append(moment)
        moment += timedelta(hours=1)
    return hours


def remaining_days(component: DemandComponent, now: datetime, tz: str) -> list[date]:
    """The days of the running month, today on, with an hour `component` counts from the running hour on."""
    current = hour_start(now, tz)
    year, month = month_of(current, tz)
    today = current.astimezone(time_zones.require_zone(tz)).date()
    # A night that starts on yesterday may still be running.
    day = today - timedelta(days=1)
    found: list[date] = []
    while (day.year, day.month) <= (year, month):
        if any(hour >= current and month_of(hour, tz) == (year, month) for hour in counted_hours(component, day, tz)):
            found.append(day)
        day += timedelta(days=1)
    return found


def day_kind(day: date, holidays: Iterable[date]) -> str:
    """A working day, or a weekend day or holiday (`DAY_OFF`)."""
    return DAY_OFF if day.isoweekday() > 5 or day in set(holidays) else DAY_WORK


def _any_month(component: DemandComponent) -> DemandComponent:
    """The part counting its hours in every month (the history before its season still says how the house peaks)."""
    if component.window is None:
        return component
    return replace(component, window=replace(component.window, from_month=1, to_month=12))


def history_day_peaks(
    component: DemandComponent, history: Sequence[HourMean], now: datetime, tz: str
) -> dict[str, float]:
    """Per day type, the median of the house-only day peaks over the last `HISTORY_DAYS` days before today, the
    hours counted as `component` counts them in any month; a type without a day is missing."""
    part = _any_month(component)
    holidays = () if part.window is None else part.window.holidays
    today = hour_start(now, tz).astimezone(time_zones.require_zone(tz)).date()
    first = today - timedelta(days=HISTORY_DAYS)
    peaks: dict[date, float] = {}
    for hour in history:
        if hour.incomplete or hour.house_kw is None:
            continue
        day = counted_day(part, hour.start, tz)
        if day is None or not first <= day < today:
            continue
        peaks[day] = max(hour.house_kw, peaks.get(day, hour.house_kw))
    by_kind: dict[str, list[float]] = {}
    for day, kw in peaks.items():
        by_kind.setdefault(day_kind(day, holidays), []).append(kw)
    return {kind: median(values) for kind, values in by_kind.items()}


def history_hour_profile(
    component: DemandComponent, history: Sequence[HourMean], now: datetime, tz: str
) -> dict[tuple[str, int], float]:
    """Per day type and local hour of the day, the median house-only import over the last `HISTORY_DAYS` days before
    today (the hours as `component` counts them in any month); a pair without an hour is missing."""
    part = _any_month(component)
    holidays = () if part.window is None else part.window.holidays
    zone = time_zones.require_zone(tz)
    today = hour_start(now, tz).astimezone(zone).date()
    first = today - timedelta(days=HISTORY_DAYS)
    found: dict[tuple[str, int], list[float]] = {}
    for hour in history:
        if hour.incomplete or hour.house_kw is None:
            continue
        day = counted_day(part, hour.start, tz)
        if day is None or not first <= day < today:
            continue
        found.setdefault((day_kind(day, holidays), hour.start.astimezone(zone).hour), []).append(hour.house_kw)
    return {key: median(values) for key, values in found.items()}


def predicted_day_peaks(
    component: DemandComponent,
    house_by_hour: Mapping[datetime, float],
    history: Sequence[HourMean],
    now: datetime,
    tz: str,
    charges: Sequence[Charge] = (),
) -> dict[date, float]:
    """The site's predicted peak on each remaining counted day of the month (see the module). `house_by_hour` is the
    house forecast without battery or heat changes at `PREDICTED_QUANTILE` with the cars' planned charge added, keyed
    by hour start (UTC); `charges` the cars' predicted charges (`car_days.predicted_charges`), counted only on a day
    beyond the forecast."""
    current = hour_start(now, tz)
    year, month = month_of(current, tz)
    zone = time_zones.require_zone(tz)
    holidays = () if component.window is None else component.window.holidays
    usual = history_day_peaks(component, history, now, tz)
    cars = car_kw_by_hour(charges, lambda start: counted_day(component, start, tz) is not None) if charges else {}
    profile = history_hour_profile(component, history, now, tz) if cars else {}
    predicted: dict[date, float] = {}
    for day in remaining_days(component, now, tz):
        ahead = [
            hour
            for hour in counted_hours(component, day, tz)
            if hour >= current and month_of(hour, tz) == (year, month)
        ]
        known = [house_by_hour[hour] for hour in ahead if hour in house_by_hour]
        if known and len(known) == len(ahead):
            predicted[day] = max(known)
            continue
        kind = day_kind(day, holidays)
        fallback = usual.get(kind)
        car_peak = max(
            (
                profile.get((kind, hour.astimezone(zone).hour), 0.0) + cars[hour]
                for hour in ahead
                if hour in cars and hour not in house_by_hour
            ),
            default=None,
        )
        found = [kw for kw in (fallback, car_peak) if kw is not None]
        if found:
            predicted[day] = max(found)
        elif known:
            predicted[day] = max(known)
    return predicted


@dataclass(frozen=True, slots=True)
class PartCost:
    """One part of a demand charge, priced for the plans at one moment."""

    component: DemandComponent
    #: λ, in the area's major unit per kW above `threshold_kw`; 0 for a part that changes nothing this month.
    marginal: float
    #: T (kW); `None` for a part with `marginal` 0.
    threshold_kw: float | None
    free_kw: float | None
    measured: Mapping[date, float]
    predicted: Mapping[date, float]


def part_cost(
    component: DemandComponent,
    hours: Sequence[HourMean],
    house_by_hour: Mapping[datetime, float],
    history: Sequence[HourMean],
    now: datetime,
    tz: str,
    charges: Sequence[Charge] = (),
) -> PartCost:
    """`component` priced at `now` from the month's `hours`, the house forecast, the house's `history` and the cars'
    predicted `charges`."""
    _year, month = month_of(hour_start(now, tz), tz)
    marginal = marginal_price(component, month)
    floored = free_level_hours(hours)
    free_kw = component_month(component, floored, tz, now).free_kw
    if marginal <= 0:
        return PartCost(component, 0.0, None, free_kw, {}, {})
    measured = {day: peak.kw for day, peak in day_peaks(component, floored, tz).items()}
    predicted = predicted_day_peaks(component, house_by_hour, history, now, tz, charges)
    return PartCost(
        component=component,
        marginal=marginal,
        threshold_kw=threshold(measured, predicted, component.top_n, free_kw),
        free_kw=free_kw,
        measured=measured,
        predicted=predicted,
    )


# ------------------------------------------------------------------------------------------------ per hour


@dataclass(frozen=True, slots=True)
class HourCeiling:
    """An hour's soft ceiling: the import (kW) above which its day's peak costs `price` per kW. `day` is the day the
    hour's import counts toward (the counting part's, `demand_charge.counted_day`); `None` where not known."""

    kw: float
    price: float
    day: date | None = field(default=None, compare=False)


def soft_ceiling(parts: Sequence[PartCost], starts: Iterable[datetime], tz: str) -> dict[datetime, HourCeiling]:
    """S_h and λ_h for each hour in `starts` some priced part counts; hours no priced part counts are left out."""
    priced = [part for part in parts if part.marginal > 0 and part.threshold_kw is not None]
    ceilings: dict[datetime, HourCeiling] = {}
    for start in starts:
        found = [part for part in priced if counted_day(part.component, start, tz) is not None]
        if not found:
            continue
        lowest = min(found, key=lambda part: part.threshold_kw or 0.0)
        ceilings[start] = HourCeiling(
            kw=lowest.threshold_kw or 0.0,
            price=sum(part.marginal for part in found),
            day=counted_day(lowest.component, start, tz),
        )
    return ceilings


def raise_cost(ceiling: HourCeiling, kw: float) -> float:
    """What raising the hour's day peak to `kw` costs (0 at or under the ceiling)."""
    return ceiling.price * max(0.0, kw - ceiling.kw)


# ------------------------------------------------------------------------------------------------ power level


@dataclass(frozen=True, slots=True)
class LevelCount:
    """A power level's exceedances in the rolling 12 months up to now."""

    level_kw: float
    #: The starts (UTC) of the hours that exceeded it, oldest first.
    exceeded: tuple[datetime, ...]
    allowed: int = ALLOWED_EXCEEDANCES

    @property
    def used(self) -> int:
        return len(self.exceeded)

    @property
    def next_number(self) -> int:
        """Which exceedance the next one would be ("exceedance 2 of 3")."""
        return self.used + 1

    @property
    def next_is_last(self) -> bool:
        """Whether the next exceedance is the last one allowed (asked twice), or past it."""
        return self.next_number >= self.allowed


def year_before(now: datetime, tz: str) -> datetime:
    """The same local moment a year before `now` (28 February for the 29th), as a UTC instant."""
    local = now.astimezone(time_zones.require_zone(tz))
    try:
        earlier = local.replace(year=local.year - 1)
    except ValueError:
        earlier = local.replace(year=local.year - 1, day=28)
    return earlier.astimezone(timezone.utc)


def level_count(
    hours: Iterable[HourMean],
    level_kw: float,
    counts: Callable[[datetime], bool],
    now: datetime,
    tz: str,
    allowed: int = ALLOWED_EXCEEDANCES,
) -> LevelCount:
    """The complete hours in the rolling 12 months up to `now` that `counts` (the level's hours) and whose floor
    was above `level_kw`; an hour with a gap, or imported and not shown complete, never counts."""
    since = year_before(now, tz)
    exceeded = sorted(
        hour.start
        for hour in free_level_hours(hours)
        if not hour.incomplete and since <= hour.start < now and counts(hour.start) and hour.kw > level_kw
    )
    return LevelCount(level_kw=level_kw, exceeded=tuple(exceeded), allowed=allowed)
