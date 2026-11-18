"""The house's learned per-phase peaks (`site/phase_peaks.py`) and the grid charge cap that uses them."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from custom_components.spotnav.battery.gate import HOUSE_PEAK_DEFAULT_KW, MIN_RATE_KW, grid_charge_cap_kw
from custom_components.spotnav.site.phase_peaks import (
    BATTERY_BAND_W,
    DAY_MINUTES,
    EXTRA_MARGIN_A,
    HISTORY_DAY_WEIGHT,
    HISTORY_LOST_SHARE,
    MARGIN_A,
    MIN_READY_DAYS,
    PLANT_MISMATCH_A,
    READY_DAYS,
    SPREAD_HOURS,
    SPREAD_MINUTES,
    WINDOW_DAYS,
    HourMark,
    PhasePeaks,
    Reading,
    SitePhasePeaks,
    exact_phase_a,
    extra_margin_a,
    house_phase,
    house_phase_a,
    house_sum_a,
    store_key,
)

VOLTS = 230.0
#: A UTC day's start (epoch seconds), so minutes, hours and days line up in the tests.
T0 = 1_800_000_000 - 1_800_000_000 % 86400


def _reading(site=(5.0, 3.0, 8.0), chargers=(0.0, 0.0, 0.0), battery_w: float | None = 0.0,
             readings: tuple[float, ...] = (), heat=None, plant=None) -> Reading:
    return Reading(site_a=site, chargers_a=chargers, charger_readings=readings, battery_w=battery_w,
                   phase_volts=VOLTS, heat_w=heat, plant_w=plant)


def _feed(peaks: PhasePeaks, start: float, minutes: int, reading=None, *, each=None) -> float:
    """Samples every 10 s for `minutes` minutes from `start`; `each(minute_index)` gives a minute's reading."""
    now = start
    for index in range(minutes):
        for second in range(0, 60, 10):
            peaks.add(start + index * 60 + second, each(index) if each is not None else (reading or _reading()))
        now = start + (index + 1) * 60
    return now


def _close(peaks: PhasePeaks, now: float) -> None:
    """Two more quiet minutes, so the minutes before are judged."""
    _feed(peaks, now, 2, _reading(site=(0.0, 0.0, 0.0)))


# --- one sample -------------------------------------------------------------------------------------------------------


def test_a_sample_takes_the_chargers_off_and_counts_a_small_discharge_on_every_phase() -> None:
    reading = _reading(site=(20.0, 12.0, -3.0), chargers=(10.0, 10.0, 0.0), battery_w=-230.0)
    # A 230 W discharge is 1 A: counted on every phase (it may all be on any one of them), so no phase reads low.
    assert house_phase_a(reading) == pytest.approx((11.0, 3.0, -2.0))
    # The whole load, summed: exact whatever the battery's split.
    assert house_sum_a(reading) == pytest.approx(29.0 - 20.0 + 1.0)
    # A small charge is not taken off (the phase reads at most its share more).
    assert house_phase_a(_reading(site=(5.0, 5.0, 5.0), battery_w=300.0)) == pytest.approx((5.0, 5.0, 5.0))


@pytest.mark.parametrize("battery_w", [BATTERY_BAND_W + 1.0, -(BATTERY_BAND_W + 1.0), None])
def test_a_sample_with_the_battery_s_power_large_or_unknown_does_not_count_per_phase(battery_w) -> None:
    reading = _reading(battery_w=battery_w)
    assert house_phase_a(reading) is None
    # The whole load still counts when the battery's power is known (for the bias factor).
    assert (house_sum_a(reading) is None) == (battery_w is None)


def test_a_sample_without_a_usable_measurement_or_a_charger_s_phase_does_not_count() -> None:
    assert house_phase_a(_reading(site=None)) is None
    assert house_phase_a(_reading(chargers=None)) is None
    assert house_sum_a(_reading(site=None)) is None


# --- the marks --------------------------------------------------------------------------------------------------------


