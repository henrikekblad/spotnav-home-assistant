"""The house load forecast's own history (`site/house_history.py`), pure: eight weeks of measured hours, merged from
the demand tracker, bounded, and read back from an untrusted store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
