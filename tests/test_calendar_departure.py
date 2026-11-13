"""The departure from a calendar, as pure rules (`planning/calendar_departure.py`).

* A day's first event replaces the daily departure on its day (later ones that day are not departures); a day
  without one keeps the daily departure on its weekdays, unless the calendar alone decides; the plan is for the
  earliest that is still ahead.
* All-day: a departure at the daily time on its first day. Multi-day: its start only. Begun: gone.
* Time zones: an event's instant is read in the area's zone.
* The target in an event's text: a level or an amount, never a number that is part of something else.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from custom_components.spotnav.planning.calendar_departure import (
    CalendarEvent,
    EventTarget,
    calendar_departure,
    events_from_response,
    parse_target,
)

TZ = "Europe/Stockholm"
#: Thursday 2026-10-01, 20:00 in Stockholm (CEST, UTC+2).
THURSDAY_EVENING = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
EVERY_DAY = (1, 2, 3, 4, 5, 6, 7)
CEST = timezone(timedelta(hours=2))


def timed(day: int, hour: int, minute: int = 0, summary: str = "", *, zone: timezone = CEST) -> CalendarEvent:
    return CalendarEvent(start=datetime(2026, 10, day, hour, minute, tzinfo=zone), summary=summary)


def plan(*events: CalendarEvent, weekdays=EVERY_DAY, daily: time = time(8, 0), now: datetime = THURSDAY_EVENING):
    return calendar_departure(events, now=now, tz=TZ, daily=daily, weekdays=weekdays)


# ------------------------------------------------------------------------------- which departure


def test_no_events_change_nothing() -> None:
    assert plan() is None


def test_an_event_later_than_the_daily_departure_on_its_day_replaces_it() -> None:
    """Friday's event at 10:00 is Friday's departure: the daily 08:00 is not."""
    found = plan(timed(2, 10, summary="Dentist"))
    assert found is not None
    assert (found.day, found.at) == (date(2026, 10, 2), time(10, 0))
    assert found.event is not None and found.event.summary == "Dentist"


def test_an_event_earlier_than_the_daily_departure_replaces_it() -> None:
    found = plan(timed(2, 5, 30))
    assert found is not None and (found.day, found.at) == (date(2026, 10, 2), time(5, 30))


def test_the_next_daily_departure_comes_first_when_the_event_is_on_a_later_day() -> None:
    """Friday has no event, so Friday 08:00 stands: nothing to change."""
    assert plan(timed(3, 10)) is None


def test_an_event_on_a_day_outside_the_weekdays_is_a_departure() -> None:
    """Only Monday is a daily departure; Saturday's event comes first."""
    found = plan(timed(3, 10), weekdays=(1,))
    assert found is not None and (found.day, found.at) == (date(2026, 10, 3), time(10, 0))


def test_only_the_first_event_of_a_day_is_its_departure() -> None:
    """Friday has events at 17:00 and 08:00 (read in that order): 08:00 is Friday's departure."""
    found = plan(timed(2, 17, summary="Gym"), timed(2, 8, summary="Work"))
    assert found is not None and found.at == time(8, 0) and found.event is not None
    assert found.event.summary == "Work"


def test_a_later_event_on_a_day_whose_first_has_gone_is_no_departure() -> None:
    """At 11:00 on Friday the 08:00 event has gone; the 17:00 one is not a departure, so Saturday's daily 08:00 is
    next, which the planner finds by itself."""
    now = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    assert plan(timed(2, 8), timed(2, 17), now=now) is None


def test_a_timed_event_earlier_than_an_all_day_one_is_the_days_first() -> None:
    found = plan(CalendarEvent(start=date(2026, 10, 2), summary="Trip 100%"), timed(2, 6, summary="Early"))
    assert found is not None and found.at == time(6, 0) and found.target is None


def test_an_all_day_event_is_the_first_before_a_later_timed_one() -> None:
    found = plan(timed(2, 12, summary="Lunch 50%"), CalendarEvent(start=date(2026, 10, 2), summary="Trip 100%"))
    assert found is not None and found.at == time(8, 0) and found.target == EventTarget(percent=100.0)


def test_at_the_same_minute_the_timed_event_is_the_first() -> None:
    found = plan(CalendarEvent(start=date(2026, 10, 2), summary="Trip 100%"), timed(2, 8, summary="Work 60%"))
    assert found is not None and found.target == EventTarget(percent=60.0)


def test_a_day_whose_event_has_gone_has_no_daily_departure_left() -> None:
    """Friday 07:30: the 06:00 event was Friday's departure, so the daily 08:00 is not; Saturday 08:00 is next,
    which is not what the planner finds by itself, so the calendar says it."""
    now = datetime(2026, 10, 2, 5, 30, tzinfo=timezone.utc)
    found = plan(timed(2, 6), now=now)
    assert found is not None
    assert found.event is None and (found.day, found.at) == (date(2026, 10, 3), time(8, 0))


def test_an_event_that_begins_before_the_first_slot_is_gone() -> None:
    """20:05 now: an event at 20:10 is inside the first slot (20:15), so it cannot be planned for."""
    now = datetime(2026, 10, 1, 18, 5, tzinfo=timezone.utc)
    assert plan(timed(1, 20, 10), now=now) is None


