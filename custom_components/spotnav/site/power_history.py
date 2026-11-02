"""The site's power history: per-day summaries of its hours kept for two years, and one month's power view.

The hour tracker (`site/demand_tracker.py`) keeps each completed hour of the running month and the month before.
Beside them the site keeps, per month, one summary per day for `ARCHIVE_MONTHS` months back:

* **A day** (on the area's clock, the hour's own local date): its highest hourly mean (`peak_kw`, among its complete
  hours, else among all of them) and that hour's start, the house's part of that hour (`None` when not known), and
  whether the import target held or paused a charger in any of its hours.
* **A month**: the demand-charge components it was measured against, and per component the hours counted as its
  peaks and its figure (`bar_kw`). These are worked out from the month's hours with the components set when its
  last hour completed: a later change of the components does not rewrite an older month.

Everything is refiled from the tracker's hours when an hour completes (`PowerArchive.file_month`), so the summaries
of a month with hours are always those hours'. Pure: no Home Assistant; the clock is passed in. Storage is
untrusted: a malformed record is dropped, never raised on. Nothing here decides anything a charger is told.

`power_month` builds the `spotnav/get_power_month` answer (`api/power_month.py`) for one month: the hours themselves
while the tracker still keeps them, else the kept summaries.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
from typing import Any, Final

from .. import time_zones
from ..planning.auto_settings import DemandComponent
from ..planning.demand_charge import component_month, HourMean

#: How many months before the running one keep their day summaries (the history dialog's own reach).
ARCHIVE_MONTHS: Final = 24
#: The view's power unit.
UNIT_KW: Final = "kW"

Month = tuple[int, int]


def month_text(month: Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def month_from(raw: Any) -> Month | None:
    """`YYYY-MM` as (year, month), or `None` when it is not one."""
    if not isinstance(raw, str) or len(raw) != 7 or raw[4] != "-" or not (raw[:4] + raw[5:]).isdigit():
        return None
    year, month = int(raw[:4]), int(raw[5:])
    return (year, month) if 1 <= month <= 12 and year >= 1 else None


def months_before(month: Month, count: int) -> Month:
    index = month[0] * 12 + month[1] - 1 - count
    return index // 12, index % 12 + 1


@dataclass(frozen=True, slots=True)
class DaySummary:
    """One day of the site's hours (see the module)."""

    day: date
    peak_kw: float
    peak_start: datetime
    house_kw: float | None
    held: bool
    paused: bool


@dataclass(frozen=True, slots=True)
class MonthRecord:
    """One month's kept summaries (see the module). `counted[i]` are the starts of component `i`'s counted peaks,
    `bars[i]` its figure (`None` while nothing counted)."""

    days: tuple[DaySummary, ...]
    components: tuple[DemandComponent, ...] = ()
    counted: tuple[tuple[datetime, ...], ...] = ()
    bars: tuple[float | None, ...] = ()


def _local_date(moment: datetime, tz: str) -> date:
    return moment.astimezone(time_zones.require_zone(tz)).date()


def day_summaries(hours: Sequence[HourMean], tz: str) -> tuple[DaySummary, ...]:
    """One summary per local date that has hours, oldest first."""
    by_day: dict[date, list[HourMean]] = {}
    for hour in hours:
        by_day.setdefault(_local_date(hour.start, tz), []).append(hour)
    summaries = []
    for day in sorted(by_day):
        day_hours = by_day[day]
        complete = [hour for hour in day_hours if not hour.incomplete] or day_hours
        peak = max(complete, key=lambda hour: (hour.kw, -hour.start.timestamp()))
        summaries.append(
            DaySummary(
                day=day,
                peak_kw=peak.kw,
                peak_start=peak.start,
                house_kw=peak.house_kw,
                held=any(hour.held for hour in day_hours),
                paused=any(hour.paused for hour in day_hours),
            )
        )
    return tuple(summaries)


def month_record(
    hours: Sequence[HourMean], components: Sequence[DemandComponent], tz: str, now: datetime
) -> MonthRecord:
    """A month's summaries from its hours, measured against `components`."""
    months = [component_month(component, hours, tz, now) for component in components]
    return MonthRecord(
        days=day_summaries(hours, tz),
        components=tuple(components),
        counted=tuple(tuple(peak.start for peak in month.peaks) for month in months),
        bars=tuple(month.bar_kw for month in months),
    )


# ------------------------------------------------------------------------------------------------------- storage


