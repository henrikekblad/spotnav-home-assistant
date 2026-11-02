"""A demand charge's components against the month's hours, and the site's hour tracker: pure, no Home Assistant.

The pilot's tariff (Tekniska verken "alternativ") is two components with one peak each: day 06-23 and night 23-06,
all year. Stockholm's clock: CET (UTC+1) in winter, CEST (UTC+2) in summer; DST ends 2026-10-25 03:00 CEST and
begins 2027-03-28 02:00 CET.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.planning.auto_settings import (
    AutoSettingsError,
    DemandComponent,
    demand_components_from,
    GridTariff,
)
from custom_components.spotnav.planning.demand_charge import (
    component_month,
    counted_day,
    hour_start,
    HourMean,
    month_of,
)
from custom_components.spotnav.planning.planner import HighLoadWindow
from custom_components.spotnav.site.demand_tracker import (
    GAP_MAX_S,
    HourTracker,
    RunningHour,
)

TZ = "Europe/Stockholm"
UTC = timezone.utc
DAY = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=6, to_hour=23)
NIGHT = HighLoadWindow(days="all", from_month=1, to_month=12, from_hour=23, to_hour=6)
PILOT = (
    DemandComponent(top_n=1, window=DAY, price_winter=45.0, price_summer=23.0),
    DemandComponent(top_n=1, window=NIGHT, price_winter=12.0, price_summer=8.0),
)
ALL_TOP3 = DemandComponent(top_n=3)
WINTER_WEEKDAYS_TOP5 = DemandComponent(
    top_n=5, window=HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20)
)


def cet(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A winter (CET, UTC+1) wall-clock time as UTC."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=1)


def hour(start: datetime, kw: float, *, incomplete: bool = False) -> HourMean:
    return HourMean(start=start, kw=kw, house_kw=None, incomplete=incomplete)


# ---------------------------------------------------------------------------------------------- the components


def test_a_component_is_stored_and_read_exactly_and_bad_ones_are_refused() -> None:
    raw = [component.as_dict() for component in PILOT]
    assert demand_components_from(raw) == PILOT
    assert raw[1] == {
        "window": {"days": "all", "from_month": 1, "to_month": 12, "from_hour": 23, "to_hour": 6},
        "top_n": 1,
        "price_per_kw": {"winter": 12.0, "summer": 8.0},
    }
    unknown_prices = {"window": None, "top_n": 3, "price_per_kw": {"winter": None, "summer": None}}
    assert demand_components_from([unknown_prices]) == (DemandComponent(top_n=3),)
    bad = [
        {**unknown_prices, "top_n": 2},
        {**unknown_prices, "top_n": True},
        {**unknown_prices, "price_per_kw": {"winter": -1, "summer": None}},
        {**unknown_prices, "price_per_kw": {"winter": "45", "summer": None}},
        {**unknown_prices, "price_per_kw": {"winter": 1}},
        {**unknown_prices, "window": {**raw[1]["window"], "to_hour": 23}},
        {**unknown_prices, "window": {**raw[1]["window"], "from_hour": 24}},
        {**unknown_prices, "extra": 1},
        {"top_n": 1, "price_per_kw": {"winter": None, "summer": None}},
    ]
    for item in bad:
        with pytest.raises(AutoSettingsError) as refused:
            demand_components_from([item])
        assert refused.value.code == "invalid_fiscal", item
    with pytest.raises(AutoSettingsError):
        demand_components_from([unknown_prices] * 4)
    with pytest.raises(AutoSettingsError):
        demand_components_from({"top_n": 1})


def test_the_tariff_carries_its_components_and_an_unreadable_list_is_none() -> None:
    tariff = GridTariff(demand=PILOT)
    assert not tariff.empty
    stored = tariff.as_dict()
    assert GridTariff.from_data(stored) == tariff
    assert GridTariff.from_data({**stored, "demand_components": [{"top_n": 7}]}) == GridTariff()
    assert GridTariff.from_data({}) == GridTariff()
    with pytest.raises(AutoSettingsError):
        GridTariff(demand=PILOT * 2).validated()


# ---------------------------------------------------------------------------------------------- days and months


def test_the_night_belongs_to_the_day_it_starts_on_and_the_hour_to_its_own_month() -> None:
    night = PILOT[1]
    # 23:00 on 31 October (CET since the 25th) and 02:00 on 1 November: the same night, of 31 October.
    late = cet(2026, 10, 31, 23)
    early = cet(2026, 11, 1, 2)
    assert counted_day(night, late, TZ) == date(2026, 10, 31)
    assert counted_day(night, early, TZ) == date(2026, 10, 31)
    assert counted_day(PILOT[0], early, TZ) is None
    assert counted_day(PILOT[0], cet(2026, 11, 1, 6), TZ) == date(2026, 11, 1)
    assert counted_day(night, cet(2026, 11, 1, 6), TZ) is None
    # Billed in the hour's own month: the morning of the 1st is November's.
    assert month_of(late, TZ) == (2026, 10)
    assert month_of(early, TZ) == (2026, 11)
    # A weekday window in winter only: Saturday and April do not count.
    assert counted_day(WINTER_WEEKDAYS_TOP5, cet(2026, 11, 7, 8), TZ) is None
    assert counted_day(WINTER_WEEKDAYS_TOP5, cet(2026, 11, 6, 8), TZ) == date(2026, 11, 6)
    april = datetime(2027, 4, 1, 8, tzinfo=UTC) - timedelta(hours=2)
    assert counted_day(WINTER_WEEKDAYS_TOP5, april, TZ) is None
    march = datetime(2027, 3, 31, 6, tzinfo=UTC)  # 08:00 CEST, a Wednesday
    assert counted_day(WINTER_WEEKDAYS_TOP5, march, TZ) == date(2027, 3, 31)


def test_dst_nights_have_23_and_25_hours_and_the_repeated_hour_is_two() -> None:
    # 25 October 2026: 02:00 CEST and 02:00 CET are two hours with their own starts.
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET
    assert hour_start(first, TZ) == datetime(2026, 10, 25, 0, tzinfo=UTC)
    assert hour_start(second, TZ) == datetime(2026, 10, 25, 1, tzinfo=UTC)
    # Both lie in the night of the 24th.
    assert counted_day(PILOT[1], first, TZ) == counted_day(PILOT[1], second, TZ) == date(2026, 10, 24)
    # 28 March 2027: 02:00 CET jumps to 03:00 CEST; 01:30 CET and 03:30 CEST are consecutive hours.
    before = datetime(2027, 3, 28, 0, 30, tzinfo=UTC)
    after = datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    assert hour_start(after, TZ) - hour_start(before, TZ) == timedelta(hours=1)
    assert counted_day(PILOT[1], after, TZ) == date(2027, 3, 27)


# ---------------------------------------------------------------------------------------------- top-N per component


@pytest.mark.parametrize("days", [0, 1, 2, 3, 4, 5])
def test_the_bar_is_the_mean_of_the_highest_days_with_what_exists(days: int) -> None:
    peaks = [4.0, 6.0, 5.0, 3.0, 7.0][:days]
    hours = []
    for index, kw in enumerate(peaks):
        start = cet(2026, 11, 2 + index, 18)
        hours += [hour(start, kw), hour(start + timedelta(hours=1), kw / 2)]
    now = cet(2026, 11, 20, 12)
    month = component_month(ALL_TOP3, hours, TZ, now)
    top = sorted(peaks, reverse=True)[:3]
    assert month.days_counted == days
    assert month.bar_kw == (None if not top else pytest.approx(sum(top) / len(top)))
    assert [peak.kw for peak in month.peaks] == top
    # Today (the 20th) has no peak: with three days measured the third highest is free, else nothing is.
    assert month.free_kw == (top[-1] if days >= 3 else 0.0)
    single = component_month(DemandComponent(top_n=1), hours, TZ, now)
    assert single.bar_kw == (None if not peaks else max(peaks))


def test_peaks_are_on_different_days_and_today_in_the_top_is_free_up_to_its_own_peak() -> None:
    # Two high hours on one day count once.
    hours = [
        hour(cet(2026, 11, 2, 17), 9.0),
        hour(cet(2026, 11, 2, 18), 8.5),
        hour(cet(2026, 11, 3, 18), 5.0),
        hour(cet(2026, 11, 4, 18), 4.0),
        hour(cet(2026, 11, 5, 18), 3.0),
    ]
    month = component_month(ALL_TOP3, hours, TZ, cet(2026, 11, 4, 21))
    assert [(peak.day, peak.kw) for peak in month.peaks] == [
        (date(2026, 11, 2), 9.0),
        (date(2026, 11, 3), 5.0),
        (date(2026, 11, 4), 4.0),
    ]
    assert month.bar_kw == pytest.approx(6.0)
    # The 4th is among the three: up to its own 4.0 kW raises nothing.
    assert month.free_kw == 4.0
    # On the 5th (not in the three) the third highest is free.
    assert component_month(ALL_TOP3, hours, TZ, cet(2026, 11, 5, 21)).free_kw == 4.0
    five = component_month(WINTER_WEEKDAYS_TOP5, hours, TZ, cet(2026, 11, 5, 9))
    assert five.days_counted == 4 and five.free_kw == 3.0


def test_the_pilots_day_and_night_components_each_take_their_own_peak() -> None:
    hours = [
        hour(cet(2026, 11, 1, 22), 7.0),  # day of the 1st (06-23)
        hour(cet(2026, 11, 1, 23), 11.0),  # night of the 1st
        hour(cet(2026, 11, 2, 3), 9.0),  # night of the 1st too
        hour(cet(2026, 11, 2, 12), 5.0),
        hour(cet(2026, 11, 3, 1), 2.0),  # night of the 2nd
    ]
    now = cet(2026, 11, 3, 10)
    day, night = (component_month(component, hours, TZ, now) for component in PILOT)
    assert (day.bar_kw, day.days_counted, day.price_per_kw) == (7.0, 2, 45.0)
    assert (night.bar_kw, night.days_counted, night.price_per_kw) == (11.0, 2, 12.0)
    assert night.peaks[0].start == cet(2026, 11, 1, 23)
    # At 10:00 the night component does not count: nothing is held for it.
    assert night.free_kw is None and day.free_kw == 7.0
    summer = component_month(PILOT[0], [], TZ, datetime(2026, 6, 1, 10, tzinfo=UTC))
    assert summer.price_per_kw == 23.0 and summer.bar_kw is None


def test_an_incomplete_hour_never_raises_a_peak() -> None:
    hours = [hour(cet(2026, 11, 2, 18), 3.0), hour(cet(2026, 11, 2, 19), 12.0, incomplete=True)]
    month = component_month(DemandComponent(top_n=1), hours, TZ, cet(2026, 11, 3, 10))
    assert month.bar_kw == 3.0 and month.days_counted == 1


# ---------------------------------------------------------------------------------------------- the hour tracker


def run(tracker: HourTracker, start: datetime, minutes: int, grid_w: float, *, step_s: float = 30.0, car_w=None):
    """Observe every `step_s` from `start` for `minutes` at `grid_w`; returns the moment after."""
    moment = start
    end = start + timedelta(minutes=minutes)
    while moment < end:
        tracker.observe(moment, grid_w, car_w)
        moment += timedelta(seconds=step_s)
    return moment


def test_an_hour_is_its_net_import_energy_over_the_hour_and_export_counts_as_nothing() -> None:
    tracker = HourTracker(tz=TZ)
    start = cet(2026, 11, 2, 18)
    moment = run(tracker, start, 30, 6000.0, car_w=4000.0)
    moment = run(tracker, moment, 30, -2000.0, car_w=0.0)
    tracker.observe(moment + timedelta(seconds=1), 0.0, 0.0)
    assert len(tracker.hours) == 1
    completed = tracker.hours[0]
    assert completed.start == start
    assert completed.kw == pytest.approx(3.0)
    assert completed.house_kw == pytest.approx(1.0)
    assert not completed.incomplete
    assert tracker.month == (2026, 11)


def test_a_twelve_minute_gap_makes_the_hour_incomplete_and_a_short_one_does_not() -> None:
    for gap_minutes, incomplete in ((12, True), (8, False)):
        tracker = HourTracker(tz=TZ)
        start = cet(2026, 11, 2, 18)
        moment = run(tracker, start, 20, 5000.0)
        moment = run(tracker, moment, gap_minutes, None)  # type: ignore[arg-type]
        moment = run(tracker, moment, 60 - 20 - gap_minutes, 5000.0)
        tracker.observe(moment, 5000.0, None)
        assert tracker.hours[0].incomplete is incomplete, gap_minutes
        assert tracker.hours[0].kw == pytest.approx(5.0)
        assert tracker.hours[0].house_kw is None


def test_a_reading_is_held_two_minutes_at_most() -> None:
    tracker = HourTracker(tz=TZ)
    start = cet(2026, 11, 2, 18)
    tracker.observe(start, 6000.0, None)
    tracker.observe(start + timedelta(minutes=30), 6000.0, None)
    running = tracker.running
    assert running is not None and running.covered_s == 120.0 and running.gap_s == pytest.approx(1680.0)


def test_a_restart_mid_hour_keeps_the_hour_and_counts_the_downtime_as_a_gap() -> None:
    tracker = HourTracker(tz=TZ)
    start = cet(2026, 11, 2, 18)
    moment = run(tracker, start, 25, 4000.0)
    stored = (tracker.month_dict(), tracker.running.as_dict())  # type: ignore[union-attr]
    restored = HourTracker(tz=TZ)
    restored.restore(*stored)
    assert restored.running is not None and restored.running.last_grid_w is None
    # Back after three minutes: those are a gap, the hour stays complete.
    moment = run(restored, moment + timedelta(minutes=3), 32, 4000.0)
    restored.observe(moment, 4000.0, None)
    assert restored.hours[0].kw == pytest.approx(4.0)
    assert not restored.hours[0].incomplete
    # A restart longer than the gap allowance makes it incomplete.
    late = HourTracker(tz=TZ)
    late.restore(*stored)
    moment = run(late, start + timedelta(minutes=45), 15, 9000.0)
    late.observe(moment, 9000.0, None)
    assert late.hours[0].incomplete
    assert component_month(DemandComponent(), late.hours, TZ, moment).bar_kw is None


def test_the_month_rolls_at_the_first_hour_of_the_month_on_the_areas_clock() -> None:
    tracker = HourTracker(tz=TZ)
    moment = run(tracker, cet(2026, 10, 31, 22), 120, 3000.0, step_s=60.0)
    tracker.observe(moment, 3000.0, None)
    # 22:00 and 23:00 on 31 October, then the first hour of November is running.
    assert tracker.month == (2026, 11)
    assert tracker.hours == []
    assert [hour.start for hour in tracker.previous] == [cet(2026, 10, 31, 22), cet(2026, 10, 31, 23)]
    assert tracker.previous_month == (2026, 10)
    assert tracker.month_hours(moment) == []
    moment = run(tracker, moment, 60, 2000.0, step_s=60.0)
    tracker.observe(moment, 2000.0, None)
    assert [hour.start for hour in tracker.hours] == [cet(2026, 11, 1, 0)]
    # The night of 31 October takes November's hour under the 31st.
    night = component_month(PILOT[1], tracker.month_hours(moment), TZ, moment)
    assert night.peaks[0].day == date(2026, 10, 31)


def test_the_autumn_night_measures_25_hours() -> None:
    tracker = HourTracker(tz=TZ)
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)  # 00:00 CEST on the 25th
    moment = run(tracker, start, 25 * 60, 1000.0, step_s=60.0)
    tracker.observe(moment, 1000.0, None)
    starts = [hour.start for hour in tracker.hours]
    assert len(starts) == 25 and len(set(starts)) == 25


def test_storage_is_untrusted() -> None:
    tracker = HourTracker(tz=TZ)
    tracker.restore(
        {"month": "2026-11", "hours": [["bad"], [cet(2026, 11, 2, 18).isoformat(), 3.0, None, 0], 7]},
        {"start": "nonsense"},
    )
    assert tracker.month == (2026, 11)
    assert [hour.kw for hour in tracker.hours] == [3.0]
    assert tracker.running is None
    assert RunningHour.from_dict({"start": cet(2026, 11, 2, 18).isoformat(), "import_wh": -1}) is None
    tracker.restore("junk", None)
    assert tracker.month == (2026, 11)


def test_a_gap_hour_limit_is_ten_minutes() -> None:
    assert GAP_MAX_S == 600.0
