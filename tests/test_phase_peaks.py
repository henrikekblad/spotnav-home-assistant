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
    MARGIN_A,
    READY_DAYS,
    WINDOW_DAYS,
    PhasePeaks,
    Reading,
    SitePhasePeaks,
    house_phase_a,
    house_sum_a,
    store_key,
)

VOLTS = 230.0
#: A UTC day's start (epoch seconds), so minutes, hours and days line up in the tests.
T0 = 1_800_000_000 - 1_800_000_000 % 86400


def _reading(site=(5.0, 3.0, 8.0), chargers=(0.0, 0.0, 0.0), battery_w: float | None = 0.0,
             readings: tuple[float, ...] = (), heat=None) -> Reading:
    return Reading(site_a=site, chargers_a=chargers, charger_readings=readings, battery_w=battery_w,
                   phase_volts=VOLTS, heat_w=heat)


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


def _days(peaks: PhasePeaks, days: int, *, minutes_a_day: int = DAY_MINUTES + 5, reading=None, start=T0) -> float:
    """`days` days, each with a stretch of counted minutes."""
    now = start
    for day in range(days):
        now = start + day * 86400
        _close(peaks, _feed(peaks, now, minutes_a_day + 1, reading))
    return now + 86400


def test_the_learned_peaks_count_only_after_two_weeks_with_enough_minutes() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS - 1)
    learned = peaks.learned(now)
    assert not learned.ready and learned.phase_peaks() is None
    assert learned.days == READY_DAYS - 1
    # A day with too few minutes does not count.
    _days(peaks, 1, minutes_a_day=DAY_MINUTES - 10, start=now)
    assert not peaks.learned(now + 86400).ready
    now = _days(peaks, 1, start=now + 86400)
    learned = peaks.learned(now)
    assert learned.ready and learned.days == READY_DAYS
    assert learned.phase_peaks() == pytest.approx({"L1": 5.0 + MARGIN_A, "L2": 3.0 + MARGIN_A, "L3": 8.0 + MARGIN_A})


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
    assert learned.phase_peaks() == pytest.approx({
        "L1": 5.0 * 1.5 + MARGIN_A, "L2": 3.0 * 1.5 + MARGIN_A, "L3": 8.0 * 1.5 + MARGIN_A,
    })


def test_a_single_phase_house_learns_its_one_loaded_phase() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS, reading=_reading(site=(14.0, 0.0, 0.0)))
    assert peaks.learned(now).phase_peaks() == pytest.approx({"L1": 14.0 + MARGIN_A, "L2": MARGIN_A, "L3": MARGIN_A})


def test_a_phase_that_only_ever_exports_never_lowers_the_peak_below_the_margin() -> None:
    peaks = PhasePeaks()
    now = _days(peaks, READY_DAYS, reading=_reading(site=(-6.0, 3.0, 8.0)))
    assert peaks.learned(now).phase_peaks()["L1"] == pytest.approx(MARGIN_A)


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
    assert _cap(peaks=peaks, house_peak_kw=8.0) == pytest.approx(cap)


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


def test_the_cap_never_goes_below_zero() -> None:
    assert _cap(peaks={"L1": 30.0, "L2": 7.0, "L3": 12.0}) == 0.0
    assert _cap(peaks={"L1": 3.0, "L2": 3.0, "L3": 3.0}, cars_a=40.0) == 0.0
    assert _cap(None, peaks={"L1": 3.0, "L2": 3.0, "L3": 3.0}) == 0.0


# --- what the site holds ----------------------------------------------------------------------------------------------


def _fake_site(*, state="observing", currents=(10.0, 6.0, 4.0), powers=(2000.0, None, -900.0),
               charger=None, phases=(3, None), battery=(True, -100.0), heat=None):
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
    return SimpleNamespace(
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