def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _kw(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _day_row(day: DaySummary) -> list[Any]:
    return [
        day.day.isoformat(),
        _kw(day.peak_kw),
        day.peak_start.isoformat(),
        _kw(day.house_kw),
        1 if day.held else 0,
        1 if day.paused else 0,
    ]


def _day_from_row(raw: Any) -> DaySummary | None:
    if not isinstance(raw, list) or len(raw) != 6:
        return None
    try:
        day = date.fromisoformat(raw[0]) if isinstance(raw[0], str) else None
    except ValueError:
        return None
    start = _instant(raw[2])
    peak, house, held, paused = raw[1], raw[3], raw[4], raw[5]
    if day is None or start is None or not _number(peak) or peak < 0:
        return None
    if house is not None and (not _number(house) or house < 0):
        return None
    if any(isinstance(flag, bool) or flag not in (0, 1) for flag in (held, paused)):
        return None
    return DaySummary(
        day=day,
        peak_kw=float(peak),
        peak_start=start,
        house_kw=None if house is None else float(house),
        held=bool(held),
        paused=bool(paused),
    )


def _record_dict(record: MonthRecord) -> dict[str, Any]:
    return {
        "days": [_day_row(day) for day in record.days],
        "components": [component.as_dict() for component in record.components],
        "counted": [[start.isoformat() for start in starts] for starts in record.counted],
        "bars": [_kw(bar) for bar in record.bars],
    }


def _record_from(raw: Any) -> MonthRecord | None:
    """A kept month, or `None`. Days that cannot be read are dropped; components that cannot be read drop the
    month's components (its days are kept)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), list):
        return None
    days = {day.day: day for day in (_day_from_row(item) for item in raw["days"]) if day is not None}
    record = MonthRecord(days=tuple(days[key] for key in sorted(days)))
    components_raw, counted_raw, bars_raw = raw.get("components"), raw.get("counted"), raw.get("bars")
    if not (isinstance(components_raw, list) and isinstance(counted_raw, list) and isinstance(bars_raw, list)):
        return record
    if not len(components_raw) == len(counted_raw) == len(bars_raw):
        return record
    try:
        components = tuple(DemandComponent.from_dict(item) for item in components_raw)
    except Exception:  # noqa: BLE001 - a stored component that no longer reads is dropped with its figures
        return record
    counted: list[tuple[datetime, ...]] = []
    for starts in counted_raw:
        if not isinstance(starts, list):
            return record
        instants = [_instant(item) for item in starts]
        if any(instant is None for instant in instants):
            return record
        counted.append(tuple(instant for instant in instants if instant is not None))
    if any(bar is not None and (not _number(bar) or bar < 0) for bar in bars_raw):
        return record
    bars = tuple(None if bar is None else float(bar) for bar in bars_raw)
    return MonthRecord(days=record.days, components=components, counted=tuple(counted), bars=bars)


@dataclass(slots=True)
class PowerArchive:
    """The kept months (see the module)."""

    months: dict[Month, MonthRecord] = field(default_factory=dict)

    def file_month(
        self,
        month: Month,
        hours: Sequence[HourMean],
        components: Sequence[DemandComponent],
        tz: str,
        now: datetime,
    ) -> None:
        """Keep `month`'s summaries from its hours (a month without any is not kept)."""
        if hours:
            self.months[month] = month_record(hours, components, tz, now)

    def prune(self, current: Month) -> bool:
        """Drop the months older than `ARCHIVE_MONTHS` before `current`; whether any went."""
        oldest = months_before(current, ARCHIVE_MONTHS)
        gone = [month for month in self.months if month < oldest]
        for month in gone:
            del self.months[month]
        return bool(gone)

    def as_dict(self) -> dict[str, Any]:
        return {"months": {month_text(month): _record_dict(self.months[month]) for month in sorted(self.months)}}

    def restore(self, raw: Any) -> None:
        """Take what the store kept; a malformed month is dropped."""
        months = raw.get("months") if isinstance(raw, dict) else None
        if not isinstance(months, dict):
            return
        for key, value in months.items():
            month = month_from(key)
            record = None if month is None else _record_from(value)
            if month is not None and record is not None:
                self.months[month] = record


# ------------------------------------------------------------------------------------------------------ the view


def _local_iso(moment: datetime, tz: str) -> str:
    return moment.astimezone(time_zones.require_zone(tz)).isoformat()


def power_month(
    month: Month,
    *,
    tracker_months: dict[Month, Sequence[HourMean]],
    archive: PowerArchive,
    components: Sequence[DemandComponent],
    tz: str,
    now: datetime,
    import_target_kw: float | None,
) -> dict[str, Any]:
    """One month's power view (the `spotnav/get_power_month` answer without its envelope). `tracker_months` are the
    months whose hours the tracker keeps: such a month (and the running one) is shown from its hours, measured against
    `components`; an older one from its kept summaries, with the components it was measured against; `hours` is
    `None` for it."""
    current = month_of_now(now, tz)
    oldest = months_before(current, ARCHIVE_MONTHS)
    # The running month's hours are always kept (none yet before its first hour completes).
    hours = tracker_months.get(month, [] if month == current else None)
    if hours is not None:
        record = month_record(hours, components, tz, now)
    else:
        record = archive.months.get(month) or MonthRecord(days=())
    counted_dates: dict[date, set[int]] = {}
    counted_starts: dict[datetime, set[int]] = {}
    for index, starts in enumerate(record.counted):
        for start in starts:
            counted_dates.setdefault(_local_date(start, tz), set()).add(index)
            counted_starts.setdefault(start, set()).add(index)
    available = {key for key, value in tracker_months.items() if value} | set(archive.months)
    return {
        "month": month_text(month),
        "available_months": [month_text(key) for key in sorted(available, reverse=True) if oldest <= key <= current],
        "unit": UNIT_KW,
        "import_target_kw": import_target_kw,
        "components": [component.as_dict() for component in record.components],
        "days": [
            {
                "date": day.day.isoformat(),
                "peak_kw": _kw(day.peak_kw),
                "peak_start": _local_iso(day.peak_start, tz),
                "house_kw": _kw(day.house_kw),
                "counted": sorted(counted_dates.get(day.day, ())),
                "held": day.held,
                "paused": day.paused,
            }
            for day in record.days
        ],
        "hours": None
        if hours is None
        else [
            {
                "start": _local_iso(hour.start, tz),
                "kw": _kw(hour.kw),
                "house_kw": _kw(hour.house_kw),
                "incomplete": hour.incomplete,
                "held": hour.held,
                "paused": hour.paused,
                "counted": sorted(counted_starts.get(hour.start, ())),
            }
            for hour in sorted(hours, key=lambda hour: hour.start)
        ],
        "bars": [_kw(bar) for bar in record.bars],
    }


def month_of_now(now: datetime, tz: str) -> Month:
    local = now.astimezone(time_zones.require_zone(tz))
    return local.year, local.month
