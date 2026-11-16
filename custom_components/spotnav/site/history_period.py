"""The periods the house and heat cards' history shows: a local day, a month or a year on the site's clock.

Pure. A period is named by its kind and any date inside it (`period_of`); it starts at its first local midnight and
ends at the next period's (`utc_bounds`), so a day of 23 or 25 hours (the clock change) and a year of 365 or 366 days
need no special case. Its buckets are what one bar of the history stands for: an hour of a day, a day of a month, a
month of a year (`bucket_key`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Final

from .. import time_zones

PERIOD_DAY: Final = "day"
PERIOD_MONTH: Final = "month"
PERIOD_YEAR: Final = "year"
PERIODS: Final = (PERIOD_DAY, PERIOD_MONTH, PERIOD_YEAR)
#: How many years back a period may be asked for (the recorder's long-term statistics are kept for good).
YEARS_BACK: Final = 10


@dataclass(frozen=True, slots=True)
class Period:
    """A local day, month or year: `first` is its first day, `end` the first day after it."""

    kind: str
    first: date
    end: date

    def days(self) -> list[date]:
        return [self.first + timedelta(days=offset) for offset in range((self.end - self.first).days)]

    def contains(self, day: date) -> bool:
        return self.first <= day < self.end


def _month_after(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def period_of(kind: str, day: date) -> Period:
    """The period of `kind` that holds `day`; `ValueError` for an unknown kind."""
    if kind == PERIOD_DAY:
        return Period(kind, day, day + timedelta(days=1))
    if kind == PERIOD_MONTH:
        first = date(day.year, day.month, 1)
        return Period(kind, first, _month_after(first))
    if kind == PERIOD_YEAR:
        return Period(kind, date(day.year, 1, 1), date(day.year + 1, 1, 1))
    raise ValueError(kind)


def shifted(period: Period, steps: int) -> Period:
    """The period `steps` periods later (negative: earlier)."""
    if period.kind == PERIOD_DAY:
        return period_of(PERIOD_DAY, period.first + timedelta(days=steps))
    if period.kind == PERIOD_MONTH:
        index = period.first.year * 12 + period.first.month - 1 + steps
        return period_of(PERIOD_MONTH, date(index // 12, index % 12 + 1, 1))
    return period_of(PERIOD_YEAR, date(period.first.year + steps, 1, 1))


def midnight_utc(day: date, tz: str) -> datetime:
    """A local day's first instant (UTC)."""
    zone = time_zones.require_zone(tz)
    return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)


def utc_bounds(period: Period, tz: str) -> tuple[datetime, datetime]:
    """The period's first instant and the next period's (UTC)."""
    return midnight_utc(period.first, tz), midnight_utc(period.end, tz)


def local_date(moment: datetime, tz: str) -> date:
    return moment.astimezone(time_zones.require_zone(tz)).date()


def bucket_key(period: Period, moment: datetime, tz: str) -> str:
    """The bucket `moment` falls in: its local hour's start (an instant, ISO 8601 with the offset) for a day, its local
    date for a month, its `YYYY-MM` for a year."""
    local = moment.astimezone(time_zones.require_zone(tz))
    if period.kind == PERIOD_DAY:
        return local.replace(minute=0, second=0, microsecond=0).isoformat()
    if period.kind == PERIOD_MONTH:
        return local.date().isoformat()
    return f"{local.year:04d}-{local.month:02d}"


def bucket_keys(period: Period, tz: str) -> list[str]:
    """Every bucket of the period, in order (a day's 23, 24 or 25 hours; a month's days; a year's twelve months)."""
    if period.kind == PERIOD_DAY:
        start, end = utc_bounds(period, tz)
        keys = []
        moment = start
        while moment < end:
            keys.append(bucket_key(period, moment, tz))
            moment += timedelta(hours=1)
        return keys
    if period.kind == PERIOD_MONTH:
        return [day.isoformat() for day in period.days()]
    return [f"{period.first.year:04d}-{month:02d}" for month in range(1, 13)]


def requested_period(kind: object, raw: object, today: date) -> Period:
    """The period asked for: `kind` one of `PERIODS`, `raw` a `YYYY-MM-DD` inside it (absent: today); `ValueError`
    for anything else, a period that starts after today or one more than `YEARS_BACK` years back."""
    if kind not in PERIODS:
        raise ValueError("period")
    if raw is None:
        day = today
    else:
        if not isinstance(raw, str) or len(raw) != 10:
            raise ValueError("date")
        day = date.fromisoformat(raw)
    period = period_of(str(kind), day)
    if period.first > today or period.first.year < today.year - YEARS_BACK:
        raise ValueError("date")
    return period
