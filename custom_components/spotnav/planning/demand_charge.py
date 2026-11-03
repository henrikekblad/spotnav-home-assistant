"""A demand charge's components measured against the month's hours: pure, no Home Assistant.

A grid operator's demand charge (effektavgift) bills the hourly mean of net import: per component, the mean of the
month's `top_n` highest hourly means, each on a different day, among the hours the component counts (all hours, or
its own window, `planning/auto_settings.py:DemandComponent`). The site's hour tracker (`site/demand_tracker.py`)
keeps every hour's mean of the month; this module turns them into what each component bills so far.

* An hour's day is the window's day (`HighLoadWindow.window_day`: a night belongs to the day it starts on) for a
  component with a window, else its own date on the area's clock. The month is the hour's own month on that clock,
  as the meter's month is: a night that starts on the last of a month and ends on the first of the next counts its
  morning hours in the next month (under the day it started on).
* An hour with a gap (`HourMean.incomplete`) never raises a peak: its mean is not known well enough to bill on.
* `bar_kw` is the mean of the `top_n` highest day peaks (of fewer while fewer days have one, as the operators bill
  the month so far); `None` while no hour counted.
* `free_kw` is the hourly mean the running hour may reach without raising the bar: today's own peak while today is
  among the `top_n` days, else the `top_n`-th highest day peak; with fewer than `top_n` days every new day enters
  the mean, so only today's own peak (or nothing, 0) is free. `None` while the running hour does not count for the
  component (nothing it draws then is billed by it).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Final

from .. import time_zones
from .auto_settings import DemandComponent

#: The months a component's winter price applies in (November to March).
WINTER_MONTHS: Final = frozenset({11, 12, 1, 2, 3})


@dataclass(frozen=True, slots=True)
class HourMean:
    """One completed clock hour of the site: its start (UTC, the local hour's start), the mean net import over the
    time it was measured (kW), the house's share without the chargers' measured power (`None` when the chargers
    could not be read for most of it), whether it had gaps (`incomplete`), and whether the site's import target held a
    charger's current down (`held`) or paused a charger (`paused`) in it. The last two are shown only."""

    start: datetime
    kw: float
    house_kw: float | None = None
    incomplete: bool = False
    held: bool = False
    paused: bool = False
    #: The hour's import over the whole hour, the time without a reading counted as no import (kW): never above what
    #: the meter billed, where `kw` (the mean over the time measured) may be. `None` for an hour kept before it was.
    floor_kw: float | None = None
    #: Read from Home Assistant's hourly statistics (`site/hour_statistics.py`), not measured by SpotNav: shown and
    #: counted in a month's peaks, but never part of the month's free level (`free_levels_by_hour`, the import
    #: target's lift), so an approximation never raises what a charger may draw.
    imported: bool = False


@dataclass(frozen=True, slots=True)
class DayPeak:
    """A day's highest counted hour for one component."""

    day: date
    start: datetime
    kw: float


@dataclass(frozen=True, slots=True)
class ComponentMonth:
    """What one component bills so far this month (see the module)."""

    bar_kw: float | None
    free_kw: float | None
    #: The `top_n` highest day peaks, highest first.
    peaks: tuple[DayPeak, ...]
    #: Days with a counted, complete hour.
    days_counted: int
    #: The price per kW that applies in this month's season, `None` while the person has not given it.
    price_per_kw: float | None


def hour_start(moment: datetime, tz: str) -> datetime:
    """The start of the clock hour `moment` falls in, on `tz`'s clock, as a UTC instant. The repeated autumn hour
    is two hours, as on the meter."""
    zone = time_zones.require_zone(tz)
    local = moment.astimezone(zone)
    return local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def month_of(start: datetime, tz: str) -> tuple[int, int]:
    """The (year, month) on `tz`'s clock an hour starting at `start` is billed in."""
    local = start.astimezone(time_zones.require_zone(tz))
    return local.year, local.month


def is_winter(month: int) -> bool:
    return month in WINTER_MONTHS


def counted_day(component: DemandComponent, start: datetime, tz: str) -> date | None:
    """The day an hour starting at `start` is counted under for `component`, or `None` when it does not count."""
    if component.window is None:
        return start.astimezone(time_zones.require_zone(tz)).date()
    return replace(component.window, tz=tz).window_day(start)


def day_peaks(component: DemandComponent, hours: Iterable[HourMean], tz: str) -> dict[date, DayPeak]:
    """Each day's highest counted, complete hour for `component`."""
    peaks: dict[date, DayPeak] = {}
    for hour in hours:
        if hour.incomplete:
            continue
        day = counted_day(component, hour.start, tz)
        if day is None:
            continue
        best = peaks.get(day)
        if best is None or hour.kw > best.kw:
            peaks[day] = DayPeak(day=day, start=hour.start, kw=hour.kw)
    return peaks


def component_month(
    component: DemandComponent, hours: Sequence[HourMean], tz: str, now: datetime
) -> ComponentMonth:
    """What `component` bills so far over `hours` (this month's), and what the hour running at `now` may reach."""
    peaks = day_peaks(component, hours, tz)
    ranked = sorted(peaks.values(), key=lambda peak: (-peak.kw, peak.start))
    top = tuple(ranked[: component.top_n])
    bar = None if not top else sum(peak.kw for peak in top) / len(top)
    today = counted_day(component, hour_start(now, tz), tz)
    free: float | None = None
    if today is not None:
        own = peaks.get(today)
        if own is not None and own in top:
            free = own.kw
        elif len(ranked) >= component.top_n:
            free = top[-1].kw
        else:
            free = 0.0 if own is None else own.kw
    _year, month = month_of(hour_start(now, tz), tz)
    price = component.price_winter if is_winter(month) else component.price_summer
    return ComponentMonth(bar_kw=bar, free_kw=free, peaks=top, days_counted=len(peaks), price_per_kw=price)


def free_level_hours(hours: Iterable[HourMean]) -> list[HourMean]:
    """The hours the month's free level is worked out from: SpotNav's own measured hours (never an imported one),
    each at its floor (its import over the whole hour, a gap counted as none: never above what the meter billed)."""
    return [
        hour if hour.floor_kw is None else replace(hour, kw=min(hour.kw, hour.floor_kw))
        for hour in hours
        if not hour.imported
    ]


def free_levels_by_hour(
    components: Sequence[DemandComponent], hours: Sequence[HourMean], tz: str
) -> dict[datetime, float | None]:
    """For each of a month's `hours`, the month's free level when it ran: the lowest `free_kw` among the parts that
    count it, worked out as `component_month` would have at its start from the month's earlier hours
    (`free_level_hours`: measured ones only, at their floor). `None` for an hour no part counts. For the power view's
    line of the month's peak; the import target's own lift reads the running hour the same way."""
    ordered = sorted(hours, key=lambda hour: hour.start)
    peaks: list[dict[date, DayPeak]] = [{} for _ in components]
    levels: dict[datetime, float | None] = {}
    for hour in ordered:
        found: list[float] = []
        for index, component in enumerate(components):
            today = counted_day(component, hour.start, tz)
            if today is None:
                continue
            ranked = sorted(peaks[index].values(), key=lambda peak: (-peak.kw, peak.start))
            top = ranked[: component.top_n]
            own = peaks[index].get(today)
            if own is not None and own in top:
                found.append(own.kw)
            elif len(ranked) >= component.top_n:
                found.append(top[-1].kw)
            else:
                found.append(0.0 if own is None else own.kw)
        levels[hour.start] = min(found) if found else None
        for counted in free_level_hours((hour,)):
            if counted.incomplete:
                continue
            for index, component in enumerate(components):
                day = counted_day(component, counted.start, tz)
                if day is None:
                    continue
                best = peaks[index].get(day)
                if best is None or counted.kw > best.kw:
                    peaks[index][day] = DayPeak(day=day, start=counted.start, kw=counted.kw)
    return levels
