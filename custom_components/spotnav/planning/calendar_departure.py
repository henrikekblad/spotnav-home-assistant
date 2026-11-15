"""The departure from a Home Assistant calendar: pure, no `hass` in it.

A charger may name one calendar (`AutoSettings.departure_calendar`). Its events within the next
`MAX_DEPARTURE_DAYS_AHEAD` days are departures:

* **The first event of a day is that day's departure.** It replaces the daily departure on its day (earlier or
  later, the calendar says when the car leaves that day); later events that day are not departures (a car leaves
  once a day: the evening's appointment is a trip from wherever it is then). Once the first has gone, that day has
  no departure left. A day without an event keeps the daily departure, on the chosen weekdays as before, unless
  the calendar alone decides (`calendar_only`): then a day without an event has no departure at all. The plan is
  for the earliest of these that is still ahead.
* **An all-day event** is a departure at the daily departure time on its first day (so it adds a departure on a
  weekday that is left out, and keeps the time on one that is in, where its target still counts). It is that
  day's first unless a timed event that day starts earlier; at the same minute the timed event is the first (its
  text is the more particular one).
* **A multi-day event** is a departure at its start only; the days it covers after that are ordinary days.
* An event that has begun (or begins before the first planning slot) is gone; an event that began before today is
  not on any day here.
* **Time zones**: an event's instant is read in the price area's zone, the zone every departure is in. An all-day
  event's date is its own (calendars give it without a zone).
* **The target**: a target in the event's title or description (`mål: 80`, `target: 80 %`, `80%`, `30 kWh`) is the
  plan's for that departure: a percentage when the charger plans to a level, energy when it plans an amount. One of
  the other kind is not used (`target_ignored`).

What this module does not decide (the controller does, `auto_controller.py`): the departure switch off or a guest
car (the calendar is not read); a one-off date the person chose (it wins while it is ahead, except when the calendar alone decides); a calendar that cannot
be read (the daily departure, also when the calendar alone decides: a car that must leave is safer charged by the
daily time than not at all, and the dialog says so); the calendar alone deciding and no event ahead (no departure:
the charger plans as with the departure switched off).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Final

from .. import time_zones
from .planner import MAX_DEPARTURE_DAYS_AHEAD, STEP_MINUTES, local_instant

#: How far ahead events are read, in local days: as far as a one-off date may lie.
CALENDAR_DAYS_AHEAD: Final = MAX_DEPARTURE_DAYS_AHEAD

#: Largest energy an event may ask for; above it the text is read as something else (an odometer, a price).
MAX_EVENT_KWH: Final = 200.0

#: Words that name a target, in the card's languages (lower case). `mål` covers Swedish, Norwegian and Danish.
_TARGET_WORDS: Final = ("mål", "maal", "target", "ziel", "tavoite", "objectif", "cible", "objetivo", "doel")
#: A number that is whole where it stands: never the tail or head of a longer one.
_NUMBER: Final = r"(\d{1,3}(?:[.,]\d+)?)(?!\d|[.,]\d)"
_LABELLED: Final = re.compile(
    rf"(?<!\w)(?:{'|'.join(_TARGET_WORDS)})\s*[:=]?\s*{_NUMBER}\s*(%|kwh\b)?", re.IGNORECASE
)
_PERCENT: Final = re.compile(rf"(?<![\d.,]){_NUMBER}\s*%", re.IGNORECASE)
_KWH: Final = re.compile(rf"(?<![\d.,]){_NUMBER}\s*kwh\b", re.IGNORECASE)



@dataclass(frozen=True, slots=True)
class EventTarget:
    """A target read from an event's text: a level, an amount, or (rarely) both written."""

    percent: float | None = None
    kwh: float | None = None


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """One event as `calendar.get_events` answers it: a timed start (aware) or an all-day date."""

    start: datetime | date
    summary: str = ""
    description: str = ""

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


@dataclass(frozen=True, slots=True)
class CalendarDeparture:
    """The departure the calendar makes the plan's: on `day` at `at` (area-local wall time), the instant
    `instant`. `event` is the event behind it, or `None` when it is a daily departure the calendar moved to (an
    event took the day the daily one would have been on)."""

    day: date
    at: time
    instant: datetime
    event: CalendarEvent | None
    target: EventTarget | None