def test_an_all_day_event_is_a_departure_at_the_daily_time_on_its_first_day() -> None:
    found = plan(CalendarEvent(start=date(2026, 10, 3), summary="Göteborg"), weekdays=(1,))
    assert found is not None and (found.day, found.at) == (date(2026, 10, 3), time(8, 0))
    assert found.event is not None and found.event.all_day


def test_an_all_day_event_on_a_departure_day_keeps_the_time_and_brings_its_target() -> None:
    found = plan(CalendarEvent(start=date(2026, 10, 2), summary="Long trip 100%"))
    assert found is not None and (found.day, found.at) == (date(2026, 10, 2), time(8, 0))
    assert found.target == EventTarget(percent=100.0)


def test_a_multi_day_event_that_began_before_today_is_on_no_day_here() -> None:
    """The trip began Tuesday and runs on: its start has gone, so Friday's daily departure stands."""
    started = CalendarEvent(start=datetime(2026, 9, 29, 9, 0, tzinfo=CEST))
    assert plan(started) is None


def test_an_event_past_the_seven_days_is_not_read() -> None:
    assert plan(timed(9, 10), weekdays=(1,)) is None


def test_an_event_in_another_zone_is_read_in_the_areas_zone() -> None:
    """09:00 in London is 10:00 in Stockholm."""
    found = plan(timed(2, 9, zone=timezone(timedelta(hours=1))))
    assert found is not None and found.at == time(10, 0)


def test_an_event_is_kept_to_the_minute() -> None:
    event = CalendarEvent(start=datetime(2026, 10, 2, 10, 15, 40, tzinfo=CEST))
    found = plan(event)
    assert found is not None and found.at == time(10, 15)


def test_the_target_of_the_departure_event_is_read() -> None:
    found = plan(CalendarEvent(start=datetime(2026, 10, 2, 10, 0, tzinfo=CEST), summary="Stockholm", description="mål: 90"))
    assert found is not None and found.target == EventTarget(percent=90.0)


def test_an_unknown_zone_plans_nothing() -> None:
    assert calendar_departure([timed(2, 10)], now=THURSDAY_EVENING, tz="Nowhere/Else", daily=time(8, 0), weekdays=EVERY_DAY) is None


# ------------------------------------------------------------------------------- the target


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mål: 80", EventTarget(percent=80.0)),
        ("Mål 80 %", EventTarget(percent=80.0)),
        ("target: 80 %", EventTarget(percent=80.0)),
        ("Ziel=90", EventTarget(percent=90.0)),
        ("Resa till Malmö 80%", EventTarget(percent=80.0)),
        ("30 kWh", EventTarget(kwh=30.0)),
        ("target: 30 kWh", EventTarget(kwh=30.0)),
        ("12,5 kWh", EventTarget(kwh=12.5)),
        ("mål 80, 25 kWh", EventTarget(percent=80.0, kwh=25.0)),
        ("Göteborg", None),
        ("", None),
        ("1000%", None),
        ("0 %", None),
        ("odometer 12345 kWh", None),
        ("500 kWh", None),
        ("Room 101", None),
    ],
)
def test_a_target_in_the_text(text: str, expected: EventTarget | None) -> None:
    assert parse_target(text) == expected


# ------------------------------------------------------------------------------- reading the answer


def test_the_answer_is_read_leniently() -> None:
    events = events_from_response(
        {
            "events": [
                {"start": "2026-10-02T10:00:00+02:00", "end": "2026-10-02T11:00:00+02:00", "summary": "Trip"},
                {"start": "2026-10-03", "end": "2026-10-04", "summary": "All day", "description": "80%"},
                {"start": "2026-10-02T10:00:00", "summary": "naive: left out"},
                {"start": "not a date"},
                {"summary": "no start"},
                "not an event",
            ]
        }
    )
    assert [event.summary for event in events] == ["Trip", "All day"]
    assert events[1].all_day and events[1].description == "80%"


@pytest.mark.parametrize("raw", [None, "events", {"events": None}, {"events": "x"}, 42])
def test_an_answer_that_is_not_one_reads_as_no_events(raw: object) -> None:
    assert events_from_response(raw) == ()


# ------------------------------------------------------------------------------- the calendar alone decides


def only(*events: CalendarEvent, now: datetime = THURSDAY_EVENING, weekdays=EVERY_DAY):
    return calendar_departure(events, now=now, tz=TZ, daily=time(8, 0), weekdays=weekdays, calendar_only=True)


def test_alone_the_calendar_skips_days_without_an_event() -> None:
    """Friday has no event: no departure Friday; Sunday's event is the next."""
    found = only(timed(4, 9, summary="Mormor"))
    assert found is not None and (found.day, found.at) == (date(2026, 10, 4), time(9, 0))


def test_alone_the_calendar_has_no_departure_without_an_event_ahead() -> None:
    assert only() is None
    # The first event of a day that has gone leaves nothing either.
    now = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    assert only(timed(2, 8), timed(2, 17), now=now) is None


def test_alone_the_calendar_still_takes_an_all_day_event_at_the_daily_time() -> None:
    found = only(CalendarEvent(start=date(2026, 10, 3)), weekdays=(1,))
    assert found is not None and (found.day, found.at) == (date(2026, 10, 3), time(8, 0))
