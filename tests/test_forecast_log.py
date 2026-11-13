"""The forecast log and its nightly review (`site/forecast_log.py`), pure."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.site.forecast_log import (
    ForecastLog,
    KEEP_HOURS_DAYS,
    KEEP_REVIEW_DAYS,
    KIND_HOUSE,
    KIND_SUN,
    KIND_SUN_CORRECTED,
    MAX_HOURS,
    MAX_KINDS,
)

TZ = "Europe/Stockholm"
ZONE = ZoneInfo(TZ)
UTC = timezone.utc


def local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZONE).astimezone(UTC)


def hours_of(day: datetime, count: int = 24) -> list[datetime]:
    return [day + timedelta(hours=index) for index in range(count)]


def test_the_first_forecast_for_an_hour_is_kept_and_a_later_one_never_replaces_it() -> None:
    log = ForecastLog(tz=TZ)
    made = local(2026, 11, 16, 10, 0) + timedelta(seconds=5)
    starts = hours_of(local(2026, 11, 16, 10), 3)
    assert log.record_forecast(KIND_HOUSE, {start: 1.0 for start in starts}, made) == 2  # the running hour is not
    assert log.record_forecast(KIND_HOUSE, {start: 9.0 for start in starts}, made + timedelta(hours=1)) == 0
    kept = log.kinds[KIND_HOUSE].hours
    assert sorted(kept) == starts[1:]
    assert all(hour.forecast == 1.0 and hour.made_at == made for hour in kept.values())
    # An hour with no figure is not logged.
    assert log.record_forecast(KIND_HOUSE, {starts[2] + timedelta(hours=1): None}, made) == 0


def test_the_outcome_is_added_once_and_only_to_a_logged_hour() -> None:
    log = ForecastLog(tz=TZ)
    start = local(2026, 11, 16, 11)
    log.record_forecast(KIND_HOUSE, {start: 1.0}, start - timedelta(hours=2))
    assert not log.record_actual(KIND_HOUSE, start + timedelta(hours=1), 2.0)
    assert not log.record_actual(KIND_HOUSE, start, float("nan"))
    assert log.record_actual(KIND_HOUSE, start, 2.0)
    assert not log.record_actual(KIND_HOUSE, start, 3.0)
    assert log.kinds[KIND_HOUSE].hours[start].actual == 2.0


def test_a_day_is_reviewed_once_after_it_has_ended_with_the_night_and_evening_apart() -> None:
    log = ForecastLog(tz=TZ)
    day = local(2026, 11, 16)
    starts = hours_of(day)
    log.record_forecast(KIND_HOUSE, {start: 1.0 for start in starts}, day - timedelta(hours=30))
    for start in starts:
        hour = start.astimezone(ZONE).hour
        # The night is 0.3 kW over at 03-05 (small), the evening 1 kW over (large); the rest exact.
        actual = 1.3 if 3 <= hour < 6 else 2.0 if 16 <= hour < 20 else 1.0
        log.record_actual(KIND_HOUSE, start, actual)
    assert log.review(local(2026, 11, 16, 23, 30)) == []  # the day has not ended
    made = log.review(local(2026, 11, 17, 0, 5))
    assert [(kind, review.day) for kind, review in made] == [(KIND_HOUSE, date(2026, 11, 16))]
    review = made[0][1]
    assert review.all.hours == 24
    assert review.all.mae == pytest.approx((3 * 0.3 + 4 * 1.0) / 24)
    assert review.all.under_share == pytest.approx(7 / 24)
    assert review.all.big_under_share == pytest.approx(4 / 24)
    assert review.night is not None and review.night.hours == 9
    assert review.night.under_share == pytest.approx(3 / 9) and review.night.big_under_share == 0.0
    assert review.evening is not None and review.evening.hours == 4
    assert review.evening.big_under_share == 1.0
    assert log.review(local(2026, 11, 17, 3)) == []
    assert log.last_week(KIND_HOUSE) is not None


def test_a_day_with_no_outcome_is_not_reviewed_and_the_dst_day_counts_its_own_hours() -> None:
    log = ForecastLog(tz=TZ)
    autumn = local(2026, 10, 25)
    starts = hours_of(autumn, 25)  # 25 hours on the autumn day
    log.record_forecast(KIND_HOUSE, {start: 1.0 for start in starts}, autumn - timedelta(hours=1))
    assert log.review(local(2026, 10, 26, 1)) == []
    for start in starts:
        log.record_actual(KIND_HOUSE, start, 1.0)
    (_kind, review), = log.review(local(2026, 10, 26, 1))
    assert review.all.hours == 25


def test_the_log_is_bounded_in_days_and_rows() -> None:
    log = ForecastLog(tz=TZ)
    start = local(2026, 9, 1)
    for day in range(60):
        moment = start + timedelta(days=day)
        hours = hours_of(moment)
        log.record_forecast(KIND_HOUSE, {hour: 1.0 for hour in hours}, moment - timedelta(minutes=1))
        for hour in hours:
            log.record_actual(KIND_HOUSE, hour, 1.5)
        log.review(moment + timedelta(days=1, minutes=5))
    kept = log.kinds[KIND_HOUSE]
    assert len(kept.hours) <= MAX_HOURS
    assert min(kept.hours) >= start + timedelta(days=60) - timedelta(days=KEEP_HOURS_DAYS + 1)
    assert len(kept.reviews) == KEEP_REVIEW_DAYS
    assert [review.day for review in kept.reviews] == sorted(review.day for review in kept.reviews)
    for index in range(MAX_KINDS + 3):
        log.record_forecast(f"kind{index}", {local(2026, 12, 1): 1.0}, local(2026, 11, 1))
    assert len(log.kinds) == MAX_KINDS


def test_it_survives_a_restart_and_storage_is_untrusted() -> None:
    log = ForecastLog(tz=TZ)
    day = local(2026, 11, 16)
    log.record_forecast(KIND_HOUSE, {start: 1.0 for start in hours_of(day)}, day - timedelta(hours=1))
    for start in hours_of(day, 20):
        log.record_actual(KIND_HOUSE, start, 1.2)
    log.review(local(2026, 11, 17, 1))
    stored = log.as_dict()
    restored = ForecastLog(tz=TZ)
    restored.restore(stored)
    assert restored.as_dict() == stored
    # The review is not made again after a restart.
    assert restored.review(local(2026, 11, 17, 2)) == []
    junk = ForecastLog(tz=TZ)
    junk.restore(
        {
            "kinds": {
                KIND_HOUSE: {
                    "hours": [["bad"], [day.isoformat(), "x", day.isoformat(), None], [day.isoformat(), 1, "no", 1]],
                    "reviews": [{"date": "x"}, {"date": "2026-11-16", "reviewed_at": day.isoformat(), "all": {"hours": 0}}],
                },
                "other": "junk",
            }
        }
    )
    assert junk.kinds[KIND_HOUSE].hours == {} and junk.kinds[KIND_HOUSE].reviews == []
    junk.restore("nonsense")
    assert KIND_HOUSE in junk.kinds
    assert junk.diagnostics()["last_week"] == {KIND_HOUSE: None}


# -- the next day as made at the day-ahead publication


def test_the_next_day_is_taken_from_the_publication_hour_on() -> None:
    from custom_components.spotnav.site.forecast_log import DAY_AHEAD_HOUR, KIND_HOUSE_DAY_AHEAD, next_day_hours

    by_hour = {start: 1.0 for start in hours_of(local(2026, 11, 16, 10), 48)}
    # Before 13:00 local nothing: the next day's prices are not out.
    assert next_day_hours(by_hour, local(2026, 11, 16, DAY_AHEAD_HOUR - 1, 59), TZ) == {}
    at_publication = next_day_hours(by_hour, local(2026, 11, 16, DAY_AHEAD_HOUR, 0), TZ)
    assert sorted(at_publication) == hours_of(local(2026, 11, 17), 24)
    # Logged once: the forecast made at the publication stays the one reviewed, a later refresh adds nothing.
    log = ForecastLog(tz=TZ)
    made = local(2026, 11, 16, 13, 0) + timedelta(seconds=5)
    assert log.record_forecast(KIND_HOUSE_DAY_AHEAD, at_publication, made) == 24
    later = next_day_hours({start: 5.0 for start in by_hour}, made + timedelta(hours=1), TZ)
    assert log.record_forecast(KIND_HOUSE_DAY_AHEAD, later, made + timedelta(hours=1)) == 0
    assert {hour.forecast for hour in log.kinds[KIND_HOUSE_DAY_AHEAD].hours.values()} == {1.0}


def test_the_next_day_has_its_own_number_of_hours_across_a_clock_change() -> None:
    from custom_components.spotnav.site.forecast_log import next_day_hours

    # 2026-10-25 is 25 hours long in Stockholm.
    by_hour = {start: 1.0 for start in hours_of(local(2026, 10, 24, 13), 48)}
    assert len(next_day_hours(by_hour, local(2026, 10, 24, 14), TZ)) == 25
    by_hour = {start: 1.0 for start in hours_of(local(2026, 3, 28, 13), 48)}
    assert len(next_day_hours(by_hour, local(2026, 3, 28, 14), TZ)) == 23


def test_a_log_kept_apart_is_taken_in_once_without_touching_what_is_there() -> None:
    """The sun's log of wave 2 (its own store, the same shape) moves into the site's log once."""
    day = local(2026, 11, 16)
    made = day - timedelta(hours=1)
    old = {
        "version": 1,
        "tz": TZ,
        "kinds": {
            KIND_SUN: {
                "hours": [[(day + timedelta(hours=9)).isoformat(), 2.0, made.isoformat(), 1.5]],
                "reviews": [
                    {
                        "date": "2026-11-15",
                        "reviewed_at": day.isoformat(),
                        "all": {"hours": 8, "mae": 0.4, "under_share": 0.5, "big_under_share": 0.0},
                        "night": None,
                        "evening": None,
                    }
                ],
            },
            KIND_SUN_CORRECTED: {"hours": [], "reviews": []},
            KIND_HOUSE: {"hours": [[(day + timedelta(hours=9)).isoformat(), 9.0, made.isoformat(), None]]},
        },
    }
    log = ForecastLog(tz=TZ)
    log.record_forecast(KIND_HOUSE, {day + timedelta(hours=9): 1.0}, made)

    assert log.take_kinds(old, (KIND_SUN, KIND_SUN_CORRECTED)) == [KIND_SUN, KIND_SUN_CORRECTED]
    assert log.hours(KIND_SUN)[day + timedelta(hours=9)].actual == 1.5
    assert log.kinds[KIND_SUN].reviews[0].night is None
    # A kind it was not asked for, or one it has, is left as it is.
    assert log.hours(KIND_HOUSE)[day + timedelta(hours=9)].forecast == 1.0
    assert log.take_kinds(old, (KIND_SUN,)) == []
    assert log.take_kinds("garbage", (KIND_SUN,)) == []
    shown = log.diagnostics((KIND_SUN, KIND_SUN_CORRECTED))
    assert set(shown["kinds"]) == set(shown["last_week"]) == {KIND_SUN, KIND_SUN_CORRECTED}
    assert set(log.diagnostics()["kinds"]) == {KIND_HOUSE, KIND_SUN, KIND_SUN_CORRECTED}


def test_the_kinds_in_use_fit_under_the_cap() -> None:
    from custom_components.spotnav.battery.plan_shadow import KIND_BATTERY  # noqa: PLC0415
    from custom_components.spotnav.site.forecast_log import KIND_HOUSE_DAY_AHEAD, KIND_HOUSE_RAW  # noqa: PLC0415

    in_use = {KIND_HOUSE, KIND_HOUSE_RAW, KIND_HOUSE_DAY_AHEAD, KIND_BATTERY, KIND_SUN, KIND_SUN_CORRECTED}
    assert len(in_use) == 6
    # Room for the battery plan's three modes beside them.
    assert len(in_use) + 3 <= MAX_KINDS


def test_a_kind_that_changes_what_it_measures_drops_the_days_not_reviewed_and_keeps_its_reviews() -> None:
    log = ForecastLog(tz=TZ)
    day = local(2026, 11, 16)
    log.record_forecast(KIND_HOUSE, {start: 1.0 for start in hours_of(day)}, day - timedelta(hours=1))
    for start in hours_of(day):
        log.record_actual(KIND_HOUSE, start, 1.5)
    assert len(log.review(local(2026, 11, 17, 1))) == 1
    tomorrow = hours_of(local(2026, 11, 17), 6)
    log.record_forecast(KIND_HOUSE, {start: 0.2 for start in tomorrow}, local(2026, 11, 17, 0, 30) - timedelta(hours=1))
    assert log.drop_unreviewed(KIND_HOUSE) == 6
    assert set(log.hours(KIND_HOUSE)) == set(hours_of(day)) and len(log.kinds[KIND_HOUSE].reviews) == 1
    # The same hours are logged again on the new figure; a kind never logged drops nothing.
    assert log.record_forecast(KIND_HOUSE, {start: 1.4 for start in tomorrow}, local(2026, 11, 16, 23)) == 6
    assert log.drop_unreviewed(KIND_SUN) == 0
