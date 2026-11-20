"""The learnt error band (`site/forecast_band.py`)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.site.forecast_band import (
    FORM_ADDITIVE,
    FORM_RATIO,
    MIN_DAYS,
    POOL_CELL,
    POOL_WIDE,
    RATIO_MAX,
    STATUS_LEARNING,
    STATUS_LEARNT,
    WINDOW_DAYS,
    learn,
    pairs_from_log,
    quantile,
    view,
)
from custom_components.spotnav.site.forecast_log import ForecastLog, KIND_HOUSE_MEDIAN

TZ = "Europe/Stockholm"
ZONE = ZoneInfo(TZ)
# A Thursday in winter time (UTC+1).
NOW = datetime(2026, 11, 26, 3, 0, tzinfo=ZONE)


def local(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=ZONE).astimezone(timezone.utc)


def days_before(count: int) -> list[date]:
    return [NOW.date() - timedelta(days=k) for k in range(count, 0, -1)]


def test_quantile_is_linear_between_the_order_statistics() -> None:
    values = [float(v) for v in range(11)]
    assert quantile(values, 0.1) == pytest.approx(1.0)
    assert quantile(values, 0.9) == pytest.approx(9.0)
    assert quantile([1.0, 2.0], 0.5) == pytest.approx(1.5)
    assert quantile([3.0], 0.9) == 3.0


def test_no_band_before_min_days_of_misses() -> None:
    pairs = [(local(day, h), 1.0, 1.5) for day in days_before(MIN_DAYS - 1) for h in range(24)]
    assert learn(pairs, NOW, TZ, FORM_ADDITIVE) is None
    shown = view(None, {}).as_dict()
    assert shown["status"] == STATUS_LEARNING and shown["band"] is None


def test_house_band_is_the_p10_and_p90_of_the_misses_per_hour() -> None:
    # Misses at 18:00 (and its neighbours) spread evenly from -1.0 to +2.0 kW; elsewhere none.
    pairs = []
    for index, day in enumerate(days_before(WINDOW_DAYS)):
        for h in range(24):
            miss = -1.0 + 3.0 * index / (WINDOW_DAYS - 1) if 17 <= h <= 19 else 0.0
            pairs.append((local(day, h), 2.0, 2.0 + miss))
    band = learn(pairs, NOW, TZ, FORM_ADDITIVE)
    assert band is not None and band.days == WINDOW_DAYS
    tomorrow = NOW.date() + timedelta(days=1)  # a Friday
    low, high = band.bounds(2.0, local(tomorrow, 18))
    assert low == pytest.approx(2.0 - 0.7, abs=0.15)
    assert high == pytest.approx(2.0 + 1.7, abs=0.15)
    # A quiet hour has no spread.
    assert band.bounds(2.0, local(tomorrow, 3)) == pytest.approx((2.0, 2.0))
    # The hour's neighbours share in it: 16:00 pools 15-17, of which 17 is spread.
    low16, high16 = band.bounds(2.0, local(tomorrow, 16))
    assert low16 < 2.0 < high16
    # Another reader's quantiles (the demand ceiling's p25): a smaller step down than p10.
    p25 = learn(pairs, NOW, TZ, FORM_ADDITIVE, quantiles=(0.25, 0.9))
    assert p25 is not None and low < p25.bounds(2.0, local(tomorrow, 18))[0] < 2.0


def test_house_band_is_per_day_type_and_widens_when_a_day_type_is_thin() -> None:
    pairs = []
    for day in days_before(WINDOW_DAYS):
        weekend = day.weekday() >= 5
        for h in range(24):
            pairs.append((local(day, h), 1.0, 1.0 + (1.0 if weekend else 0.0)))
    band = learn(pairs, NOW, TZ, FORM_ADDITIVE)
    assert band is not None
    friday, saturday = NOW.date() + timedelta(days=1), NOW.date() + timedelta(days=2)
    weekday = band.spread_at(local(friday, 12))
    weekend = band.spread_at(local(saturday, 12))
    assert weekday is not None and weekday.pool == POOL_CELL and weekday.high == pytest.approx(0.0)
    # 4 weekend days x 3 hours = 12 misses: just enough for a cell of its own.
    assert weekend is not None and weekend.pool == POOL_CELL and weekend.low == pytest.approx(1.0)


def test_a_thin_cell_takes_both_day_types() -> None:
    # Only 2 weekend days in the window: 6 misses per weekend cell, below the minimum.
    pairs = [
        (local(day, h), 1.0, 1.0 if day.weekday() < 5 else 3.0)
        for day in days_before(WINDOW_DAYS)
        if day.weekday() < 5 or day.weekday() == 5
        for h in range(24)
    ]
    band = learn(pairs, NOW, TZ, FORM_ADDITIVE)
    assert band is not None
    saturday = NOW.date() + timedelta(days=2)
    spread = band.spread_at(local(saturday, 12))
    assert spread is not None and spread.pool == POOL_WIDE


def test_a_holiday_counts_as_a_weekend_day() -> None:
    holiday = NOW.date() - timedelta(days=2)  # a Tuesday
    pairs = [(local(day, h), 1.0, 1.0) for day in days_before(WINDOW_DAYS) for h in range(24)]
    band = learn(pairs, NOW, TZ, FORM_ADDITIVE, holidays=frozenset({holiday, NOW.date() + timedelta(days=1)}))
    assert band is not None
    spread = band.spread_at(local(NOW.date() + timedelta(days=1), 12))
    assert spread is not None and ("weekend", 12) in band.cells


def test_house_band_never_goes_below_zero() -> None:
    pairs = [(local(day, h), 1.0, 0.0) for day in days_before(WINDOW_DAYS) for h in range(24)]
    band = learn(pairs, NOW, TZ, FORM_ADDITIVE)
    assert band is not None
    assert band.bounds(0.3, local(NOW.date() + timedelta(days=1), 12)) == (0.0, 0.0)


def test_misses_outside_the_window_or_today_are_not_read() -> None:
    old = [(local(day, h), 1.0, 9.0) for day in days_before(WINDOW_DAYS + 5)[:5] for h in range(24)]
    today = [(local(NOW.date(), h), 1.0, 9.0) for h in range(3)]
    recent = [(local(day, h), 1.0, 1.0) for day in days_before(WINDOW_DAYS) for h in range(24)]
    band = learn(old + today + recent, NOW, TZ, FORM_ADDITIVE)
    assert band is not None
    spread = band.spread_at(local(NOW.date() + timedelta(days=1), 1))
    assert spread is not None and spread.high == pytest.approx(0.0)


def test_sun_band_is_a_ratio_from_hours_with_sun_only() -> None:
    pairs = []
    for index, day in enumerate(days_before(WINDOW_DAYS)):
        ratio = 0.4 + 0.8 * index / (WINDOW_DAYS - 1)  # 0.4 .. 1.2
        for h in range(24):
            forecast = 2.0 if 9 <= h <= 14 else 0.0
            pairs.append((local(day, h), forecast, forecast * ratio))
        pairs.append((local(day, 15), 0.1, 5.0))  # a forecast of next to nothing says nothing
    band = learn(pairs, NOW, TZ, FORM_RATIO)
    assert band is not None and not band.by_day_type
    noon = local(NOW.date() + timedelta(days=1), 12)
    low, high = band.bounds(3.0, noon)
    assert low == pytest.approx(3.0 * 0.48, abs=0.1)
    assert high == pytest.approx(3.0 * 1.12, abs=0.1)
    # A dark hour stays dark; an hour of the day without misses of its own takes every sunny hour's.
    assert band.bounds(0.0, noon) == (0.0, 0.0)
    early = band.spread_at(local(NOW.date() + timedelta(days=1), 6))
    assert early is not None and early.pool == POOL_WIDE and early.samples == WINDOW_DAYS * 6


def test_sun_ratio_is_capped() -> None:
    pairs = [(local(day, 12), 0.5, 50.0) for day in days_before(WINDOW_DAYS)]
    band = learn(pairs, NOW, TZ, FORM_RATIO)
    assert band is not None
    spread = band.spread_at(local(NOW.date() + timedelta(days=1), 12))
    assert spread is not None and spread.high == RATIO_MAX


def test_pairs_from_the_log_and_the_view() -> None:
    log = ForecastLog(tz=TZ)
    for day in days_before(WINDOW_DAYS):
        made = local(day, 0) - timedelta(hours=1)
        log.record_forecast(KIND_HOUSE_MEDIAN, {local(day, h): 1.0 for h in range(24)}, made)
        for h in range(24):
            log.record_actual(KIND_HOUSE_MEDIAN, local(day, h), 1.0 + (0.5 if h % 2 else -0.5))
    pairs = pairs_from_log(log.hours(KIND_HOUSE_MEDIAN))
    assert len(pairs) == WINDOW_DAYS * 24
    band = learn(pairs, NOW, TZ, FORM_ADDITIVE)
    start = local(NOW.date() + timedelta(days=1), 12)
    shown = view(band, {start: 2.0}).as_dict()
    assert shown["status"] == STATUS_LEARNT
    [[when, low, mid, high]] = shown["hours"]
    assert when == start.isoformat() and low < mid == 2.0 < high
    assert shown["band"]["form"] == FORM_ADDITIVE and "weekday/12" in shown["band"]["cells"]
