"""A departure's window in absolute time: clock-change nights and departures just after midnight.

Stockholm's 2026 transitions: 2026-03-29 02:00+01:00 -> 03:00+02:00 (a 23-hour day) and
2026-10-25 03:00+02:00 -> 02:00+01:00 (a 25-hour day). The latest start and the end of the
required prices are elapsed-time questions; on a wall clock they move by an hour on those nights,
and the end must never require a slot starting at or after the departure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from custom_components.spotnav.planning.planner import (
    PlanRequest,
    PlanResult,
    calculate_plan,
    energy_per_slot_kwh,
    price_gap,
)

from .planning import build_request, parse_documents

STEP = timedelta(minutes=15)
#: Three phases at 16 A: 2.76 kWh a quarter-hour.
PER_SLOT_KWH = energy_per_slot_kwh(16, 3)


def day(date: str, count: int, start: str, expensive: range = range(0)) -> dict[str, Any]:
    """One 15-minute day document: flat 1.0 EUR/kWh, with `expensive` indexes at 9.0."""
    return {
        "area": "SE4",
        "date": date,
        "tz": "Europe/Stockholm",
        "res": 15,
        "start": start,
        "unit": "EUR/kWh",
        "prices": [9.0 if index in expensive else 1.0 for index in range(count)],
    }


def request(now: str, departure: str, slots: int, documents: list[dict[str, Any]]) -> PlanRequest:
    """A daily-departure request needing exactly `slots` quarter-hours."""
    return build_request(
        {
            "area_id": "SE4",
            "timezone": "Europe/Stockholm",
            "currency": "EUR",
            "now": now,
            "phases": 3,
            "amps": 16,
            "requested_kwh": slots * PER_SLOT_KWH - 0.01,
            "consumption_kwh_per_10km": 2.0,
            "max_periods": 2,
            "departure": departure,
        },
        parse_documents(documents),
    )


def utc_starts(result: PlanResult) -> list[datetime]:
    return [slot.start.astimezone(timezone.utc) for slot in result.slots]


def every_slot(first: datetime, end: datetime) -> list[datetime]:
    out = []
    while first < end:
        out.append(first)
        first += STEP
    return out


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


# Spring: 01:30+01:00 (00:30Z) to a 06:00+02:00 departure (04:00Z) is 3.5 elapsed hours, 14 slots.
SPRING_NOW = "2026-03-29T01:30:00+01:00"
SPRING_DEPARTURE = utc("2026-03-29T06:00:00+02:00")
# The document starts at 23:00Z; indexes 8..11 are 01:00Z..02:00Z (03:00..04:00+02:00 local), priced high.
SPRING = [day("2026-03-29", 92, "2026-03-29T00:00:00+01:00", expensive=range(8, 12))]


def test_a_spring_need_that_does_not_fit_charges_every_slot_until_the_departure() -> None:
    result = calculate_plan(request(SPRING_NOW, "06:00", 16, SPRING))

    assert result.reason is None
    assert result.short_of_deadline is True
    assert utc_starts(result) == every_slot(utc(SPRING_NOW), SPRING_DEPARTURE)
    assert len(result.slots) == 14
    assert result.slots[-1].end.astimezone(timezone.utc) == SPRING_DEPARTURE


def test_a_spring_need_that_fits_is_the_cheapest_plan_before_the_departure() -> None:
    result = calculate_plan(request(SPRING_NOW, "06:00", 10, SPRING))

    assert result.reason is None
    assert result.short_of_deadline is False
    expensive = every_slot(utc("2026-03-29T01:00:00Z"), utc("2026-03-29T02:00:00Z"))
    assert utc_starts(result) == [
        moment for moment in every_slot(utc(SPRING_NOW), SPRING_DEPARTURE) if moment not in expensive
    ]
    assert all(slot.end <= SPRING_DEPARTURE for slot in result.slots)


def test_an_autumn_need_that_fits_is_the_cheapest_plan_and_no_shortfall() -> None:
    """00:00+02:00 (22:00Z) to 06:00+01:00 (05:00Z) is seven elapsed hours, 28 slots; 26 fit easily."""
    now = "2026-10-25T00:00:00+02:00"
    departure = utc("2026-10-25T06:00:00+01:00")
    # Indexes 8 and 9 are 02:00 and 02:15 (+02:00), 00:00Z and 00:15Z.
    documents = [day("2026-10-25", 100, "2026-10-25T00:00:00+02:00", expensive=range(8, 10))]
    result = calculate_plan(request(now, "06:00", 26, documents))

    assert result.reason is None
    assert result.short_of_deadline is False
    window = every_slot(utc(now), departure)
    assert len(window) == 28
    assert utc_starts(result) == [
        moment for moment in window if moment not in (utc("2026-10-25T00:00:00Z"), utc("2026-10-25T00:15:00Z"))
    ]
    assert all(slot.end.astimezone(timezone.utc) <= departure for slot in result.slots)


TODAY_ONLY = [day("2026-09-12", 96, "2026-09-12T00:00:00+02:00", expensive=range(80, 96))]


def test_a_midnight_departure_plans_with_todays_prices_before_tomorrow_is_published() -> None:
    for departure in ("00:00", "00:10"):
        planned = request("2026-09-12T18:00:00+02:00", departure, 4, TODAY_ONLY)
        result = calculate_plan(planned)
        midnight = utc("2026-09-13T00:00:00+02:00")

        assert result.reason is None, departure
        assert result.short_of_deadline is False
        assert price_gap(planned) is None, "no price at or after the departure is required"
        assert all(slot.end.astimezone(timezone.utc) <= midnight for slot in result.slots)
        # 20:00..24:00 local is expensive, so the cheapest four are the last ones before it.
        assert utc_starts(result) == every_slot(utc("2026-09-12T19:00:00+02:00"), utc("2026-09-12T20:00:00+02:00"))


def test_a_tight_departure_just_after_midnight_takes_every_slot_that_ends_before_it() -> None:
    """22:50 to 00:10: four whole slots (23:00..00:00) end before the departure, and five are needed."""
    result = calculate_plan(request("2026-09-12T22:50:00+02:00", "00:10", 5, TODAY_ONLY))

    assert result.reason is None
    assert result.short_of_deadline is True
    assert utc_starts(result) == every_slot(utc("2026-09-12T23:00:00+02:00"), utc("2026-09-13T00:00:00+02:00"))


def test_a_normal_day_is_unchanged() -> None:
    documents = [day("2026-09-12", 96, "2026-09-12T00:00:00+02:00", expensive=range(48, 56))]
    result = calculate_plan(request("2026-09-12T08:00:00+02:00", "18:00", 8, documents))

    assert result.reason is None
    assert result.short_of_deadline is False
    # Flat prices outside 12:00..14:00; equal costs take the latest slots before the departure.
    assert utc_starts(result) == every_slot(utc("2026-09-12T16:00:00+02:00"), utc("2026-09-12T18:00:00+02:00"))
