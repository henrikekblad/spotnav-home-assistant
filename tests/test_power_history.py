"""The site's power history (`site/power_history.py`) and the hour tracker's import-target marks: day summaries kept
for two years, refiled from the hours as each completes, across a month's roll and the clock changes, and one
month's power view built from the hours or from the kept summaries. Pure: no Home Assistant."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.site.demand_tracker import HourTracker, RunningHour, SiteDemand, TargetMark
from custom_components.spotnav.site.power_history import (
    ARCHIVE_MONTHS,
    day_summaries,
    PowerArchive,
    power_month,
)

from .test_demand_charge import cet, PILOT, TZ

UTC = timezone.utc


def run(
    tracker: HourTracker, start: datetime, minutes: int, grid_w: float, *, car_w=None, target=None, step_s=60.0
) -> datetime:
    moment = start
    end = start + timedelta(minutes=minutes)
    while moment < end:
        tracker.observe(moment, grid_w, car_w, None, target)
        moment += timedelta(seconds=step_s)
    return moment


# ------------------------------------------------------------------------------------------- the target's marks


def test_an_hour_carries_whether_the_import_target_held_or_paused_a_charger_in_it() -> None:
    tracker = HourTracker(tz=TZ)
    first = cet(2026, 11, 2, 18)
    second = first + timedelta(hours=1)
    # Held for a few minutes of the first hour; a mark for another hour (the last decision's, late) marks nothing.
    moment = run(tracker, first, 10, 6000.0)
    moment = run(tracker, moment, 5, 6000.0, target=TargetMark(hour_start=first, held=True, paused=False))
    moment = run(tracker, moment, 45, 6000.0, target=TargetMark(hour_start=first - timedelta(hours=1), held=True, paused=True))
    # Paused in the second hour.
    moment = run(tracker, moment, 60, 2000.0, target=TargetMark(hour_start=second, held=False, paused=True))
    tracker.observe(moment, 2000.0, None)
    assert [(hour.held, hour.paused) for hour in tracker.hours] == [(True, False), (False, True)]


def test_the_marks_are_kept_across_a_restart_and_rows_without_them_still_read() -> None:
    tracker = HourTracker(tz=TZ)
    start = cet(2026, 11, 2, 18)
    moment = run(tracker, start, 20, 5000.0, target=TargetMark(hour_start=start, held=False, paused=True))
    restored = RunningHour.from_dict(tracker.running.as_dict())  # type: ignore[union-attr]
    assert restored is not None and restored.paused and not restored.held
    moment = run(tracker, moment, 41, 5000.0)
    stored = tracker.month_dict()
    assert stored["hours"][0][4:6] == [0, 1]
    # The hour's floor: its import over the whole hour (here measured whole, so the same as its mean).
    assert stored["hours"][0][6] == pytest.approx(5.0, abs=0.01)
    again = HourTracker(tz=TZ)
    old_row = [cet(2026, 11, 2, 17).isoformat(), 3.0, None, 0]
    again.restore({**stored, "hours": [old_row, *stored["hours"], [*old_row[:4], 1, 2]]}, None)
    assert [(hour.kw, hour.held, hour.paused) for hour in again.hours] == [(3.0, False, False), (5.0, False, True)]


# ------------------------------------------------------------------------------------------------ day summaries


def hour(start: datetime, kw: float, house: float | None = None, **flags: bool) -> HourMean:
    return HourMean(start=start, kw=kw, house_kw=house, **flags)


def test_a_day_is_its_highest_complete_hour_with_its_house_part_and_any_mark() -> None:
    hours = [
        hour(cet(2026, 11, 2, 7), 3.0, 2.0),
        hour(cet(2026, 11, 2, 18), 6.5, 2.5, held=True),
        hour(cet(2026, 11, 2, 19), 9.0, incomplete=True),
        hour(cet(2026, 11, 2, 23), 4.0, paused=True),
        # Only an incomplete hour on the 3rd: it is the day's bar all the same.
        hour(cet(2026, 11, 3, 1), 2.0, incomplete=True),
    ]
    first, second = day_summaries(hours, TZ)
    assert (first.day, first.peak_kw, first.peak_start, first.house_kw) == (date(2026, 11, 2), 6.5, cet(2026, 11, 2, 18), 2.5)
    assert (first.held, first.paused) == (True, True)
    assert (second.day, second.peak_kw, second.held, second.paused) == (date(2026, 11, 3), 2.0, False, False)


def test_the_days_follow_the_areas_clock_across_both_clock_changes() -> None:
    # 29 March 2026 has 23 hours, 25 October 2026 has 25 (the repeated 02:00 twice).
    spring = [hour(datetime(2026, 3, 28, 23, tzinfo=UTC) + timedelta(hours=index), 1.0 + index / 10) for index in range(23)]
    autumn = [hour(datetime(2026, 10, 24, 22, tzinfo=UTC) + timedelta(hours=index), 1.0) for index in range(25)]
    autumn[3] = hour(autumn[3].start, 5.0)  # the second 02:00
    (spring_day,) = day_summaries(spring, TZ)
    assert spring_day.day == date(2026, 3, 29) and spring_day.peak_kw == pytest.approx(3.2)
    (autumn_day,) = day_summaries(autumn, TZ)
    assert autumn_day.day == date(2026, 10, 25) and autumn_day.peak_start == datetime(2026, 10, 25, 1, tzinfo=UTC)
    view = power_month(
        (2026, 10),
        tracker_months={(2026, 10): autumn},
        archive=PowerArchive(),
        components=(),
        tz=TZ,
        now=datetime(2026, 10, 26, 12, tzinfo=UTC),
        import_target_kw=None,
    )
    starts = [row["start"] for row in view["hours"]]
    assert len(starts) == 25 and starts[2:4] == ["2026-10-25T02:00:00+02:00", "2026-10-25T02:00:00+01:00"]
    assert view["days"][0]["peak_start"] == "2026-10-25T02:00:00+01:00"


# ---------------------------------------------------------------------------------------- the archive over time


def site(now: list[datetime]) -> SiteDemand:
    return SiteDemand(None, None, TZ, now=lambda: now[0])


def feed(demand: SiteDemand, now: list[datetime], until: datetime, grid_w: float, *, components=PILOT) -> None:
    while now[0] < until:
        demand.observe(grid_w, None, TZ, components=lambda: components)
        now[0] += timedelta(seconds=60)


def test_the_days_are_filed_as_each_hour_completes_and_outlive_the_months_hours() -> None:
    cest = timedelta(hours=2)
    now = [datetime(2026, 9, 30, 21, tzinfo=UTC) - cest]
    demand = site(now)
    feed(demand, now, datetime(2026, 9, 30, 23, tzinfo=UTC) - cest, 4000.0)  # 21:00-23:00 on the 30th
    archive = demand.archive.months
    assert [day.day for day in archive[(2026, 9)].days] == [date(2026, 9, 30)]
    # The day component (06-23) counted 21:00 (the first of two equal hours), the night component (23-06) nothing.
    assert archive[(2026, 9)].counted == ((datetime(2026, 9, 30, 21, tzinfo=UTC) - cest,), ())
    # Into October: the night's 23:00 is September's, its later hours October's (under the 30th for the night).
    feed(demand, now, datetime(2026, 10, 1, 2, tzinfo=UTC) - cest, 3000.0)
    assert set(archive) == {(2026, 9), (2026, 10)}
    assert [day.day for day in archive[(2026, 10)].days] == [date(2026, 10, 1)]
    assert archive[(2026, 10)].counted[1] == (datetime(2026, 10, 1, 0, tzinfo=UTC) - cest,)
    # Two months on, September's hours are gone from the tracker but its days are kept.
    now[0] = cet(2026, 11, 30, 23)
    feed(demand, now, cet(2026, 12, 1, 2), 1000.0)
    assert demand.tracker.previous_month == (2026, 11) and demand.tracker.month == (2026, 12)
    assert (2026, 9) in archive and (2026, 10) in archive
    view = demand.power_month((2026, 9), (), import_target_kw=6.0)
    assert view["hours"] is None
    assert [day["date"] for day in view["days"]] == ["2026-09-30"]
    assert view["days"][0]["counted"] == [0, 1]
    # The month keeps the components it was measured against, whatever is set now.
    assert view["components"] == [component.as_dict() for component in PILOT]
    assert view["bars"] == [pytest.approx(4.0), pytest.approx(3.0)]
    assert view["available_months"] == ["2026-12", "2026-11", "2026-10", "2026-09"]


def test_months_older_than_two_years_are_dropped() -> None:
    archive = PowerArchive()
    for months_ago in range(ARCHIVE_MONTHS + 3):
        index = 2026 * 12 + 11 - 1 - months_ago
        month = (index // 12, index % 12 + 1)
        archive.file_month(month, [hour(datetime(month[0], month[1], 10, 12, tzinfo=UTC), 1.0)], (), TZ, cet(2026, 11, 2, 12))
    assert archive.prune((2026, 11))
    assert min(archive.months) == (2024, 11) and len(archive.months) == ARCHIVE_MONTHS + 1


def test_the_kept_days_are_read_back_and_bad_records_are_dropped() -> None:
    archive = PowerArchive()
    hours = [hour(cet(2026, 11, 2, 18), 6.5, 2.5, held=True), hour(cet(2026, 11, 3, 2), 4.0, paused=True)]
    archive.file_month((2026, 11), hours, PILOT, TZ, cet(2026, 11, 3, 12))
    stored = archive.as_dict()
    again = PowerArchive()
    again.restore(stored)
    assert again.months == archive.months
    broken = {
        "months": {
            "2026-11": {**stored["months"]["2026-11"], "components": [{"top_n": 2}], "days": [["x"], *stored["months"]["2026-11"]["days"]]},
            "2026-13": stored["months"]["2026-11"],
            "2026-10": "junk",
        }
    }
    third = PowerArchive()
    third.restore(broken)
    assert list(third.months) == [(2026, 11)]
    assert third.months[(2026, 11)].days == archive.months[(2026, 11)].days
    assert third.months[(2026, 11)].components == ()
    PowerArchive().restore("junk")


# ------------------------------------------------------------------------------------------------- the month view


def test_the_month_view_marks_each_components_counted_peaks_on_their_days_and_hours() -> None:
    hours = [
        hour(cet(2026, 11, 2, 18), 6.5, 2.5, held=True),
        hour(cet(2026, 11, 3, 2), 4.0, 1.0, paused=True),
        hour(cet(2026, 11, 3, 12), 5.0, 1.5),
        hour(cet(2026, 11, 4, 3), 3.0, 1.0),
    ]
    view = power_month(
        (2026, 11),
        tracker_months={(2026, 11): hours, (2026, 10): []},
        archive=PowerArchive(),
        components=PILOT,
        tz=TZ,
        now=cet(2026, 11, 4, 12),
        import_target_kw=6.0,
    )
    assert view["month"] == "2026-11" and view["unit"] == "kW" and view["import_target_kw"] == 6.0
    assert view["available_months"] == ["2026-11"]
    assert [(day["date"], day["peak_kw"], day["counted"], day["held"], day["paused"]) for day in view["days"]] == [
        ("2026-11-02", 6.5, [0], True, False),
        # The night of the 2nd peaks at 02:00 on the 3rd: that day carries the night component's mark.
        ("2026-11-03", 5.0, [1], False, True),
        ("2026-11-04", 3.0, [], False, False),
    ]
    assert view["days"][0]["peak_start"] == "2026-11-02T18:00:00+01:00" and view["days"][0]["house_kw"] == 2.5
    assert [row["counted"] for row in view["hours"]] == [[0], [1], [], []]
    assert view["hours"][1] == {
        "start": "2026-11-03T02:00:00+01:00",
        "kw": 4.0,
        "house_kw": 1.0,
        "incomplete": False,
        "held": False,
        "paused": True,
        "counted": [1],
    }
    assert view["bars"] == [6.5, 4.0]


def test_a_month_with_nothing_kept_is_empty() -> None:
    view = power_month(
        (2025, 1),
        tracker_months={},
        archive=PowerArchive(),
        components=PILOT,
        tz=TZ,
        now=cet(2026, 11, 4, 12),
        import_target_kw=None,
    )
    assert (view["days"], view["hours"], view["components"], view["bars"], view["available_months"]) == ([], None, [], [], [])
