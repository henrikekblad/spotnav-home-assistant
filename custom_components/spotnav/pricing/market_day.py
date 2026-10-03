"""Two calendars, and the one place they meet (contract v2's `market_tz`).

A day file `<MM-DD>` covers `[MM-DD 00:00, next day 00:00)` on the area's **market** calendar
(`market_tz`); everything a person sees ("today", "tomorrow", the chart, the plan, a session's day) is
on the area's **display** calendar (`tz`). For almost every area the two are one zone and a display
day is exactly one file, and then nothing here is used. Great Britain is not: Agile is published
23:00 to 23:00 UK time, which is a Paris day, so a London day is the last 23 hours of one file and
the first hour of the next, and on the evening of the 4th in London it is already the 5th in Paris.
Portugal in v2 (Lisbon shown, Madrid calendar) has the same shape.

Everything here is instants: a display day is `[its first instant, the next day's first instant)` in
`tz`, and a file belongs to it when any part of the file's market day overlaps that span, so a
clock-change day of 46 or 50 half-hours needs no case of its own. The relay's web page
(`spotnav-relay/web/src/domain/market-day.ts`) is the reference client; the rules are the same.

Pure: no `hass`, no clock, no storage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Iterable

from homeassistant.util import dt as dt_util

from .relay_contract import PriceDocument, PriceInterval


def local_midnight(day: date, tz: str) -> datetime:
    """The first instant of a local calendar date, from the zone's own rules."""
    return datetime(day.year, day.month, day.day, tzinfo=dt_util.get_time_zone(tz))


def display_day_span(day: date, tz: str) -> tuple[datetime, datetime]:
    """The first and the first-after instants of a display date, in UTC."""
    start = local_midnight(day, tz).astimezone(dt_util.UTC)
    end = local_midnight(day + timedelta(days=1), tz).astimezone(dt_util.UTC)
    return start, end


def market_days_for(day: date, tz: str, market_tz: str) -> tuple[date, ...]:
    """The market-day files that cover the display date [day], in date order: one when the two
    calendars agree, two when they do not (never more for zones within a day of each other).
    """
    if tz == market_tz:
        return (day,)
    start, end = display_day_span(day, tz)
    zone = dt_util.get_time_zone(market_tz)
    first = start.astimezone(zone).date()
    last = (end - timedelta(microseconds=1)).astimezone(zone).date()
    days = [first]
    # Bounded: two zones on this planet are never more than a calendar day apart.
    while len(days) < 3 and days[-1] < last:
        days.append(days[-1] + timedelta(days=1))
    return tuple(days)


def principal_market_day(day: date, tz: str, market_tz: str) -> date:
    """The market day holding the middle of the display date: the file most of it comes from.

    It is the one whose publication decides whether the display day exists ("tomorrow published"): a
    London day is 23 hours of one Paris file and one hour of the next, so it is the first; a zone ahead
    of its market calendar would be the second. Equal to [day] when the calendars agree.
    """
    if tz == market_tz:
        return day
    start, end = display_day_span(day, tz)
    middle = start + (end - start) / 2
    return middle.astimezone(dt_util.get_time_zone(market_tz)).date()


def market_day_of(instant: datetime, market_tz: str) -> date:
    """The market day an instant falls in (the file that prices it)."""
    return instant.astimezone(dt_util.get_time_zone(market_tz)).date()


def _cut(document: PriceDocument, start: datetime, end: datetime) -> tuple[PriceInterval, ...]:
    return tuple(
        interval for interval in document.intervals if start <= interval.utc_start and interval.utc_end <= end
    )


def compose_display_day(
    area_id: str, day: date, tz: str, files: Iterable[PriceDocument]
) -> PriceDocument | None:
    """The display date [day] cut from whichever market-day [files] are held, or `None` when none of them
    prices a moment of it.

    The result is one document in the display zone, labelled with the display date, whose `parts` are the
    cut files: each keeps its own rate, resolution and provenance, so a reader converting money per piece
    prices the next file's hour with the next file's rate. The composed document's own positional
    `prices` run from its first interval at the finest resolution of its parts, and stop at the first gap
    (a day whose later file is not out yet simply ends early). Its own rate table and provenance are the
    first part's.
    """
    zone = dt_util.get_time_zone(tz)
    start, end = display_day_span(day, tz)
    parts: list[PriceDocument] = []
    for document in sorted(files, key=lambda item: item.start_instant):
        cut = _cut(document, start, end)
        if not cut:
            continue
        local = tuple(
            PriceInterval.from_instants(item.utc_start, item.utc_end, zone=zone, eur_per_kwh=item.eur_per_kwh)
            for item in cut
        )
        parts.append(
            replace(
                document,
                day=day,
                tz=tz,
                start=local[0].start,
                start_instant=local[0].utc_start,
                prices=tuple(item.eur_per_kwh for item in local),
                intervals=local,
                parts=(),
            )
        )
    if not parts:
        return None
    # Contiguous from the first part only: a later piece after a hole is not this day's continuation.
    kept = [parts[0]]
    for part in parts[1:]:
        if part.start_instant != kept[-1].intervals[-1].utc_end:
            break
        kept.append(part)
    if len(kept) == 1:
        return kept[0]
    resolution = min(part.resolution_minutes for part in kept)
    step = timedelta(minutes=resolution)
    intervals: list[PriceInterval] = []
    for part in kept:
        for interval in part.intervals:
            cursor = interval.utc_start
            while cursor < interval.utc_end:
                intervals.append(
                    PriceInterval.from_instants(cursor, cursor + step, zone=zone, eur_per_kwh=interval.eur_per_kwh)
                )
                cursor += step
    first = kept[0]
    return replace(
        first,
        resolution_minutes=resolution,
        prices=tuple(item.eur_per_kwh for item in intervals),
        intervals=tuple(intervals),
        fx=MappingProxyType(dict(first.fx)),
        parts=tuple(kept),
    )
