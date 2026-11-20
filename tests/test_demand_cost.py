"""The demand charge priced for the plans (`planning/demand_cost.py`): λ, T, the soft ceiling and the power level.

Göteborg Energi's two tariffs: *tidsindelad* is weekdays 07-20, November to March, the mean of the 3 highest days at
135 kr/kW (nothing in summer), with a power level of 6/10/14 … kW in the other hours that may be exceeded three
times in 12 months; *standard* is all hours, the mean of the 3 highest days at 49 kr/kW. Stockholm's clock (CET in
November). 2026-11-16 is a Monday; 2026-12-24 a Thursday.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.auto_settings import DemandComponent
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.planning.demand_cost import (
    ALLOWED_EXCEEDANCES,
    HourCeiling,
    level_count,
    marginal_price,
    part_cost,
    predicted_day_peaks,
    raise_cost,
    remaining_days,
    soft_ceiling,
    threshold,
)
from custom_components.spotnav.planning.planner import HighLoadWindow

TZ = "Europe/Stockholm"
STOCKHOLM = ZoneInfo(TZ)
UTC = timezone.utc
GBG_WINDOW = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20)
GBG_TID = DemandComponent(top_n=3, window=GBG_WINDOW, price_winter=135.0, price_summer=0.0)
GBG_STD = DemandComponent(top_n=3, window=None, price_winter=49.0, price_summer=49.0)
SINGLE = DemandComponent(top_n=1, window=None, price_winter=60.0, price_summer=30.0)


def at(*args: int) -> datetime:
    """A local moment on Stockholm's clock, as UTC."""
    return datetime(*args, tzinfo=STOCKHOLM).astimezone(UTC)


def hour(start: datetime, kw: float, house_kw: float | None = None, **extra: object) -> HourMean:
    return HourMean(start=start, kw=kw, house_kw=house_kw, floor_kw=kw, **extra)


def day_hours(day: date, kw_by_hour: dict[int, float], house: float | None = None) -> list[HourMean]:
    return [hour(at(day.year, day.month, day.day, h), kw, house) for h, kw in kw_by_hour.items()]


def hours_ahead(now: datetime, count: int, kw: float) -> dict[datetime, float]:
    first = now.replace(minute=0, second=0, microsecond=0)
    return {first + timedelta(hours=index): kw for index in range(count)}


# ------------------------------------------------------------------------------------------------ λ


def test_the_marginal_price_is_the_seasons_price_over_the_peaks_counted() -> None:
    assert marginal_price(GBG_TID, 11) == pytest.approx(45.0)
    assert marginal_price(GBG_TID, 4) == 0.0  # summer: nothing priced
    assert marginal_price(GBG_STD, 7) == pytest.approx(49.0 / 3)
    assert marginal_price(SINGLE, 1) == pytest.approx(60.0)
    assert marginal_price(DemandComponent(top_n=3), 1) == 0.0  # no price given: nothing invented


# ------------------------------------------------------------------------------------------------ T


def test_the_owners_rule_under_1_5_kw_or_the_third_highest_once_three_days_exceed_it() -> None:
    """The Göteborg user's own rule: stay under 1.5 kWh/h, or under the 3rd highest peak so far once three days are
    above 1.5. With the house predicted at 1.5 kW on every day left, T is that rule."""
    rest = {date(2026, 11, day): 1.5 for day in range(10, 28)}
    two_above = {date(2026, 11, 2): 2.0, date(2026, 11, 3): 1.8, date(2026, 11, 4): 1.2}
    assert threshold(two_above, rest, 3, free_kw=0.0) == pytest.approx(1.5)
    three_above = {**two_above, date(2026, 11, 5): 2.5, date(2026, 11, 6): 1.9}
    assert threshold(three_above, rest, 3, free_kw=1.9) == pytest.approx(1.9)
    assert threshold(three_above, {}, 3, free_kw=1.9) == pytest.approx(1.9)


def test_the_threshold_never_falls_below_the_free_level_and_is_it_with_fewer_days() -> None:
    assert threshold({date(2026, 11, 2): 3.0}, {}, 3, free_kw=None) == 0.0
    assert threshold({date(2026, 11, 2): 3.0}, {}, 3, free_kw=3.0) == pytest.approx(3.0)
    assert threshold({}, {date(2026, 11, 2): 1.0}, 1, free_kw=2.0) == pytest.approx(2.0)
    # A day measured and predicted counts at the higher of the two.
    assert threshold({date(2026, 11, 2): 1.0}, {date(2026, 11, 2): 2.5}, 1, free_kw=0.0) == pytest.approx(2.5)


# ------------------------------------------------------------------------------------------------ the days left


def test_the_remaining_counted_days_of_a_tidsindelad_november() -> None:
    days = remaining_days(GBG_TID, at(2026, 11, 16, 10), TZ)
    assert days == [date(2026, 11, d) for d in (16, 17, 18, 19, 20, 23, 24, 25, 26, 27, 30)]
    # After the window on the month's last day nothing is left; before it, only that day.
    assert remaining_days(GBG_TID, at(2026, 11, 30, 21), TZ) == []
    assert remaining_days(GBG_TID, at(2026, 11, 30, 10), TZ) == [date(2026, 11, 30)]
    assert remaining_days(GBG_STD, at(2026, 11, 30, 23), TZ) == [date(2026, 11, 30)]


