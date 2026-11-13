"""The sun's forecast log: first forecast kept, outcomes, the nightly review, bounds and storage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from custom_components.spotnav.site.sun_forecast_log import (
    KEEP_HOURS_DAYS,
    KIND_SUN,
    KIND_SUN_CORRECTED,
    MAX_KINDS,
    SunForecastLog,
)

MADE = datetime(2026, 6, 19, 6, 30, tzinfo=UTC)


def _hour(hour: int, day: int = 19) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=UTC)


def test_only_the_first_forecast_of_a_future_hour_is_logged() -> None:
    log = SunForecastLog(tz="UTC")

    assert log.record_forecast(KIND_SUN, {_hour(6): 1.0, _hour(9): 2.0, _hour(10): None}, MADE) == 1
    assert log.record_forecast(KIND_SUN, {_hour(9): 5.0}, MADE + timedelta(hours=1)) == 0
    assert log.hours(KIND_SUN)[_hour(9)].forecast == 2.0


def test_the_outcome_is_added_once() -> None:
    log = SunForecastLog(tz="UTC")
    log.record_forecast(KIND_SUN, {_hour(9): 2.0}, MADE)

    assert log.record_actual(KIND_SUN, _hour(9), 1.5)
    assert not log.record_actual(KIND_SUN, _hour(9), 3.0)
    assert not log.record_actual(KIND_SUN, _hour(11), 3.0)
    assert log.hours(KIND_SUN)[_hour(9)].actual == 1.5


def test_a_day_is_reviewed_once_after_it_ended() -> None:
    log = SunForecastLog(tz="UTC")
    log.record_forecast(KIND_SUN, {_hour(9): 2.0, _hour(10): 2.0}, MADE)
    log.record_forecast(KIND_SUN_CORRECTED, {_hour(9): 1.5, _hour(10): 1.5}, MADE)
    for kind in (KIND_SUN, KIND_SUN_CORRECTED):
        log.record_actual(kind, _hour(9), 1.0)
        log.record_actual(kind, _hour(10), 3.0)

    assert log.review(_hour(23)) == []
    made = log.review(_hour(1, day=20))
    assert len(made) == 2
    assert log.review(_hour(2, day=20)) == []
    raw = log.last_week(KIND_SUN)
    corrected = log.last_week(KIND_SUN_CORRECTED)
    assert raw is not None and raw.mae == 1.0 and raw.under_share == 0.5 and raw.big_under_share == 0.5
    assert corrected is not None and corrected.mae == 1.0


def test_bounds_hold() -> None:
    log = SunForecastLog(tz="UTC")
    for index in range(MAX_KINDS + 3):
        log.record_forecast(f"kind{index}", {_hour(9): 1.0}, MADE)
    assert len(log.kinds) == MAX_KINDS

    log = SunForecastLog(tz="UTC")
    log.record_forecast(KIND_SUN, {_hour(9): 1.0}, MADE)
    later = MADE + timedelta(days=KEEP_HOURS_DAYS + 1)
    log.record_forecast(KIND_SUN, {later + timedelta(hours=1): 1.0}, later)
    assert list(log.hours(KIND_SUN)) == [later + timedelta(hours=1)]


def test_the_log_survives_a_round_trip_and_drops_bad_rows() -> None:
    log = SunForecastLog(tz="UTC")
    log.record_forecast(KIND_SUN, {_hour(9): 2.0}, MADE)
    log.record_actual(KIND_SUN, _hour(9), 1.0)
    log.review(_hour(1, day=20))
    stored = json.loads(json.dumps(log.as_dict()))
    stored["kinds"][KIND_SUN]["hours"].append(["nope", 1, "x", None])
    stored["kinds"][KIND_SUN]["reviews"].append({"date": "bad"})

    again = SunForecastLog(tz="UTC")
    again.restore(stored)

    assert again.as_dict() == log.as_dict()
    again.restore("garbage")
    assert again.as_dict() == log.as_dict()


def test_the_store_has_the_house_logs_shape() -> None:
    log = SunForecastLog(tz="UTC")
    log.record_forecast(KIND_SUN, {_hour(9): 2.0}, MADE)
    log.record_actual(KIND_SUN, _hour(9), 1.0)
    log.review(_hour(1, day=20))
    stored = log.as_dict()

    assert set(stored) == {"version", "tz", "kinds"}
    assert stored["kinds"][KIND_SUN]["hours"] == [[_hour(9).isoformat(), 2.0, MADE.isoformat(), 1.0]]
    assert set(stored["kinds"][KIND_SUN]["reviews"][0]) == {"date", "reviewed_at", "all", "night", "evening"}
