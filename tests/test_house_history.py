"""The house load forecast's own history (`site/house_history.py`), pure: eight weeks of measured hours, merged from
the demand tracker, bounded, and read back from an untrusted store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.site.house_history import HouseHistory, KEEP_DAYS, MAX_ROWS
from custom_components.spotnav.site.load_forecast import forecast_house_load, WINDOW_DAYS

UTC = timezone.utc
NOW = datetime(2026, 11, 3, 10, 0, 5, tzinfo=UTC)
TZ = "Europe/Stockholm"


def hours(days: int, *, kw: float = 1.0, end: datetime = NOW) -> list[HourMean]:
    first = end.replace(minute=0, second=0, microsecond=0)
    return [
        HourMean(start=first - timedelta(hours=index), kw=kw, house_kw=kw, outdoor_c=4.0)
        for index in range(days * 24, 0, -1)
    ]


def test_only_hours_the_forecast_can_use_are_kept() -> None:
    history = HouseHistory()
    start = datetime(2026, 11, 3, 8, tzinfo=UTC)
    rows = [
        HourMean(start=start, kw=2.0, house_kw=1.5, outdoor_c=3.0),
        HourMean(start=start - timedelta(hours=1), kw=2.0, house_kw=None),
        HourMean(start=start - timedelta(hours=2), kw=2.0, house_kw=1.0, imported=True),
        HourMean(start=start - timedelta(hours=3), kw=2.0, house_kw=-0.5),
        HourMean(start=start - timedelta(hours=4), kw=2.0, house_kw=float("nan")),
    ]
    assert history.merge(rows, NOW) == 1
    assert [(hour.start, hour.house_kw, hour.outdoor_c) for hour in history.hours()] == [(start, 1.5, 3.0)]
    assert history.migrated_at == NOW


def test_the_tracker_s_months_are_taken_over_once_and_each_new_hour_added() -> None:
    history = HouseHistory()
    assert history.merge(hours(30), NOW) == 30 * 24
    first = history.migrated_at
    # The next refresh: the tracker still holds the same hours, and one more.
    later = NOW + timedelta(hours=1)
    assert history.merge(hours(30, end=later), later) == 1
    assert history.migrated_at == first
    # The tracker's figure wins for an hour both hold.
    changed = hours(1, kw=3.0, end=later)
    history.merge(changed, later)
    assert history.hours()[-1].house_kw == 3.0


def test_early_in_a_month_the_forecast_still_reads_six_weeks() -> None:
    """The tracker drops a month at the turn; the history keeps it, so the window stays full."""
    history = HouseHistory()
    history.merge(hours(50), NOW)
    tracker_now = hours(3)  # what a tracker holds on the third of a month after the month before was dropped
    history.merge(tracker_now, NOW)
    from_history = forecast_house_load(history.hours(), NOW, TZ)
    from_tracker = forecast_house_load(tracker_now, NOW, TZ)
    assert from_history.samples == WINDOW_DAYS * 24 - 1  # the hour exactly six weeks back started before the window
    assert from_tracker.samples == 3 * 24


def test_eight_weeks_are_kept_and_no_more() -> None:
    history = HouseHistory()
    history.merge(hours(70), NOW)
    kept = history.hours()
    assert len(kept) <= MAX_ROWS
    assert kept[0].start >= NOW - timedelta(days=KEEP_DAYS)
    assert len(kept) == KEEP_DAYS * 24 - 1  # the hour exactly 56 days back started before the bound


def test_the_store_round_trips_and_drops_what_it_cannot_read() -> None:
    history = HouseHistory()
    history.merge(hours(2), NOW)
    again = HouseHistory()
    again.restore(history.as_dict())
    assert again.hours() == history.hours()
    assert again.migrated_at == history.migrated_at
    broken = HouseHistory()
    broken.restore(
        {
            "migrated_at": "nonsense",
            "hours": [
                ["2026-11-03T08:00:00+00:00", 1.0, None],
                ["2026-11-03T07:00:00", 1.0, None],  # no zone
                ["2026-11-03T06:00:00+00:00", -1.0, None],
                ["2026-11-03T05:00:00+00:00", 1.0, "warm"],
                "junk",
                [1, 2],
            ],
        }
    )
    assert [(hour.start.hour, hour.outdoor_c) for hour in broken.hours()] == [(5, None), (8, None)]
    assert broken.migrated_at is None
    for raw in (None, [], {"hours": "x"}):
        empty = HouseHistory()
        empty.restore(raw)
        assert empty.hours() == []


def test_the_diagnostics_count_rather_than_list() -> None:
    history = HouseHistory()
    assert history.diagnostics()["hours"] == 0
    history.merge(hours(1), NOW)
    block = history.diagnostics()
    assert block["hours"] == 24 and block["with_temperature"] == 24
    assert block["first"] < block["last"]


# ---------------------------------------------------------------------------------------------- the whole load


from custom_components.spotnav.site.house_history import MIN_GROSS_HOURS, PRODUCTION_WAIT_H  # noqa: E402


def tracked(count: int, *, ex_battery: float = 0.5, end: datetime = NOW) -> list[HourMean]:
    first = end.replace(minute=0, second=0, microsecond=0)
    return [
        HourMean(start=first - timedelta(hours=index), kw=0.1, house_kw=0.1, ex_battery_kw=ex_battery, outdoor_c=2.0)
        for index in range(count, 0, -1)
    ]


def test_an_hour_of_the_whole_load_waits_for_its_production_and_never_counts_net_of_the_sun() -> None:
    history = HouseHistory()
    rows = tracked(3, ex_battery=-1.0)
    history.merge(rows, NOW)
    # The last hour is not compiled yet: it waits; the others have the sun added back.
    production = {rows[0].start: 2.5, rows[1].start: 1.25}
    assert history.merge_gross(rows, production, NOW) == 2
    assert history.gross == {rows[0].start: (1.5, True), rows[1].start: (0.25, True)}
    assert [hour.start for hour in history.waiting(rows, NOW)] == [rows[2].start]
    # A refresh later the statistics have it; a negative sum is no load.
    assert history.merge_gross(rows, {rows[2].start: 0.5}, NOW + timedelta(hours=1)) == 1
    assert history.gross[rows[2].start] == (0.0, True)
    # The whole load keeps the temperature the measured hour recorded.
    assert {hour.outdoor_c for hour in history.gross_hours()} == {2.0}
    # An hour older than the wait is left out for good.
    late = NOW + timedelta(hours=PRODUCTION_WAIT_H + 2)
    assert history.waiting(tracked(1, end=NOW), late) == []


def test_without_solar_statistics_the_hours_are_filed_as_they_are_and_marked() -> None:
    history = HouseHistory()
    rows = tracked(2, ex_battery=0.7)
    assert history.merge_gross(rows, None, NOW) == 2
    assert all(row == (0.7, False) for row in history.gross.values())
    assert history.gross_with_sun() is False
    # Imported hours and hours without the figure are never the whole load.
    history = HouseHistory()
    assert history.merge_gross([HourMean(start=NOW - timedelta(hours=1), kw=1.0, imported=True, ex_battery_kw=1.0)], {}, NOW) == 0
    assert history.merge_gross([HourMean(start=NOW - timedelta(hours=1), kw=1.0)], {}, NOW) == 0


def test_the_battery_plans_rows_are_taken_over_once_and_only_before_the_first_hour_filed() -> None:
    history = HouseHistory()
    rows = tracked(2, ex_battery=1.0)
    history.merge_gross(rows, {hour.start: 0.0 for hour in rows}, NOW)
    first = min(history.gross)
    plan_rows = [(first - timedelta(hours=index), 0.8) for index in range(MIN_GROSS_HOURS, -2, -1)]
    production = {start: 0.25 for start, _kw in plan_rows[1:]}
    taken = history.take_rows(plan_rows, production, NOW)
    # The oldest has no production figure; those at or after the first filed hour stay the tracker's.
    assert taken == MIN_GROSS_HOURS - 1
    assert history.gross[first] == (1.0, True)
    assert history.gross[first - timedelta(hours=1)] == (1.05, True)
    assert history.gross_migrated_at == NOW
    assert history.gross_ready(NOW) is True
    assert history.gross_flat_kw() == pytest.approx((22 * 1.05 + 2 * 1.0) / 24)


def test_the_whole_load_is_the_house_figure_only_from_enough_hours_in_the_window() -> None:
    history = HouseHistory()
    history.take_rows([(NOW - timedelta(hours=index), 1.0) for index in range(1, MIN_GROSS_HOURS)], None, NOW)
    assert history.gross_in_window(NOW) == MIN_GROSS_HOURS - 1 and not history.gross_ready(NOW)
    history.gross[NOW - timedelta(hours=MIN_GROSS_HOURS)] = (1.0, False)
    assert history.gross_ready(NOW)
    # Six weeks later the same hours are out of the forecast's window.
    assert not history.gross_ready(NOW + timedelta(days=WINDOW_DAYS + 1))


def test_the_whole_load_round_trips_in_the_store_beside_the_hours() -> None:
    history = HouseHistory()
    history.merge(hours(1), NOW)
    history.take_rows([(NOW - timedelta(hours=index), 1.0) for index in range(1, 5)], None, NOW)
    stored = history.as_dict()
    # Beside the hours: a build before the whole load reads the rest as it was.
    assert all(len(row) == 3 for row in stored["hours"]) and len(stored["gross"]) == 4
    again = HouseHistory()
    again.restore({**stored, "gross": [*stored["gross"], ["bad", 1.0, 1], [NOW.isoformat(), -1.0, 1], [NOW.isoformat(), 1.0, True]]})
    assert again.gross == history.gross and again.gross_migrated_at == history.gross_migrated_at
    assert again.hours() == history.hours()