def test_a_holiday_is_no_counted_day() -> None:
    part = DemandComponent(
        top_n=3,
        window=HighLoadWindow(**{**_window_fields(GBG_WINDOW), "holidays": frozenset({date(2026, 12, 24)})}),
        price_winter=135.0,
        price_summer=0.0,
    )
    days = remaining_days(part, at(2026, 12, 21, 8), TZ)
    assert date(2026, 12, 24) not in days and date(2026, 12, 23) in days
    assert date(2026, 12, 24) in remaining_days(GBG_TID, at(2026, 12, 21, 8), TZ)


def _window_fields(window: HighLoadWindow) -> dict[str, object]:
    return {name: getattr(window, name) for name in ("days", "from_month", "to_month", "from_hour", "to_hour", "tz")}


def test_predicted_peaks_take_the_forecast_inside_its_horizon_and_the_usual_peak_beyond() -> None:
    now = at(2026, 11, 16, 10)
    # Four weeks of history: working days peaking at 2.0 kW (one at 4.0), weekends at 3.0 (not counted here).
    history: list[HourMean] = []
    for offset in range(1, 29):
        day = date(2026, 11, 16) - timedelta(days=offset)
        peak = 3.0 if day.isoweekday() > 5 else (4.0 if offset == 3 else 2.0)
        history += day_hours(day, {9: 1.0, 17: peak}, house=None)
        history[-1] = hour(history[-1].start, peak, house_kw=peak)
        history[-2] = hour(history[-2].start, 1.0, house_kw=1.0)
    # The forecast covers today to 10:00 tomorrow, at 1.2 kW, 2.6 kW at 18:00 today.
    forecast = hours_ahead(now, 24, 1.2)
    forecast[at(2026, 11, 16, 18)] = 2.6
    predicted = predicted_day_peaks(GBG_TID, forecast, history, now, TZ)
    assert predicted[date(2026, 11, 16)] == pytest.approx(2.6)  # inside the horizon
    assert predicted[date(2026, 11, 17)] == pytest.approx(2.0)  # partly covered: the usual working-day peak
    assert predicted[date(2026, 11, 30)] == pytest.approx(2.0)
    assert date(2026, 11, 21) not in predicted  # a Saturday counts nothing
    # With neither history nor forecast nothing is predicted (T only from what was measured).
    assert predicted_day_peaks(GBG_TID, {}, [], now, TZ) == {}


# ------------------------------------------------------------------------------------------------ S_h, λ_h


def _november(now: datetime) -> tuple[list[HourMean], list[HourMean]]:
    """The month so far (weekday 17:00 peaks 3.0, 2.8, 2.5 kW in the first week, 2.0 after) and four weeks of
    house history at 1.8 kW on working days."""
    hours: list[HourMean] = []
    peaks = {2: 3.0, 3: 2.8, 4: 2.5}
    day = date(2026, 11, 1)
    while at(day.year, day.month, day.day, 23) < now:
        hours += day_hours(day, {3: 0.8, 17: peaks.get(day.day, 2.0)})
        day += timedelta(days=1)
    history = [
        hour(at(d.year, d.month, d.day, 17), 1.8, house_kw=1.8)
        for d in (date(2026, 11, 16) - timedelta(days=offset) for offset in range(1, 29))
    ]
    return hours, history


def test_tidsindelad_on_a_november_weekday_prices_its_window_hours_only() -> None:
    now = at(2026, 11, 16, 10)
    hours, history = _november(now)
    part = part_cost(GBG_TID, hours, hours_ahead(now, 36, 1.0), history, now, TZ)
    assert part.marginal == pytest.approx(45.0)
    # Three measured days above everything predicted: T is the third highest, 2.5 kW.
    assert part.threshold_kw == pytest.approx(2.5)
    starts = [at(2026, 11, 16, h) for h in range(24)] + [at(2026, 11, 21, 12)]
    ceiling = soft_ceiling((part,), starts, TZ)
    assert set(ceiling) == {at(2026, 11, 16, h) for h in range(7, 20)}
    assert ceiling[at(2026, 11, 16, 12)] == HourCeiling(kw=pytest.approx(2.5), price=pytest.approx(45.0))
    assert raise_cost(ceiling[at(2026, 11, 16, 12)], 3.3) == pytest.approx(45.0 * 0.8)
    assert raise_cost(ceiling[at(2026, 11, 16, 12)], 2.0) == 0.0


def test_early_in_the_month_the_predicted_days_give_a_real_threshold() -> None:
    now = at(2026, 11, 2, 10)
    part = part_cost(GBG_TID, [], {}, _november(at(2026, 11, 16, 10))[1], now, TZ)
    # No day measured yet: the house's usual working-day peak (1.8 kW) on ~20 days left, not "nothing is free".
    assert part.threshold_kw == pytest.approx(1.8)


