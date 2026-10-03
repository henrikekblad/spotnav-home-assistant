"""Sessions summed per local day and month: month and DST boundaries, savings, solar, CSV."""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from custom_components.spotnav.sessions.model import SOURCE_ESTIMATED
from custom_components.spotnav.sessions.summary import (
    month_summary,
    sessions_csv,
    sessions_summary,
    summarize,
)

from .sessions_helpers import session, STOCKHOLM, UTC


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=STOCKHOLM).astimezone(UTC)


def test_a_day_sums_its_sessions_and_says_what_it_cost_on_average() -> None:
    sessions = [
        session(local(2026, 9, 22, 1), energy=10, cost_minor=400),
        session(local(2026, 9, 22, 14), energy=5, cost_minor=500),
    ]

    (day,) = summarize(sessions, STOCKHOLM, by="day")

    assert day["period"] == "2026-09-22"
    assert day["sessions"] == 2
    assert day["energy_kwh"] == pytest.approx(15)
    assert day["cost"] == pytest.approx(9.0)
    assert day["average_price_minor_per_kwh"] == pytest.approx(60.0)
    assert day["currency"] == "SEK"
    assert day["estimated"] is False


def test_a_session_counts_on_the_local_day_it_started_not_the_utc_day() -> None:
    """00:10 local on 1 October is still 30 September in UTC; it belongs to October."""
    sessions = [
        session(local(2026, 9, 30, 23, 30)),
        session(local(2026, 10, 1, 0, 10)),
    ]
    assert sessions[1].start.date() == date(2026, 9, 30)

    months = summarize(sessions, STOCKHOLM, by="month")

    assert [(m["period"], m["sessions"]) for m in months] == [("2026-10", 1), ("2026-09", 1)]


def test_a_session_across_midnight_is_one_row_on_the_day_it_started() -> None:
    sessions = [session(local(2026, 9, 22, 23, 0), hours=3)]

    days = summarize(sessions, STOCKHOLM, by="day")

    assert [(d["period"], d["sessions"]) for d in days] == [("2026-09-22", 1)]


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_the_dst_changeover_days_are_ordinary_days(day: date) -> None:
    """Spring has 23 hours and autumn 25; sessions either side of the change land on that one date."""
    first = session(local(day.year, day.month, day.day, 0, 30))
    last = session(local(day.year, day.month, day.day, 23, 30))
    before = session(local(day.year, day.month, day.day - 1, 23, 30))
    after = session(local(day.year, day.month, day.day + 1, 0, 10))

    days = {row["period"]: row["sessions"] for row in summarize([first, last, before, after], STOCKHOLM, by="day")}

    assert days == {
        day.isoformat(): 2,
        (day - timedelta(days=1)).isoformat(): 1,
        (day + timedelta(days=1)).isoformat(): 1,
    }


def test_two_sessions_the_autumn_night_with_the_same_wall_clock_hour_stay_two() -> None:
    first_two = datetime(2026, 10, 25, 2, 30, fold=0, tzinfo=STOCKHOLM).astimezone(UTC)
    second_two = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=STOCKHOLM).astimezone(UTC)
    assert second_two - first_two == timedelta(hours=1)

    (day,) = summarize([session(first_two), session(second_two)], STOCKHOLM, by="day")

    assert day["sessions"] == 2 and day["period"] == "2026-10-25"


def test_this_month_and_last_month_across_a_year_boundary() -> None:
    sessions = [
        session(local(2025, 12, 31, 22), energy=7, cost_minor=100),
        session(local(2026, 1, 1, 8), energy=3, cost_minor=50),
        session(local(2025, 11, 30, 12), energy=99, cost_minor=999),
    ]
    now = local(2026, 1, 15, 12)

    block = sessions_summary(sessions, STOCKHOLM, now)

    assert block["this_month"]["period"] == "2026-01"
    assert block["this_month"]["energy_kwh"] == pytest.approx(3)
    assert block["last_month"]["period"] == "2025-12"
    assert block["last_month"]["energy_kwh"] == pytest.approx(7)
    assert month_summary(sessions, STOCKHOLM, "2026-02")["sessions"] == 0
    assert month_summary(sessions, STOCKHOLM, "2026-02")["cost"] is None


