"""The outdoor temperatures kept for a year (`site/temperature_history.py`): the measured hours and their source, the
forecast temperatures as issued (day-ahead and last before the hour), pruning, the compact store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.site.temperature_history import (
    KEEP_DAYS,
    TemperatureHistory,
)

TZ = "Europe/Stockholm"
UTC = timezone.utc
NOW = datetime(2026, 11, 17, 9, 0, 5, tzinfo=UTC)  # 10:00 local
SENSOR = "sensor:sensor.bt1"
WEATHER = "weather:weather.forecast_home"


def hours(first: datetime, count: int, celsius: float | None = 3.04) -> list[HourMean]:
    return [HourMean(start=first + timedelta(hours=index), kw=1.0, outdoor_c=celsius) for index in range(count)]


def ahead(now: datetime, count: int, celsius: float) -> dict[datetime, float]:
    first = now.replace(minute=0, second=0, microsecond=0)
    return {first + timedelta(hours=index): celsius for index in range(count)}


# ---------------------------------------------------------------------------------------------- measured hours


def test_measured_hours_are_kept_rounded_and_only_with_a_temperature() -> None:
    kept = TemperatureHistory()
    start = datetime(2026, 11, 17, 6, tzinfo=UTC)
    assert kept.merge_measured([*hours(start, 3), *hours(start + timedelta(hours=3), 2, None)], NOW)
    assert len(kept.measured) == 3 and set(kept.measured.values()) == {30}
    # The same hours again: nothing changed, nothing to write.
    assert not kept.merge_measured(hours(start, 3), NOW)
    # The tracker's figure wins.
    assert kept.merge_measured(hours(start, 1, -1.26), NOW)
    assert sorted(kept.measured.values()) == [-13, 30, 30]


def test_the_source_is_a_change_marker_not_per_hour() -> None:
    kept = TemperatureHistory()
    start = datetime(2026, 11, 17, 6, tzinfo=UTC)
    assert kept.mark_source(WEATHER, start)
    assert not kept.mark_source(WEATHER, start + timedelta(hours=1))
    assert not kept.mark_source(None, start + timedelta(hours=2))
    assert kept.mark_source(SENSOR, start + timedelta(hours=3))
    assert [marker for _key, marker in kept.sources] == [WEATHER, SENSOR]
    # Back and forth within the same hour: the hour takes the newest, no duplicate marker.
    assert kept.mark_source(WEATHER, start + timedelta(hours=3, minutes=30))
    assert [marker for _key, marker in kept.sources] == [WEATHER]


def test_old_hours_and_markers_are_pruned_after_a_year() -> None:
    kept = TemperatureHistory()
    old = NOW - timedelta(days=KEEP_DAYS + 2)
    kept.mark_source(WEATHER, old)
    kept.mark_source(SENSOR, old + timedelta(days=1))
    kept.mark_source(WEATHER, NOW - timedelta(days=3))
    kept.merge_measured(hours(old, 24), old + timedelta(hours=25))
    assert len(kept.measured) == 24
    kept.merge_measured(hours(NOW - timedelta(hours=5), 4), NOW)
    assert len(kept.measured) == 4
    # The marker in force at the oldest hour kept stays (the sensor); the one before it goes.
    assert [marker for _key, marker in kept.sources] == [SENSOR, WEATHER]


# ---------------------------------------------------------------------------------------------- forecast as issued


def test_the_last_issue_before_each_hour_and_the_day_ahead_issue_are_logged() -> None:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(TZ)
    kept = TemperatureHistory()
    # 10:00 local: every hour not yet started is logged as the last issue; the running hour is not (it started).
    assert kept.record_forecast(ahead(NOW, 48, 2.0), WEATHER, NOW, TZ)
    assert len(kept.last) == 47 and kept.day_ahead == {}
    # 13:00 local: the next local day's hours are logged as the day-ahead issue, first wins.
    issue = NOW.astimezone(zone).replace(hour=13, minute=0, second=10).astimezone(UTC)
    assert kept.record_forecast(ahead(issue, 48, 1.0), WEATHER, issue, TZ)
    tomorrow = issue.astimezone(zone).date() + timedelta(days=1)
    assert len(kept.day_ahead) == 24 and set(kept.day_ahead.values()) == {10}
    assert all(
        datetime.fromtimestamp(key * 3600, UTC).astimezone(zone).date() == tomorrow for key in kept.day_ahead
    )
    later = issue + timedelta(hours=1)
    assert kept.record_forecast(ahead(later, 48, -4.0), WEATHER, later, TZ)
    # The day-ahead keeps the 13:00 issue; the last issue is overwritten for the hours not yet started.
    assert set(kept.day_ahead.values()) == {10}
    first_key = int(later.replace(minute=0, second=0).timestamp()) // 3600
    assert kept.last[first_key + 1] == -40
    assert kept.last[first_key] == 10  # started at the refresh: keeps the issue before it
    # The same forecast again changes nothing; no forecast changes nothing.
    assert not kept.record_forecast(ahead(later, 48, -4.0), WEATHER, later, TZ)
    assert not kept.record_forecast({}, WEATHER, later, TZ)
    assert [marker for _key, marker in kept.forecast_sources] == [WEATHER]


# ---------------------------------------------------------------------------------------------- storage


def test_the_store_is_one_compact_list_per_series_and_survives_a_restart() -> None:
    kept = TemperatureHistory()
    start = NOW - timedelta(hours=30)
    kept.mark_source(SENSOR, start)
    kept.merge_measured([*hours(start, 10), *hours(start + timedelta(hours=12), 5, -7.5)], NOW)
    kept.record_forecast(ahead(NOW, 48, 2.0), WEATHER, NOW, TZ)
    stored = kept.as_dict()
    assert stored["format"] == 1
    assert stored["first"] == start.replace(minute=0, second=0, microsecond=0).isoformat()
    assert stored["measured"][:10] == [30] * 10 and stored["measured"][10:12] == [None, None]
    assert stored["measured"][12:] == [-75] * 5
    assert stored["sources"] == [[stored["first"], SENSOR]]
    assert all(value is None or isinstance(value, int) for value in stored["last"])

    again = TemperatureHistory()
    again.restore(stored, NOW)
    assert again == kept

    # Restored an age later: what is older than a year is gone.
    later = TemperatureHistory()
    later.restore(stored, NOW + timedelta(days=KEEP_DAYS) - timedelta(hours=20))
    assert later.measured == {key: tenths for key, tenths in kept.measured.items() if tenths == -75}


def test_a_malformed_store_is_dropped_value_by_value() -> None:
    kept = TemperatureHistory()
    kept.restore(None, NOW)
    kept.restore({"format": 99, "first": NOW.isoformat(), "measured": [1]}, NOW)
    assert kept == TemperatureHistory()
    kept.restore(
        {
            "format": 1,
            "first": (NOW - timedelta(hours=3)).replace(minute=0, second=0).isoformat(),
            "measured": [10, "x", True, 99999, 12.5, -20],
            "last": "junk",
            "sources": [["bad", SENSOR], [NOW.isoformat(), ""], [NOW.isoformat(), WEATHER], ["2020-01-01", "x"]],
        },
        NOW,
    )
    assert sorted(kept.measured.values()) == [-20, 10]
    assert kept.last == {}
    assert [marker for _key, marker in kept.sources] == [WEATHER]