def parse_target(text: str) -> EventTarget | None:
    """A target written in an event's text, or `None`.

    A labelled number (`mål: 80`, `target 80 %`) is a percentage unless `kWh` follows; else the first `N %`; and the
    first `N kWh`. A percentage outside 1-100 or energy outside (0, `MAX_EVENT_KWH`] is not one.
    """
    if not text:
        return None
    percent: float | None = None
    labelled = _LABELLED.search(text)
    if labelled is not None and (labelled.group(2) or "").lower() != "kwh":
        percent = _number(labelled.group(1))
    if percent is None:
        match = _PERCENT.search(text)
        percent = None if match is None else _number(match.group(1))
    if percent is not None and not 1 <= percent <= 100:
        percent = None
    kwh: float | None = None
    match = _KWH.search(text)
    if match is not None:
        value = _number(match.group(1))
        if value is not None and 0 < value <= MAX_EVENT_KWH:
            kwh = value
    if percent is None and kwh is None:
        return None
    return EventTarget(percent=percent, kwh=kwh)


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def events_from_response(raw: Any) -> tuple[CalendarEvent, ...]:
    """The events of one calendar's `get_events` answer (`{"events": [...]}` or the list itself).

    Lenient: an event without a readable start is left out, never the calendar. A timed start must carry its offset
    (Home Assistant always gives one); a naive one is left out rather than guessed.
    """
    items = raw.get("events") if isinstance(raw, Mapping) else raw
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes)):
        return ()
    events: list[CalendarEvent] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        start = _start(item.get("start"))
        if start is None:
            continue
        summary = item.get("summary")
        description = item.get("description")
        events.append(
            CalendarEvent(
                start=start,
                summary=summary if isinstance(summary, str) else "",
                description=description if isinstance(description, str) else "",
            )
        )
    return tuple(events)


def _start(value: Any) -> datetime | date | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _first_key(item: tuple[datetime, int, int, CalendarEvent]) -> tuple[datetime, int, int]:
    instant, all_day, order, _event = item
    return instant.astimezone(timezone.utc), all_day, order


def first_slot(now: datetime, zone: Any) -> datetime:
    """`now` rounded up to the planning grid: a departure must lie after it to be planned for."""
    local_now = now.astimezone(zone)
    floored = local_now - timedelta(
        minutes=local_now.minute % STEP_MINUTES, seconds=local_now.second, microseconds=local_now.microsecond
    )
    return floored if floored >= local_now else floored + timedelta(minutes=STEP_MINUTES)


def calendar_departure(
    events: Iterable[CalendarEvent],
    *,
    now: datetime,
    tz: str,
    daily: time,
    weekdays: Iterable[int],
    calendar_only: bool = False,
) -> CalendarDeparture | None:
    """The departure the calendar makes the plan's, or `None` when it changes nothing (no event within
    `CALENDAR_DAYS_AHEAD` days moves the next daily departure) or, with `calendar_only`, when no event ahead is a
    departure (there is none).

    The rules are the module's. `weekdays` are the daily departure's (ISO numbers); `daily` its wall time, also an
    all-day event's. `calendar_only`: a day without an event has no daily departure.
    """
    zone = time_zones.zone(tz)
    if zone is None:
        return None
    after = first_slot(now, zone).astimezone(timezone.utc)
    today = now.astimezone(zone).date()
    last = today + timedelta(days=CALENDAR_DAYS_AHEAD)
    allowed = frozenset(weekdays)

    # Each day's first event: (instant, timed before all-day at the same minute, order read, event).
    firsts: dict[date, tuple[datetime, int, int, CalendarEvent]] = {}
    for order, event in enumerate(events):
        if event.all_day:
            day = event.start if isinstance(event.start, date) else None
            assert day is not None
            instant = local_instant(day, daily, zone)
        else:
            assert isinstance(event.start, datetime)
            local = event.start.astimezone(zone)
            day = local.date()
            # Minute precision, as every departure: an event at 07:30:30 leaves at 07:30.
            instant = local_instant(day, time(local.hour, local.minute), zone)
        if not today <= day <= last:
            continue
        item = (instant, 1 if event.all_day else 0, order, event)
        known = firsts.get(day)
        if known is None or _first_key(item) < _first_key(known):
            firsts[day] = item
    event_days = set(firsts)
    # A day's first event that has gone leaves that day without a departure: its later events are not ones.
    candidates = [
        (instant, order, event)
        for instant, _all_day, order, event in firsts.values()
        if instant.astimezone(timezone.utc) > after
    ]

    daily_next: datetime | None = None  # the next daily departure as the planner sees it without a calendar
    daily_kept: datetime | None = None  # the next one on a day without an event
    for offset in range(0 if calendar_only else CALENDAR_DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        if day.isoweekday() not in allowed:
            continue
        instant = local_instant(day, daily, zone)
        if instant.astimezone(timezone.utc) <= after:
            continue
        if daily_next is None:
            daily_next = instant
        if day not in event_days:
            daily_kept = instant
            break

    best: tuple[datetime, int, CalendarEvent] | None = min(
        candidates, key=lambda item: (item[0].astimezone(timezone.utc), item[1]), default=None
    )
    if best is not None and (daily_kept is None or best[0].astimezone(timezone.utc) <= daily_kept.astimezone(timezone.utc)):
        instant, _, event = best
        local = instant.astimezone(zone)
        text = f"{event.summary}\n{event.description}"
        return CalendarDeparture(
            day=local.date(),
            at=time(local.hour, local.minute),
            instant=instant,
            event=event,
            target=parse_target(text),
        )
    if calendar_only or daily_kept is None or daily_kept == daily_next:
        # The calendar moves nothing (or, deciding alone, has no departure ahead): the daily departure stands as the planner already finds it.
        return None
    local = daily_kept.astimezone(zone)
    return CalendarDeparture(day=local.date(), at=daily, instant=daily_kept, event=None, target=None)