def test_the_savings_compare_with_the_days_average_and_say_they_are_an_estimate() -> None:
    cheap = session(local(2026, 9, 22, 2), energy=10, cost_minor=200, reference_minor=400)
    dear = session(local(2026, 9, 22, 15), energy=10, cost_minor=500, reference_minor=400)

    (day,) = summarize([cheap, dear], STOCKHOLM, by="day")

    assert day["reference_cost"] == pytest.approx(8.0)
    assert day["savings"] == pytest.approx(1.0)
    assert day["savings_estimate"] is True
    assert day["savings_basis"] == "day_average_price"


def test_energy_without_a_price_is_counted_but_never_costed() -> None:
    priced = session(local(2026, 9, 22, 2), energy=10, cost_minor=300)
    unpriced = session(local(2026, 9, 22, 15), energy=4, cost_minor=None, currency=None)

    (day,) = summarize([priced, unpriced], STOCKHOLM, by="day")

    assert day["energy_kwh"] == pytest.approx(14)
    assert day["cost"] == pytest.approx(3.0)
    assert day["average_price_minor_per_kwh"] == pytest.approx(30.0)
    only_unpriced = summarize([unpriced], STOCKHOLM, by="day")[0]
    assert only_unpriced["cost"] is None and only_unpriced["savings"] is None


def test_a_change_of_market_never_adds_two_currencies() -> None:
    old = session(local(2026, 9, 1, 2), energy=10, cost_minor=300, currency="SEK")
    new = session(local(2026, 9, 20, 2), energy=10, cost_minor=100, currency="EUR")

    (month,) = summarize([old, new], STOCKHOLM, by="month")

    assert month["currency"] == "EUR"
    assert month["cost"] == pytest.approx(1.0)
    assert month["energy_kwh"] == pytest.approx(20)


def test_solar_share_is_over_the_energy_the_executor_could_judge_and_absent_otherwise() -> None:
    a = session(local(2026, 9, 22, 11), energy=10, solar=(8.0, 10.0))
    b = session(local(2026, 9, 22, 13), energy=10, solar=(0.0, 0.0))
    c = session(local(2026, 9, 22, 18), energy=10)

    assert summarize([a, b], STOCKHOLM, by="day")[0]["solar_share"] == pytest.approx(0.8)
    assert summarize([c], STOCKHOLM, by="day")[0]["solar_share"] is None


def test_an_estimated_session_marks_its_bucket() -> None:
    estimated = session(local(2026, 9, 22, 2), source=SOURCE_ESTIMATED)

    assert summarize([estimated], STOCKHOLM, by="day")[0]["estimated"] is True


def test_an_open_session_is_not_in_a_summary() -> None:
    open_one = replace(session(local(2026, 9, 22, 2)), end=None)

    assert summarize([open_one], STOCKHOLM, by="day") == []


# ------------------------------------------------------------------------------ CSV


def parse(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def test_the_csv_has_one_row_per_session_in_the_range_oldest_first_in_local_time() -> None:
    sessions = [
        session(local(2026, 9, 24, 2), energy=3),
        session(local(2026, 9, 22, 2), energy=1),
        session(local(2026, 9, 23, 2), energy=2),
        session(local(2026, 10, 30, 2), energy=9),
    ]

    rows = parse(sessions_csv(sessions, STOCKHOLM, first=date(2026, 9, 22), last=date(2026, 9, 23)))

    assert [row["energy_kwh"] for row in rows] == ["1", "2"]
    assert rows[0]["start"] == "2026-09-22T02:00:00+02:00"
    assert rows[0]["currency"] == "SEK" and rows[0]["energy_source"] == "register"
    assert list(rows[0]) == [
        "start", "end", "energy_kwh", "energy_source", "estimated", "cost", "currency",
        "average_price_minor_per_kwh", "started_by", "strategy", "vehicle", "solar_share",
        "reference_cost", "savings",
    ]


def test_an_open_range_includes_everything_and_an_empty_one_is_just_the_header() -> None:
    sessions = [session(local(2026, 9, 22, 2))]

    assert len(parse(sessions_csv(sessions, STOCKHOLM, first=None, last=None))) == 1
    empty = sessions_csv(sessions, STOCKHOLM, first=date(2027, 1, 1), last=None)
    assert empty.count("\n") == 1


def test_a_vehicle_name_a_spreadsheet_would_run_as_a_formula_is_defused() -> None:
    risky = replace(session(local(2026, 9, 22, 2)), vehicle_name='=HYPERLINK("http://x")')

    rows = parse(sessions_csv([risky], STOCKHOLM, first=None, last=None))

    assert rows[0]["vehicle"].startswith("'=")