def test_tidsindelad_in_summer_changes_nothing() -> None:
    now = at(2027, 4, 12, 10)
    part = part_cost(GBG_TID, [hour(at(2027, 4, 5, 12), 5.0)], {}, [], now, TZ)
    assert (part.marginal, part.threshold_kw) == (0.0, None)
    assert soft_ceiling((part,), [at(2027, 4, 12, h) for h in range(24)], TZ) == {}


def test_tidsindelad_leaves_a_holiday_without_a_ceiling() -> None:
    window = HighLoadWindow(**{**_window_fields(GBG_WINDOW), "holidays": frozenset({date(2026, 12, 24)})})
    part = part_cost(
        DemandComponent(top_n=3, window=window, price_winter=135.0, price_summer=0.0),
        [],
        {},
        [],
        at(2026, 12, 21, 8),
        TZ,
    )
    ceiling = soft_ceiling((part,), [at(2026, 12, 23, 12), at(2026, 12, 24, 12)], TZ)
    assert set(ceiling) == {at(2026, 12, 23, 12)}


def test_standard_prices_every_hour_at_a_third_of_49() -> None:
    now = at(2026, 11, 16, 10)
    hours, history = _november(now)
    part = part_cost(GBG_STD, hours, hours_ahead(now, 36, 1.0), history, now, TZ)
    assert part.marginal == pytest.approx(49.0 / 3)
    assert part.threshold_kw == pytest.approx(2.5)
    ceiling = soft_ceiling((part,), [at(2026, 11, 21, 3), at(2026, 11, 16, 23)], TZ)
    assert set(ceiling) == {at(2026, 11, 21, 3), at(2026, 11, 16, 23)}


def test_a_single_peak_part_is_its_price_per_kw_above_the_months_peak() -> None:
    now = at(2026, 11, 16, 10)
    hours, _history = _november(now)
    part = part_cost(SINGLE, hours, {}, [], now, TZ)
    assert part.marginal == pytest.approx(60.0)
    assert part.threshold_kw == pytest.approx(3.0)


def test_stacked_parts_take_the_lowest_threshold_and_add_their_prices() -> None:
    now = at(2026, 11, 16, 10)
    hours, history = _november(now)
    low = part_cost(GBG_TID, hours, {}, history, now, TZ)
    high = part_cost(SINGLE, hours, {}, history, now, TZ)
    ceiling = soft_ceiling((low, high), [at(2026, 11, 16, 12), at(2026, 11, 16, 22)], TZ)
    assert ceiling[at(2026, 11, 16, 12)] == HourCeiling(kw=pytest.approx(2.5), price=pytest.approx(105.0))
    assert ceiling[at(2026, 11, 16, 22)] == HourCeiling(kw=pytest.approx(3.0), price=pytest.approx(60.0))


def test_on_the_last_day_of_the_month_only_its_own_peak_is_predicted() -> None:
    now = at(2026, 11, 30, 10)
    hours, history = _november(now)
    part = part_cost(GBG_TID, hours, hours_ahead(now, 24, 4.0), history, now, TZ)
    assert set(part.predicted) == {date(2026, 11, 30)}
    # The house forecast at 4.0 kW today enters the month's top three: T is the third highest, 2.8 kW.
    assert part.threshold_kw == pytest.approx(2.8)


# ------------------------------------------------------------------------------------------------ power level


def test_the_power_level_counts_complete_hours_in_its_hours_over_a_rolling_year() -> None:
    def outside_window(start: datetime) -> bool:
        return HighLoadWindow(**{**_window_fields(GBG_WINDOW), "tz": TZ}).window_day(start) is None

    now = at(2026, 11, 20, 12)
    hours = [
        hour(at(2025, 11, 19, 22), 12.0),  # more than a year ago
        hour(at(2025, 11, 21, 22), 12.0),  # inside the year: 1
        hour(at(2026, 3, 28, 22), 11.0),  # 2
        HourMean(start=at(2026, 6, 1, 3), kw=13.0, floor_kw=9.0),  # its floor is under the level
        HourMean(start=at(2026, 7, 1, 3), kw=13.0, floor_kw=13.0, incomplete=True),  # a gap: never
        HourMean(start=at(2026, 8, 1, 3), kw=13.0, imported=True),  # imported, not shown complete
        HourMean(start=at(2026, 8, 2, 3), kw=13.0, imported=True, complete=True),  # 3
        hour(at(2026, 11, 18, 12), 15.0),  # in the high-load window: the demand charge's, not the level's
        hour(at(2026, 11, 18, 21), 10.0),  # at the level, not above
    ]
    count = level_count(hours, 10.0, outside_window, now, TZ)
    assert count.exceeded == (at(2025, 11, 21, 22), at(2026, 3, 28, 22), at(2026, 8, 2, 3))
    assert (count.used, count.allowed, count.next_number, count.next_is_last) == (3, ALLOWED_EXCEEDANCES, 4, True)
    fresh = level_count(hours[:2], 10.0, outside_window, now, TZ)
    assert (fresh.next_number, fresh.next_is_last) == (2, False)
    # A year on, the oldest falls out.
    assert level_count(hours, 10.0, outside_window, at(2026, 11, 22, 12), TZ).used == 2