def test_each_hour_keeps_each_phase_s_largest_one_minute_mean() -> None:
    peaks = PhasePeaks()
    # Minute 2 of the hour: L3 at 16 A for 20 s and 8 A for 40 s, a mean of 10.67 A; minute 5: L1 at 12 A.

    def each(index: int) -> Reading:
        return _reading(site=(12.0 if index == 5 else 5.0, 3.0, 8.0))

    now = _feed(peaks, T0, 10, each=each)
    for second in range(0, 60, 10):
        peaks.add(now + second, _reading(site=(5.0, 3.0, 16.0 if second < 20 else 8.0)))
    _close(peaks, now + 60)
    mark = peaks.hours[T0 // 3600]
    assert mark.house_a == pytest.approx((12.0, 3.0, (2 * 16.0 + 4 * 8.0) / 6))
    # The first minute has no minute before it, so it is not judged; the 11th is judged by the quiet minute after it.
    assert mark.minutes == 10


def test_a_minute_with_one_sample_outside_the_band_or_too_few_samples_does_not_count() -> None:
    peaks = PhasePeaks()
    start = _feed(peaks, T0, 3)
    # A minute in which the battery discharged 2 kW for 10 s: its peak (30 A on L1) is not counted per phase.
    for second in range(0, 60, 10):
        peaks.add(start + second, _reading(site=(30.0, 3.0, 8.0), battery_w=-2000.0 if second == 20 else 0.0))
    # A minute with only 3 samples.
    for second in range(0, 30, 10):
        peaks.add(start + 60 + second, _reading(site=(25.0, 3.0, 8.0)))
    _close(peaks, start + 120)
    mark = peaks.hours[T0 // 3600]
    assert mark.house_a == pytest.approx((5.0, 3.0, 8.0))


def test_a_charger_changing_its_current_keeps_that_minute_and_its_neighbours_out() -> None:
    peaks = PhasePeaks()
    # Minutes 0-4 steady; in minute 5 the car starts (its own reading jumps 0 -> 16 A), the meter showing it a minute
    # before the charger's report: minute 4 reads 21 A on L1 with the charger still at 0. Neither counts.

    def each(index: int) -> Reading:
        if index == 4:
            return _reading(site=(21.0, 19.0, 24.0), readings=(0.0, 0.0, 0.0))
        if index == 5:
            return _reading(site=(21.0, 19.0, 24.0), chargers=(16.0, 16.0, 16.0), readings=(16.0, 16.0, 16.0))
        car = 16.0 if index > 5 else 0.0
        return _reading(site=(5.0 + car, 3.0 + car, 8.0 + car), chargers=(car, car, car), readings=(car, car, car))

    now = T0
    for index in range(10):
        for second in range(0, 60, 10):
            reading = each(index)
            if index == 5 and second < 30:
                reading = _reading(site=(21.0, 19.0, 24.0), readings=(0.0, 0.0, 0.0))
            peaks.add(now + second, reading)
        now += 60
    _close(peaks, now)
    mark = peaks.hours[T0 // 3600]
    assert mark.house_a == pytest.approx((5.0, 3.0, 8.0)), "the car's start never reads as the house"
    assert mark.minutes == 6  # minute 0 unjudged; 4, 5 and 6 out; 1-3 and 7-9 counted


def test_a_gap_in_the_samples_counts_nothing_across_it() -> None:
    peaks = PhasePeaks()
    _feed(peaks, T0, 3, _reading(site=(9.0, 3.0, 8.0)))
    # A restart: nothing for 10 minutes, then a quiet stretch.
    _feed(peaks, T0 + 13 * 60, 3)
    mark = peaks.hours[T0 // 3600]
    assert mark.minutes == 1  # minute 1 only: neither minute 2 (before the gap) nor 13 (after it) has both neighbours


def test_the_window_keeps_only_the_last_four_weeks() -> None:
    peaks = PhasePeaks()
    _close(peaks, _feed(peaks, T0, 5))
    later = T0 + (WINDOW_DAYS * 24 + 2) * 3600
    _close(peaks, _feed(peaks, later, 5))
    assert sorted(peaks.hours) == [later // 3600]


# --- what is learned ----------------------------------------------------------------------------------------------


def _days(peaks: PhasePeaks, days: int, *, minutes_a_day: int | None = None, reading=None, start=T0,
          spread: bool = True) -> float:
    """`days` days that count (their minutes spread over twelve clock hours); with `minutes_a_day` or without
    `spread`, each with one stretch of counted minutes instead."""
    if spread and minutes_a_day is None:
        return _spread_days(peaks, days, reading=reading, start=start)
    minutes_a_day = DAY_MINUTES + 5 if minutes_a_day is None else minutes_a_day
    now = start
    for day in range(days):
        now = start + day * 86400
        _close(peaks, _feed(peaks, now, minutes_a_day + 1, reading))
    return now + 86400


def _spread_days(peaks: PhasePeaks, days: int, *, reading=None, start=T0, hours: int = SPREAD_HOURS) -> float:
    """`days` days, each with counted minutes in `hours` of its clock hours."""
    for day in range(days):
        for hour in range(hours):
            _close(peaks, _feed(peaks, start + day * 86400 + hour * 3600, 13, reading))
    return start + days * 86400


def test_the_learning_is_done_only_after_two_weeks_with_enough_minutes() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS - 1)
    learned = peaks.learned(now)
    assert not learned.ready and learned.usable and learned.days_spread == READY_DAYS - 1
    assert learned.days == READY_DAYS - 1
    assert learned.days_left == 1 and learned.as_dict()["days_left"] == 1
    assert PhasePeaks().learned(now).days_left == READY_DAYS
    # A day with too few minutes does not count.
    _days(peaks, 1, minutes_a_day=DAY_MINUTES - 10, start=now)
    assert not peaks.learned(now + 86400).ready
    now = _days(peaks, 1, start=now + 86400)
    learned = peaks.learned(now)
    assert learned.ready and learned.days == READY_DAYS and learned.days_left == 0
    assert learned.phase_peaks() == pytest.approx({"L1": 5.0 + MARGIN_A, "L2": 3.0 + MARGIN_A, "L3": 8.0 + MARGIN_A})


def test_the_further_margin_falls_in_a_straight_line_from_the_first_usable_day_to_two_weeks() -> None:
    assert MIN_READY_DAYS >= 2, "one day never suffices (see the module)"
    assert all(extra_margin_a(days) is None for days in range(MIN_READY_DAYS))
    assert extra_margin_a(MIN_READY_DAYS) == pytest.approx(EXTRA_MARGIN_A)
    assert extra_margin_a(READY_DAYS) == 0.0 and extra_margin_a(READY_DAYS + 9) == 0.0
    steps = [extra_margin_a(days) for days in range(MIN_READY_DAYS, READY_DAYS + 1)]
    assert all(earlier > later for earlier, later in zip(steps, steps[1:], strict=False))
    assert steps[0] - steps[1] == pytest.approx(EXTRA_MARGIN_A / (READY_DAYS - MIN_READY_DAYS))


def test_too_few_days_use_nothing_learned_and_the_first_usable_ones_carry_the_whole_further_margin() -> None:
    peaks = PhasePeaks()
    now = _spread_days(peaks, MIN_READY_DAYS - 1)
    learned = peaks.learned(now)
    # The peaks are there, and not used: the conservative rule stays.
    assert learned.peaks_a is not None and not learned.usable and learned.phase_peaks() is None
    assert learned.extra_margin_a is None and learned.as_dict()["extra_margin_a"] is None
    assert learned.as_dict()["usable"] is False and learned.as_dict()["days_min"] == MIN_READY_DAYS
    now = _spread_days(peaks, 1, start=now)
    learned = peaks.learned(now)
    assert learned.usable and not learned.ready and learned.days == MIN_READY_DAYS == learned.days_spread
    assert learned.days_left == READY_DAYS - MIN_READY_DAYS
    assert learned.extra_margin_a == pytest.approx(EXTRA_MARGIN_A)
    assert learned.phase_peaks() == pytest.approx({
        "L1": 5.0 + MARGIN_A + EXTRA_MARGIN_A, "L2": 3.0 + MARGIN_A + EXTRA_MARGIN_A,
        "L3": 8.0 + MARGIN_A + EXTRA_MARGIN_A,
    })
    # What the status says of the peaks themselves does not hold the further margin.
    assert learned.as_dict()["peaks_a"] == {"L1": 5.0 + MARGIN_A, "L2": 3.0 + MARGIN_A, "L3": 8.0 + MARGIN_A}
    # Half-way: half of it.
    half = (MIN_READY_DAYS + READY_DAYS) // 2
    now = _spread_days(peaks, half - MIN_READY_DAYS, start=now)
    learned = peaks.learned(now)
    assert learned.phase_peaks()["L3"] == pytest.approx(
        8.0 + MARGIN_A + EXTRA_MARGIN_A * (READY_DAYS - half) / (READY_DAYS - MIN_READY_DAYS)
    )


def test_a_day_with_too_few_minutes_is_not_one_of_the_first_usable_days() -> None:
    peaks = PhasePeaks()
    now = _spread_days(peaks, MIN_READY_DAYS - 1)
    now = _days(peaks, 1, minutes_a_day=DAY_MINUTES - 10, start=now)
    assert peaks.learned(now).phase_peaks() is None


def test_days_count_before_the_learning_is_done_only_spread_over_the_day_and_over_the_week() -> None:
    # A week of days each with two hours in one stretch (the battery idle every night at three): seven days by
    # their minutes, none that counts.
    nights = PhasePeaks()
    now = _days(nights, MIN_READY_DAYS, spread=False)
    learned = nights.learned(now)
    assert learned.days == MIN_READY_DAYS and learned.days_spread == 0 and learned.phase_peaks() is None
    # Nor do two weeks of them end the learning, or four: the conservative rule stays, with all its days left.
    for days in (READY_DAYS, WINDOW_DAYS - 1):
        nights = PhasePeaks()
        learned = nights.learned(_days(nights, days, spread=False))
        assert learned.days == days and not learned.ready and not learned.usable and learned.phase_peaks() is None
        assert learned.extra_margin_a is None and learned.days_left == READY_DAYS
    # Two weeks of which only ten are spread over the day: used, with the margin of ten days.
    mixed = PhasePeaks()
    now = _days(mixed, 10)
    learned = mixed.learned(_days(mixed, READY_DAYS - 10, spread=False, start=now))
    assert learned.days == READY_DAYS and not learned.ready and learned.days_left == READY_DAYS - 10
    assert learned.extra_margin_a == pytest.approx(extra_margin_a(10))
    # Eleven clock hours a day: still not; twelve: yes.
    eleven = PhasePeaks()
    assert eleven.learned(_spread_days(eleven, MIN_READY_DAYS, hours=SPREAD_HOURS - 1)).days_spread == 0
    twelve = PhasePeaks()
    assert twelve.learned(_spread_days(twelve, MIN_READY_DAYS)).days_spread == MIN_READY_DAYS
    # Or ten hours of minutes in fewer clock hours.
    long = PhasePeaks()
    now = T0
    for day in range(MIN_READY_DAYS):
        _close(long, _feed(long, T0 + day * 86400, SPREAD_MINUTES + 2))
    assert long.learned(T0 + MIN_READY_DAYS * 86400).days_spread == MIN_READY_DAYS
    # Days that count are no more than the time from the first to the last counted hour holds, a day's slack
    # given: seven days whose first counts only its last ten hours and whose last only its first ten reach over
    # five days and twenty hours, which is six.
    def week(first_from: int, last_from: int) -> int:
        kept = PhasePeaks()
        for day in range(MIN_READY_DAYS):
            start = first_from if day == 0 else last_from if day == MIN_READY_DAYS - 1 else 6
            for hour in range(start, start + 10):
                kept.hours[T0 // 3600 + day * 24 + hour] = HourMark(house_a=(5.0, 3.0, 8.0), minutes=60)
        return kept.learned(T0 + 8 * 86400).days_spread

    assert week(14, 0) == MIN_READY_DAYS - 1
    assert week(10, 0) == MIN_READY_DAYS
    for days in (1, 2):
        kept = PhasePeaks()
        _spread_days(kept, days)
        assert kept.learned(T0 + 3 * 86400).days_spread == days


def test_a_day_read_back_with_many_minutes_lost_does_not_count_and_history_days_weigh_half() -> None:
    def kept(history_days: int, live_days: int, *, lost: int = 0) -> PhasePeaks:
        peaks = PhasePeaks()
        for day in range(history_days + live_days):
            for hour in range(SPREAD_HOURS):
                peaks.hours[T0 // 3600 + day * 24 + hour] = HourMark(
                    house_a=(5.0, 3.0, 8.0), minutes=20, backfilled=day < history_days,
                    lost=lost if day < history_days else 0,
                )
        return peaks

    now = T0 + 20 * 86400
    # Ten days of history, few minutes lost: they count, and carry the whole further margin (ten at half weight
    # are five, less than the first usable days).
    learned = kept(10, 0, lost=5).learned(now)
    assert learned.days == 10 == learned.days_spread and learned.history_days == 10 and learned.history_days_dropped == 0
    assert learned.days_weighted == pytest.approx(10 * HISTORY_DAY_WEIGHT)
    assert learned.usable and learned.extra_margin_a == pytest.approx(EXTRA_MARGIN_A)
    assert extra_margin_a(10) < EXTRA_MARGIN_A
    # Fourteen days of history are not the end of the learning: seven at their weight.
    learned = kept(READY_DAYS, 0).learned(now)
    assert not learned.ready and learned.days_left == READY_DAYS - READY_DAYS // 2
    assert learned.extra_margin_a == pytest.approx(EXTRA_MARGIN_A)
    # Seven of history and five live: 8.5 days for the margin.
    learned = kept(7, 5).learned(now)
    assert learned.days == 12 and learned.extra_margin_a == pytest.approx(extra_margin_a(8.5))
    # Live days alone fall as before; ten of history and nine live end the learning.
    assert kept(0, 10).learned(now).extra_margin_a == pytest.approx(extra_margin_a(10))
    assert kept(10, 9).learned(now).ready and kept(10, 9).learned(now).extra_margin_a == 0.0
    # More than the share of a day's minutes lost to readings that stood still: its history is no day.
    over = int(20 * HISTORY_LOST_SHARE / (1 - HISTORY_LOST_SHARE)) + 1
    learned = kept(10, 0, lost=over).learned(now)
    assert learned.days == 0 and learned.history_days_dropped == 10 and learned.phase_peaks() is None
    assert learned.raw_a == (5.0, 3.0, 8.0), "its peaks are still kept: they can only raise what is learned"
    assert kept(10, 0, lost=over - 1).learned(now).days == 10


def test_a_minute_lost_to_an_unjudged_sample_is_kept_with_the_hour_and_the_store() -> None:
    peaks = PhasePeaks()
    lost = Reading(site_a=None, chargers_a=(0.0, 0.0, 0.0), charger_readings=(), battery_w=0.0, phase_volts=VOLTS,
                   unjudged=True)
    now = _feed(peaks, T0, 5)
    now = _feed(peaks, now, 4, lost)
    _close(peaks, _feed(peaks, now, 5))
    mark = peaks.hours[T0 // 3600]
    assert mark.lost == 4 and mark.minutes >= 8
    mark.backfilled = True
    again = PhasePeaks()
    again.load(json.loads(json.dumps(peaks.as_dict())), now + 600)
    assert again.hours[T0 // 3600].lost == 4
    # A live sample without the measurement is not a lost minute of this kind.
    live = PhasePeaks()
    _close(live, _feed(live, T0, 5, _reading(site=None)))
    assert live.hours[T0 // 3600].lost == 0


def test_the_learned_peak_is_the_window_s_largest_hour_not_a_quantile() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS)
    # One evening, one hour: 17 A on L2 for a whole minute.
    _close(peaks, _feed(peaks, now + 18 * 3600, 3, _reading(site=(5.0, 17.0, 8.0))))
    learned = peaks.learned(now + 20 * 3600)
    assert learned.raw_a == pytest.approx((5.0, 17.0, 8.0))
    assert learned.phase_peaks()["L2"] == pytest.approx(17.0 + MARGIN_A)


def test_the_bias_factor_scales_the_peaks_by_what_the_battery_hid() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS)  # counted minutes: the house's whole load 16 A summed
    # Evenings the battery covers: a 3 kW discharge (out of the band) while the meter reads nothing; the house's whole
    # load is 24 A summed, one and a half times the most seen in a counted minute.
    _close(peaks, _feed(peaks, now + 18 * 3600, 5, _reading(site=(1.0, 1.0, 1.0), battery_w=-21.0 * VOLTS)))
    learned = peaks.learned(now + 20 * 3600)
    assert learned.factor == pytest.approx(24.0 / 16.0)
    # The hidden 8 A (24 - 16, summed) may all sit on any one phase: each phase's own peak plus all of it, which
    # is more than the ratio gives (7.5, 4.5 and 12 A).
    assert learned.phase_peaks() == pytest.approx({
        "L1": 5.0 + 8.0 + MARGIN_A, "L2": 3.0 + 8.0 + MARGIN_A, "L3": 8.0 + 8.0 + MARGIN_A,
    })


def test_the_ratio_counts_where_it_gives_more_than_the_hidden_load_on_one_phase() -> None:
    peaks = PhasePeaks()
    # A phase that exports in the counted minutes: the sum seen (4 A) is small beside L3's own 10 A, so the ratio
    # (hidden 2 A of 4: one and a half times) gives L3 15 A where its peak plus the hidden load gives 12 A.
    now = _days(peaks, READY_DAYS, reading=_reading(site=(-8.0, 2.0, 10.0)))
    _close(peaks, _feed(peaks, now + 18 * 3600, 5, _reading(site=(1.0, 1.0, 1.0), battery_w=-3.0 * VOLTS)))
    learned = peaks.learned(now + 20 * 3600)
    assert learned.factor == pytest.approx(6.0 / 4.0)
    assert learned.phase_peaks() == pytest.approx({"L1": 2.0 + MARGIN_A, "L2": 2.0 + 2.0 + MARGIN_A, "L3": 15.0 + MARGIN_A})


def test_a_single_phase_house_learns_its_one_loaded_phase() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS, reading=_reading(site=(14.0, 0.0, 0.0)))
    assert peaks.learned(now).phase_peaks() == pytest.approx({"L1": 14.0 + MARGIN_A, "L2": MARGIN_A, "L3": MARGIN_A})


def test_a_phase_that_only_ever_exports_never_lowers_the_peak_below_the_margin() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS, reading=_reading(site=(-6.0, 3.0, 8.0)))
    assert peaks.learned(now).phase_peaks()["L1"] == pytest.approx(MARGIN_A)


# --- the plant's own per-phase power (exact) --------------------------------------------------------------------------


def test_the_plant_s_output_per_phase_is_added_back_exactly_whatever_the_battery_does() -> None:
    # A 3.5 kW discharge, far outside the band, all of it as the plant's output, unbalanced: 2.3 kW on L1, 1.15 kW on
    # L3. The meter reads what the house draws beyond it; the car is taken off.
    reading = _reading(site=(1.0, 4.0, 15.0), chargers=(0.0, 0.0, 10.0), battery_w=-3500.0,
                       plant=(10.0 * VOLTS, 0.0, 5.0 * VOLTS))
    assert house_phase_a(reading) is None  # the band would not count it
    assert exact_phase_a(reading) == pytest.approx((11.0, 4.0, 10.0))
    assert house_phase(reading) == (pytest.approx((11.0, 4.0, 10.0)), True)
    assert house_sum_a(reading) == pytest.approx(25.0)


def test_a_grid_charge_draws_through_the_plant_and_is_taken_off_its_phases() -> None:
    # The battery charging from the grid: the plant draws (negative), so the meter's import holds it as well.
    reading = _reading(site=(20.0, 20.0, 20.0), battery_w=9000.0, plant=(-15.0 * VOLTS, -12.0 * VOLTS, -12.0 * VOLTS))
    assert exact_phase_a(reading) == pytest.approx((5.0, 8.0, 8.0))


def test_the_sun_through_a_hybrid_inverter_is_part_of_the_plant_s_output() -> None:
    # Midday: the sun gives 6.9 kW, the battery charges 2.3 kW of it, the plant puts out 4.6 kW (2 kW per phase on
    # L1 and L2, 0.6 kW on L3); the house uses some, the rest is exported. The battery's power alone (charging)
    # would hide the house; the plant's output gives it back.
    plant = (2000.0, 2000.0, 600.0)
    reading = _reading(site=(-6.0, -2.0, 0.5), battery_w=2300.0, plant=plant)
    assert exact_phase_a(reading) == pytest.approx((2000.0 / VOLTS - 6.0, 2000.0 / VOLTS - 2.0, 600.0 / VOLTS + 0.5))
    assert house_sum_a(reading) == pytest.approx(sum(plant) / VOLTS - 7.5)


def test_the_owner_s_unbalanced_reading_with_the_grid_held_at_zero_is_the_plant_s_split() -> None:
    # 2026-11-16 13:44 on the owner's plant: the battery covers the house per phase, the grid sensor reads 0 on each,
    # and the plant's phases read 0.203, 0.079 and 0.972 kW (the heat pump's own meter 0.97 kW on L3). The band rule
    # (were the 1.36 kW discharge inside it) would put the whole discharge on every phase.
    reading = _reading(site=(0.0, 0.0, 0.0), battery_w=-1359.0, plant=(203.0, 79.0, 972.0))
    assert exact_phase_a(reading) == pytest.approx((203.0 / VOLTS, 79.0 / VOLTS, 972.0 / VOLTS))


def test_a_phase_reading_far_below_zero_is_no_exact_sample() -> None:
    # The plant puts out 3 kW on L1 by its reading while the meter imports 20 A there: the plant's phases are not the
    # meter's (or a reading is wrong). The sample falls to the band (outside it here: nothing counts).
    wrong = _reading(site=(-20.0, 20.0, 0.0), battery_w=-3000.0, plant=(0.0, 3000.0, 0.0))
    assert exact_phase_a(wrong) is None and house_phase(wrong) == (None, False)
    # Just above the line still counts (a house with its own sun on another inverter may read a little below 0).
    near = _reading(site=(-PLANT_MISMATCH_A + 0.1, 5.0, 5.0), plant=(0.0, 0.0, 0.0))
    assert exact_phase_a(near) is not None


def test_without_the_plant_s_reading_the_band_rule_counts_as_before() -> None:
    reading = _reading(site=(5.0, 5.0, 5.0), battery_w=-230.0)
    assert exact_phase_a(reading) is None
    assert house_phase(reading) == (pytest.approx((6.0, 6.0, 6.0)), False)
    # Nor without the measurement or a charger's phase.
    assert exact_phase_a(_reading(site=None, plant=(0.0, 0.0, 0.0))) is None
    assert exact_phase_a(_reading(chargers=None, plant=(0.0, 0.0, 0.0))) is None


def test_exact_minutes_count_with_the_battery_busy_and_need_no_bias_factor() -> None:
    peaks = PhasePeaks()
    # Two weeks of the battery covering the house all day (3 kW out, never in the band): the band counts nothing.
    busy = _reading(site=(0.0, 0.0, 0.0), battery_w=-3000.0, plant=(5.0 * VOLTS, 3.0 * VOLTS, 5.0 * VOLTS))
    now = _days(peaks, READY_DAYS, reading=busy)
    learned = peaks.learned(now)
    assert learned.ready and learned.basis == "exact"
    # The quiet minutes that close each day count by the band (no plant reading there), with a large bias factor:
    # once the exact minutes suffice, neither counts.
    assert learned.exact_days == READY_DAYS and learned.factor > 1.0
    assert learned.phase_peaks() == pytest.approx({"L1": 5.0 + MARGIN_A, "L2": 3.0 + MARGIN_A, "L3": 5.0 + MARGIN_A})


def test_band_and_exact_minutes_together_take_the_larger_per_phase_until_the_exact_ones_suffice() -> None:
    peaks = PhasePeaks()
    # A week by the band (16 A summed), then a 24 A evening the band misses: factor 1.5 on the band's peaks.
    now = _days(peaks, 7)
    _close(peaks, _feed(peaks, now + 18 * 3600, 5, _reading(site=(1.0, 1.0, 1.0), battery_w=-21.0 * VOLTS)))
    # Then the plant's per-phase power is there: a week of exact minutes, 12 A on L2.
    exact = _reading(site=(2.0, 12.0, 2.0), battery_w=-1000.0, plant=(0.0, 0.0, 0.0))
    now = _days(peaks, 7, reading=exact, start=now + 86400)
    learned = peaks.learned(now)
    assert learned.ready and learned.basis == "mixed"
    assert learned.factor == pytest.approx(24.0 / 16.0)
    assert learned.phase_peaks() == pytest.approx({
        "L1": 5.0 + 8.0 + MARGIN_A, "L2": 12.0 + MARGIN_A, "L3": 8.0 + 8.0 + MARGIN_A,
    })
    # Two weeks of exact minutes: the band's (scaled) peaks no longer count.
    now = _days(peaks, READY_DAYS - 7, reading=exact, start=now)
    learned = peaks.learned(now)
    assert learned.basis == "exact" and learned.exact_days == READY_DAYS
    assert learned.phase_peaks() == pytest.approx({"L1": 2.0 + MARGIN_A, "L2": 12.0 + MARGIN_A, "L3": 2.0 + MARGIN_A})


def test_a_minute_with_one_sample_by_the_band_is_counted_by_the_band() -> None:
    peaks = PhasePeaks()
    exact = _reading(site=(4.0, 4.0, 4.0), plant=(0.0, 0.0, 0.0))
    band = _reading(site=(4.0, 4.0, 4.0))
    # The plant's reading went stale for one sample of the minute: the minute counts by the band (scaled).
    now = _feed(peaks, T0, 3, each=lambda index: exact)
    for second in range(0, 60, 10):
        peaks.add(now + second, band if second == 30 else exact)
    _close(peaks, now + 60)
    marks = [mark for mark in peaks.hours.values()]
    assert sum(mark.minutes for mark in marks) == 1
    assert sum(mark.exact_minutes for mark in marks) >= 1


def test_exact_marks_survive_the_store_and_old_marks_without_them_still_read() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, 2, reading=_reading(plant=(0.0, 0.0, 0.0), battery_w=-2000.0))
    stored = json.loads(json.dumps(peaks.as_dict()))
    again = PhasePeaks()
    again.load(stored, now)
    assert again.learned(now) == peaks.learned(now)
    assert again.learned(now).exact_minutes > 0
    hour = int(now // 3600) - 1
    old = PhasePeaks()
    old.load({"hours": {
        str(hour): {"a": [1.0, 2.0, 3.0], "n": 5},  # kept before the exact minutes
        str(hour - 1): {"a": None, "n": 0, "xa": [1.0, 2.0, 3.0], "xn": 5},
        str(hour - 2): {"a": None, "n": 0, "xa": None, "xn": 5},  # exact minutes without a peak
        str(hour - 3): {"a": [1.0, 2.0, 3.0], "n": 40, "xa": [1.0, 2.0, 3.0], "xn": 30},  # more than an hour
        str(hour - 4): {"a": None, "n": 0, "xa": [1.0, 2.0, 3.0], "xn": True},
    }}, now)
    assert sorted(old.hours) == [hour - 1, hour]
    assert old.hours[hour].exact_minutes == 0 and old.hours[hour - 1].exact_a == (1.0, 2.0, 3.0)


# --- the store ------------------------------------------------------------------------------------------------------


def test_the_marks_survive_a_store_round_trip_and_a_malformed_store_is_dropped_piece_by_piece() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS, reading=_reading(heat=(1200.0, 0.0, 900.0)))
    stored = json.loads(json.dumps(peaks.as_dict()))
    again = PhasePeaks()
    again.load(stored, now)
    assert again.learned(now) == peaks.learned(now)
    assert again.learned(now).heat_w == pytest.approx((1200.0, 0.0, 900.0))
    hour = str(min(again.hours))
    broken: dict[str, Any] = {"hours": {
        hour: stored["hours"][hour],
        "not an hour": stored["hours"][hour],
        str(int(now // 3600) + 5): stored["hours"][hour],  # in the future
        str(int(now // 3600) - WINDOW_DAYS * 24 - 5): stored["hours"][hour],  # too old
        str(int(now // 3600) - 3): {"a": [1.0, "x", 2.0], "n": 5},
        str(int(now // 3600) - 4): {"a": [1.0, 2.0, math.inf], "n": 5},
        str(int(now // 3600) - 6): {"a": [1.0, 2.0, 3.0], "n": 500},
        str(int(now // 3600) - 7): [1, 2, 3],
    }}
    kept = PhasePeaks()
    kept.load(broken, now)
    assert sorted(kept.hours) == [int(hour)]
    for raw in (None, [], "x", {"hours": []}, {"hours": None}):
        empty = PhasePeaks()
        empty.load(raw, now)
        assert empty.hours == {}


def test_the_store_is_bounded_to_the_window() -> None:
    peaks = PhasePeaks()
    now = T0 + 3600 * 24 * 40
    raw = {"hours": {str(int(now // 3600) - hour): {"a": [1.0, 1.0, 1.0], "n": 30} for hour in range(24 * 40)}}
    peaks.load(raw, now)
    assert len(peaks.hours) <= WINDOW_DAYS * 24 + 1


async def test_the_site_s_marks_are_kept_in_its_own_store(hass, hass_storage) -> None:
    hass_storage[store_key("site")] = {
        "version": 1, "key": store_key("site"),
        "data": {"hours": {str(T0 // 3600): {"a": [4.0, 5.0, 6.0], "n": 30, "all": 15.0, "counted": 15.0}}},
    }
    keeper = SitePhasePeaks(hass, "site")
    await keeper.async_load(T0 + 7200)
    assert keeper.learned(T0 + 7200).raw_a == pytest.approx((4.0, 5.0, 6.0))
    _close(keeper.peaks, _feed(keeper.peaks, T0 + 3600, 4, _reading(site=(9.0, 1.0, 1.0))))
    keeper._store.changed()
    await keeper.async_close()
    saved = hass_storage[store_key("site")]["data"]["hours"]
    assert saved[str(T0 // 3600)]["a"] == [4.0, 5.0, 6.0]
    assert saved[str(T0 // 3600 + 1)]["a"][0] == 9.0
    await SitePhasePeaks.async_remove_stored(hass, "site")
    assert store_key("site") not in hass_storage


# --- the cap ----------------------------------------------------------------------------------------------------------


def _cap(fuse_a=20.0, *, peaks=None, cars_a=0.0, phases=3, house_peak_kw=None):
    return grid_charge_cap_kw(fuse_a, 400.0, margin_a=2.0, cars_a=cars_a, house_peak_kw=house_peak_kw,
                              plant_phases=phases, house_phase_peaks_a=peaks)


def test_the_cap_uses_each_phase_s_learned_peak_once_ready() -> None:
    volts = 400.0 / 3 ** 0.5
    conservative = _cap()
    assert conservative < MIN_RATE_KW, "20 A with 4 kW on one phase: no grid charge"
    peaks = {"L1": 9.0, "L2": 7.0, "L3": 12.0}
    cap = _cap(peaks=peaks)
    # The worst phase (L3) decides: 20 - 2 - 12 = 6 A on each phase for a balanced three-phase charge.
    assert cap == pytest.approx(6.0 * 3 * volts / 1000)
    # On that phase with HA down: the house's peak + the battery's third + the margin stays at the fuse.
    assert peaks["L3"] + cap * 1000 / 3 / volts + 2.0 == pytest.approx(20.0)
    # The chargers' last currents still count, and the conservative p90 no longer does.
    assert _cap(peaks=peaks, cars_a=4.0) == pytest.approx(2.0 * 3 * volts / 1000)
    # The forecast's high figure for the hour no longer counts on one phase, only as a floor spread over three:
    # 8 kW is 11.5 A a phase (below L3's 12 A), 9 kW is 13 A a phase (a day colder than any learned).
    assert _cap(peaks=peaks, house_peak_kw=8.0) == pytest.approx(cap)
    assert _cap(peaks=peaks, house_peak_kw=9.0) == pytest.approx((20.0 - 2.0 - 9000.0 / 3 / volts) * 3 * volts / 1000)
    assert _cap(peaks=peaks, house_peak_kw=math.nan) == pytest.approx(cap)


def test_the_cap_on_a_single_phase_plant_counts_the_worst_phase() -> None:
    volts = 400.0 / 3 ** 0.5
    cap = _cap(peaks={"L1": 9.0, "L2": 7.0, "L3": 12.0}, phases=1)
    assert cap == pytest.approx(6.0 * volts / 1000)
    assert 12.0 + cap * 1000 / volts + 2.0 == pytest.approx(20.0)


@pytest.mark.parametrize("peaks", [
    {"L1": 9.0, "L2": 7.0},
    {"L1": 9.0, "L2": 7.0, "L3": math.nan},
    {"L1": 9.0, "L2": 7.0, "L3": None},
    {},
])
def test_the_cap_falls_back_to_the_conservative_rule_without_three_usable_phases(peaks) -> None:
    assert _cap(25.0, peaks=peaks) == pytest.approx(_cap(25.0))
    assert _cap(25.0) == pytest.approx(
        (25.0 - 2.0 - HOUSE_PEAK_DEFAULT_KW * 1000 / (400.0 / 3 ** 0.5)) * 3 * (400.0 / 3 ** 0.5) / 1000
    )


def _learned_days(days: int, house=(8.0, 6.0, 10.0)):
    peaks = PhasePeaks()
    now = _spread_days(peaks, days, reading=_reading(site=house))
    return peaks.learned(now)


def test_a_20_a_site_grows_its_cap_with_the_days_kept_and_holds_the_fuse_on_every_phase() -> None:
    volts = 400.0 / 3 ** 0.5
    house = (8.0, 6.0, 10.0)
    # No day, and too few: the old rule (4 kW on one phase), which leaves nothing on 20 A.
    for days in (0, MIN_READY_DAYS - 1):
        learned = _learned_days(days) if days else PhasePeaks().learned(T0)
        assert learned.phase_peaks() is None
        assert _cap(peaks=learned.phase_peaks()) == pytest.approx(_cap()) and _cap() < MIN_RATE_KW
    # The first usable days: a grid charge, with the whole further margin.
    first = _cap(peaks=_learned_days(MIN_READY_DAYS).phase_peaks())
    assert first == pytest.approx((20.0 - 2.0 - (10.0 + MARGIN_A + EXTRA_MARGIN_A)) * 3 * volts / 1000)
    assert first > 0.0
    # Two weeks: the normal margin alone.
    done = _cap(peaks=_learned_days(READY_DAYS).phase_peaks())
    assert done == pytest.approx((20.0 - 2.0 - (10.0 + MARGIN_A)) * 3 * volts / 1000)
    # It only grows on the way, and with Home Assistant down every phase stays under the fuse with what the cap
    # kept free: the house's peak there, the battery's third, the cap's own margin and the learned margins.
    caps = [_cap(peaks=_learned_days(days).phase_peaks()) for days in range(MIN_READY_DAYS, READY_DAYS + 1)]
    assert caps == sorted(caps) and caps[0] == pytest.approx(first) and caps[-1] == pytest.approx(done)
    for days, cap in zip(range(MIN_READY_DAYS, READY_DAYS + 1), caps, strict=True):
        battery_a = cap * 1000 / 3 / volts
        for peak in house:
            assert peak + battery_a <= 20.0 - 2.0 - MARGIN_A - extra_margin_a(days) + 1e-9
    # Never above what the fuse allows a house without any load.
    assert done < (20.0 - 2.0) * 3 * volts / 1000


def test_the_learned_peaks_replace_the_old_rule_even_where_that_gave_less_or_more() -> None:
    # A quiet house on 25 A: the learned cap is larger than the 4 kW rule's already at the first usable days.
    quiet = _learned_days(MIN_READY_DAYS, house=(3.0, 2.0, 4.0)).phase_peaks()
    assert _cap(25.0, peaks=quiet) > _cap(25.0)
    # A house that peaks above 4 kW on a phase: smaller than the old rule's.
    heavy = _learned_days(MIN_READY_DAYS, house=(3.0, 2.0, 19.0)).phase_peaks()
    assert _cap(35.0, peaks=heavy) < _cap(35.0)


def test_the_cap_never_goes_below_zero() -> None:
    assert _cap(peaks={"L1": 30.0, "L2": 7.0, "L3": 12.0}) == 0.0
    assert _cap(peaks={"L1": 3.0, "L2": 3.0, "L3": 3.0}, cars_a=40.0) == 0.0
    assert _cap(None, peaks={"L1": 3.0, "L2": 3.0, "L3": 3.0}) == 0.0


# --- what the site holds ----------------------------------------------------------------------------------------------


#: The time the plant's readings are taken against in the site's tests (epoch seconds).
PLANT_NOW = 1_800_000_000.0


def _fake_site(*, state="observing", currents=(10.0, 6.0, 4.0), powers=(2000.0, None, -900.0),
               charger=None, phases=(3, None), battery=(True, -100.0), heat=None, plant=None, plant_sign=1.0):
    """`plant`: per phase (state, unit, age in s) of the plant's per-phase power entities, named in the battery's
    facts; `None` for a battery whose facts name none."""
    from types import SimpleNamespace

    from custom_components.spotnav.site.site_capacity import DirectPhaseMeasurement, PhaseValue

    names = ("L1", "L2", "L3")
    result = SimpleNamespace(
        state=state,
        measured_phase_current_a=dict(zip(names, currents, strict=True)),
        phase_signed_active_power_w=dict(zip(names, powers, strict=True)),
    )
    measured = None if charger is None else DirectPhaseMeasurement(
        *(PhaseValue(value, age_s=age, report_age_s=age) for value, age in charger)
    )
    states = {}
    if heat is not None:
        for suffix, value in zip(("_l1", "_l2", "_l3"), heat, strict=True):
            states[f"sensor.heat{suffix}"] = SimpleNamespace(state=str(value), attributes={"unit_of_measurement": "W"})
    plant_ids = None
    if plant is not None:
        from datetime import UTC, datetime

        plant_ids = tuple(f"sensor.plant_phase_{name}" for name in "abc")
        for entity_id, (value, unit, age) in zip(plant_ids, plant, strict=True):
            states[entity_id] = SimpleNamespace(
                state=value, attributes={"unit_of_measurement": unit},
                last_reported=datetime.fromtimestamp(PLANT_NOW - age, UTC),
            )
    return SimpleNamespace(
        battery_shadow=SimpleNamespace(facts=SimpleNamespace(plant_phase_entities=plant_ids, plant_phase_sign=plant_sign)),
        result=result,
        config={"charger_entry_ids": ["c1"], "max_age_s": 120.0},
        charger_measured_current=lambda _id: measured,
        reckoned_phases=lambda _id: phases,
        battery_live_w=lambda: battery,
        hass=SimpleNamespace(states=SimpleNamespace(get=states.get)),
        house_forecast=SimpleNamespace(heat=SimpleNamespace(
            meter=SimpleNamespace(entity_id="sensor.heat", kind="power") if heat is not None else None
        )),
    )


def test_the_site_s_reading_signs_the_meter_and_takes_fresh_charger_currents_only() -> None:
    from custom_components.spotnav.site.phase_peaks import read_site

    site = _fake_site(charger=((8.0, 5.0), (8.0, 5.0), (6.0, 500.0)), heat=(1500, 0, 700))
    reading = read_site(site, 0.0)
    # L3 exports (its active power is negative); L2's direction is unknown and taken as import (larger).
    assert reading.site_a == (10.0, 6.0, -4.0)
    # L3's charger reading is stale (500 s): counted as 0, so the car stays in the house there (never smaller).
    assert reading.chargers_a == (8.0, 8.0, 0.0)
    assert reading.battery_w == -100.0
    assert reading.heat_w == (1500.0, 0.0, 700.0)
    assert reading.phase_volts == pytest.approx(400.0 / 3 ** 0.5)


def test_the_site_s_reading_without_a_usable_measurement_a_battery_or_a_charger_s_phase() -> None:
    from custom_components.spotnav.site.phase_peaks import read_site

    assert read_site(_fake_site(state="stale_measurements"), 0.0).site_a is None
    assert read_site(_fake_site(currents=(10.0, None, 4.0)), 0.0).site_a is None
    # A site without a battery: 0; one whose battery's power cannot be read: unknown.
    assert read_site(_fake_site(battery=(False, None)), 0.0).battery_w == 0.0
    assert read_site(_fake_site(battery=(True, None)), 0.0).battery_w is None
    # A single-phase charger on L2 takes its largest reading there; on an unknown phase, drawing, it stops the sample.
    one = ((0.0, 5.0), (0.0, 5.0), (7.0, 5.0))
    assert read_site(_fake_site(charger=one, phases=(1, "L2")), 0.0).chargers_a == (0.0, 7.0, 0.0)
    assert read_site(_fake_site(charger=one, phases=(1, None)), 0.0).chargers_a is None
    idle = ((0.0, 5.0), (0.0, 5.0), (0.2, 5.0))
    assert read_site(_fake_site(charger=idle, phases=(1, None)), 0.0).chargers_a == (0.0, 0.0, 0.0)


def test_the_site_s_reading_takes_the_plant_s_fresh_per_phase_power_as_output() -> None:
    from custom_components.spotnav.site.phase_peaks import read_site

    plant = (("0.203", "kW", 2.0), ("79", "W", 4.0), ("0.972", "kW", 1.0))
    reading = read_site(_fake_site(plant=plant), PLANT_NOW)
    assert reading.plant_w == pytest.approx((203.0, 79.0, 972.0))
    # A brand stating its output negative is turned round.
    turned = read_site(_fake_site(plant=plant, plant_sign=-1.0), PLANT_NOW)
    assert turned.plant_w == pytest.approx((-203.0, -79.0, -972.0))


@pytest.mark.parametrize("phase_c", [
    ("0.972", "kW", 500.0),  # stale: older than the site's maximum age (120 s)
    ("unknown", "kW", 1.0),
    ("unavailable", "kW", 1.0),
    ("0.972", "kWh", 1.0),  # not a power
    ("0.972", "kW", -30.0),  # reported in the future
])
def test_the_site_s_reading_without_all_three_phases_fresh_has_no_plant_power(phase_c) -> None:
    from custom_components.spotnav.site.phase_peaks import read_site

    plant = (("0.2", "kW", 2.0), ("0.1", "kW", 2.0), phase_c)
    reading = read_site(_fake_site(plant=plant), PLANT_NOW)
    # No value is made up: the sample falls to the battery's band.
    assert reading.plant_w is None
    assert house_phase(reading)[1] is False


def test_the_site_s_reading_without_the_plant_s_entities_has_no_plant_power() -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.site.phase_peaks import read_site

    assert read_site(_fake_site(), PLANT_NOW).plant_w is None
    site = _fake_site()
    del site.battery_shadow
    assert read_site(site, PLANT_NOW).plant_w is None
    site.battery_shadow = SimpleNamespace(facts=SimpleNamespace(
        plant_phase_entities=("sensor.a", "sensor.b", "sensor.c"), plant_phase_sign=1.0,
    ))
    assert read_site(site, PLANT_NOW).plant_w is None  # the entities have no state


def test_the_exact_peaks_stand_alone_only_after_two_weeks_of_exact_days_spread_over_the_day() -> None:
    def kept(exact_hours: int, *, history_days: int = 0, first_from: int = 0) -> PhasePeaks:
        """Two weeks: every day twelve hours by the band (9 A on L3), and `exact_hours` hours of exact minutes
        (4 A) from the hour `first_from` (the first day) or 0."""
        peaks = PhasePeaks()
        for day in range(READY_DAYS):
            start = first_from if day == 0 else 0
            for hour in range(24):
                mark = HourMark(backfilled=day < history_days)
                if hour < SPREAD_HOURS:
                    mark.house_a, mark.minutes, mark.total_all, mark.total_counted = (5.0, 3.0, 9.0), 20, 17.0, 17.0
                if start <= hour < start + exact_hours:
                    mark.exact_a, mark.exact_minutes = (4.0, 4.0, 4.0), 40
                if mark.counted:
                    peaks.hours[T0 // 3600 + day * 24 + hour] = mark
        return peaks

    now = T0 + (READY_DAYS + 1) * 86400
    # Exact minutes in twelve clock hours of each day: the band is no longer needed.
    learned = kept(SPREAD_HOURS).learned(now)
    assert learned.exact_days == READY_DAYS and learned.basis == "exact"
    assert learned.phase_peaks() == pytest.approx({"L1": 4.0 + MARGIN_A, "L2": 4.0 + MARGIN_A, "L3": 4.0 + MARGIN_A})
    # Three exact hours a day (120 minutes, in one stretch): no exact day, and the band's 9 A still counts.
    learned = kept(3).learned(now)
    assert learned.ready and learned.exact_days == 0 and learned.basis == "mixed"
    assert learned.phase_peaks()["L3"] == pytest.approx(9.0 + MARGIN_A)
    # Days read back weigh half here too: fourteen with four of them from the history are twelve.
    learned = kept(SPREAD_HOURS, history_days=4).learned(now)
    assert learned.exact_days == READY_DAYS - 2 and learned.basis == "mixed"
    # And no more than the time from the first exact hour to the last holds: the first day's only from 14:00,
    # ten hours of minutes, the last day's ending at 10:00, is thirteen.
    late = PhasePeaks()
    for day in range(READY_DAYS):
        for hour in range(14 if day == 0 else 0, 24 if day == 0 else 10):
            late.hours[T0 // 3600 + day * 24 + hour] = HourMark(exact_a=(4.0, 4.0, 4.0), exact_minutes=60)
    assert late.learned(now).exact_days == READY_DAYS - 1
    late.hours[T0 // 3600 + 10] = HourMark(exact_a=(4.0, 4.0, 4.0), exact_minutes=60)
    assert late.learned(now).exact_days == READY_DAYS
