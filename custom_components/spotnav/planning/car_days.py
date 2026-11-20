"""The cars' charges on the rest of the month, predicted from their history: pure, no Home Assistant.

The soft ceiling (`planning/demand_cost.py`) predicts each remaining counted day's peak. Inside the house forecast's
horizon the cars' installed plans are known (`site/demand_ceiling.with_cars`); beyond it a day was predicted from the
house alone, so early in a month the threshold saw only the next two days of car load and was too low where the cars
peak (plan v3_power_tariff, status 2026-11-20). This module adds the cars' own days:

* **Which days** (`predicted_charges`). A charge belongs to the evening it starts on (`charge_day`: the local date
  of its plug-in less `EVENING_SHIFT`, so a car plugged in at 01:00 belongs to the night before). A weekday the car
  charged on in at least `MIN_WEEKS` of its last `WEEKS` occurrences before today is predicted to charge on every
  remaining one, with the median of those charges' energy, plug-in time (from noon) and plugged time.
* **At what power** (`car_kw_by_hour`). The least power the charge must take in the hours a part counts: the energy
  less what the uncounted hours of its plugged window take at the car's top power, spread evenly over its counted
  hours (never above the top power). A charge the uncounted hours hold whole adds nothing. This is what a car planned
  under the soft ceiling draws (step 6), and the cautious side for the plan in force (a lower threshold: plan §1).
* **The plugged window** of a past charge is its plug-in (else its start) to the departure set for it (the charger's
  daily departure after the plug-in), else to where the charge ended: a car without a departure is taken as it
  charged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Final

from .. import time_zones

#: The weeks of history a weekday's habit is read from, and how many of them it must have charged in.
WEEKS: Final = 4
MIN_WEEKS: Final = 2
#: A charge plugged in before noon belongs to the evening before.
EVENING_SHIFT: Final = timedelta(hours=12)
#: A plugged window longer than this is cut to it (a car left plugged in for days).
MAX_WINDOW: Final = timedelta(hours=24)
#: Charges smaller than this are no habit (a top-up, a test).
MIN_ENERGY_KWH: Final = 1.0
HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Charge:
    """One charge of a car, past or predicted: plugged from `plugged_at` to `leaves_at` (UTC), `energy_kwh` taken at
    up to `max_kw`."""

    plugged_at: datetime
    leaves_at: datetime
    energy_kwh: float
    max_kw: float

    def as_dict(self) -> dict[str, object]:
        return {
            "plugged_at": self.plugged_at.isoformat(),
            "leaves_at": self.leaves_at.isoformat(),
            "energy_kwh": round(self.energy_kwh, 2),
            "max_kw": round(self.max_kw, 2),
        }


def plugged_window(
    plugged_at: datetime, ended_at: datetime, departure: time | None, tz: str
) -> tuple[datetime, datetime]:
    """A past charge's plugged window (see the module): to the first `departure` (local wall time) after the
    plug-in, never before the charge ended, else to the charge's end; at most `MAX_WINDOW`."""
    end = ended_at
    if departure is not None:
        zone = time_zones.require_zone(tz)
        local = plugged_at.astimezone(zone)
        leave = datetime.combine(local.date(), departure, zone)
        if leave <= local:
            leave = datetime.combine(local.date() + timedelta(days=1), departure, zone)
        end = max(ended_at, leave.astimezone(timezone.utc))
    return plugged_at, min(end, plugged_at + MAX_WINDOW)


def charge_day(moment: datetime, tz: str) -> date:
    """The evening a charge plugged in at `moment` belongs to."""
    return (moment.astimezone(time_zones.require_zone(tz)) - EVENING_SHIFT).date()


def _noon(day: date, tz: str) -> datetime:
    zone = time_zones.require_zone(tz)
    return datetime.combine(day, time(12), zone).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class _Habit:
    """A weekday's usual charge: plugged in `after_noon` past the evening's noon for `plugged`."""

    after_noon: timedelta
    plugged: timedelta
    energy_kwh: float
    max_kw: float


def predicted_charges(history: Iterable[Charge], now: datetime, last_day: date, tz: str) -> list[Charge]:
    """The charges the cars' weekday habit predicts on the evenings from today to `last_day` (see the module), each
    plugged in no earlier than `now`."""
    today = now.astimezone(time_zones.require_zone(tz)).date()
    first = today - timedelta(days=7 * WEEKS)
    by_day: dict[date, list[Charge]] = {}
    for charge in history:
        day = charge_day(charge.plugged_at, tz)
        if first <= day < today and charge.energy_kwh >= MIN_ENERGY_KWH:
            by_day.setdefault(day, []).append(charge)
    habits: dict[int, _Habit] = {}
    for weekday in range(7):
        days = [day for day in by_day if day.weekday() == weekday]
        if len(days) < MIN_WEEKS:
            continue
        # One evening's charges (a car charged twice, two cars) as one: their energy added, from the earliest plug-in
        # to the latest leaving.
        evenings = []
        for day in days:
            charges = by_day[day]
            plugged = min(charge.plugged_at for charge in charges)
            evenings.append(
                _Habit(
                    after_noon=plugged - _noon(day, tz),
                    plugged=max(charge.leaves_at for charge in charges) - plugged,
                    energy_kwh=sum(charge.energy_kwh for charge in charges),
                    max_kw=max(charge.max_kw for charge in charges),
                )
            )
        habits[weekday] = _Habit(
            after_noon=median(e.after_noon for e in evenings),
            plugged=median(e.plugged for e in evenings),
            energy_kwh=median(e.energy_kwh for e in evenings),
            max_kw=max(e.max_kw for e in evenings),
        )
    found: list[Charge] = []
    day = today
    while day <= last_day:
        habit = habits.get(day.weekday())
        if habit is not None:
            plugged = _noon(day, tz) + habit.after_noon
            if plugged >= now:
                found.append(Charge(plugged, plugged + habit.plugged, habit.energy_kwh, habit.max_kw))
        day += timedelta(days=1)
    return found


def _hours(start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """The UTC hour starts `[start, end)` touches, with the share of each it covers."""
    found: list[tuple[datetime, float]] = []
    moment = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    while moment < end:
        share = (min(end, moment + HOUR) - max(start, moment)) / HOUR
        if share > 0:
            found.append((moment, share))
        moment += HOUR
    return found


def least_counted_kw(charge: Charge, counts: Callable[[datetime], bool]) -> dict[datetime, float]:
    """The least power `charge` must draw in the hours `counts` (see the module), per UTC hour start; empty when its
    uncounted hours hold it whole."""
    hours = _hours(charge.plugged_at, charge.leaves_at)
    counted = [(start, share) for start, share in hours if counts(start)]
    free_kwh = sum(share for start, share in hours if not counts(start)) * charge.max_kw
    left = charge.energy_kwh - free_kwh
    counted_h = sum(share for _start, share in counted)
    if left <= 0 or counted_h <= 0:
        return {}
    kw = min(charge.max_kw, left / counted_h)
    return {start: kw for start, _share in counted}


def car_kw_by_hour(charges: Sequence[Charge], counts: Callable[[datetime], bool]) -> dict[datetime, float]:
    """The predicted charges' least power in the counted hours, added up per UTC hour start."""
    total: dict[datetime, float] = {}
    for charge in charges:
        for start, kw in least_counted_kw(charge, counts).items():
            total[start] = total.get(start, 0.0) + kw
    return total
