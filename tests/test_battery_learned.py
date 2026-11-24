"""A battery's size and rated power learnt from the quarter hours (`battery/learned.py`)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.facts import BatteryFacts
from custom_components.spotnav.battery.learned import (
    EST_CAPACITY_LEARNED,
    EST_RATED_LEARNED,
    LearnedBattery,
    with_learned,
)
from custom_components.spotnav.battery.plan_shadow import BUCKET, EST_ROUND_TRIP, Bucket, battery_params
from tests.test_battery_facts import _facts

T0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=2)
#: Round trip 0.9: a third of the energy's loss on each way.
ROOT = 0.9**0.5


def _run(
    start: datetime, kw: float, soc_from: float, soc_per_bucket: float, count: int, *, covered: float = 900.0
) -> list[Bucket]:
    """`count` quarter hours at `kw` (+ charging) with the SoC moving `soc_per_bucket` in each."""
    out = []
    for i in range(count):
        bucket = Bucket(start=start + BUCKET * i)
        bucket.battery_kwh = kw * covered / 3600.0
        bucket.covered_s = covered
        bucket.soc_start = soc_from + soc_per_bucket * i
        bucket.soc_end = soc_from + soc_per_bucket * (i + 1)
        out.append(bucket)
    return out


def _charge_kwh_for(capacity: float, points: float) -> float:
    """The energy a battery of `capacity` takes in (AC side) to gain `points`."""
    return capacity * points / 100.0 / ROOT


def _charge(start: datetime, capacity: float, soc_from: float, points: float, count: int = 8) -> list[Bucket]:
    kw = _charge_kwh_for(capacity, points) / (count * 0.25)
    return _run(start, kw, soc_from, points / count, count)


def _discharge(start: datetime, capacity: float, soc_from: float, points: float, count: int = 8) -> list[Bucket]:
    kw = -(capacity * points / 100.0 * ROOT) / (count * 0.25)
    return _run(start, kw, soc_from, -points / count, count)


def _three_runs(capacity: float = 10.0) -> list[Bucket]:
    return [
        *_charge(T0, capacity, 20.0, 40.0),
        *_discharge(T0 + timedelta(hours=6), capacity, 80.0, 40.0),
        *_charge(T0 + timedelta(hours=12), capacity, 30.0, 50.0),
    ]


def test_the_capacity_is_the_median_of_several_steady_runs_with_the_round_trip_counted() -> None:
    learned = LearnedBattery()

    assert learned.learn(_three_runs(10.0), NOW)

    assert len(learned.samples) == 3
    assert learned.capacity_kwh == pytest.approx(10.0, abs=0.01)


def test_two_runs_are_not_enough() -> None:
    learned = LearnedBattery()
    learned.learn(_three_runs()[:16], NOW)  # charge + discharge only
    assert len(learned.samples) == 2 and learned.capacity_kwh is None


def test_learning_the_same_quarter_hours_again_adds_nothing() -> None:
    learned = LearnedBattery()
    buckets = _three_runs()
    learned.learn(buckets, NOW)
    assert not learned.learn(buckets, NOW)
    assert len(learned.samples) == 3


def test_a_run_that_moved_less_than_twenty_points_teaches_nothing() -> None:
    learned = LearnedBattery()
    learned.learn(_charge(T0, 10.0, 20.0, 19.0), NOW)
    assert learned.samples == []


def test_a_gap_in_the_run_splits_it() -> None:
    first = _charge(T0, 10.0, 20.0, 20.0, count=4)
    second = _charge(T0 + BUCKET * 5, 10.0, 40.0, 20.0, count=4)  # one quarter hour missing between them
    learned = LearnedBattery()
    learned.learn([*first, *second], NOW)
    # Each half moved 20 points: two samples, none for the 40 across the hole.
    assert [round(kwh, 1) for _end, kwh in learned.samples] == [10.0, 10.0]


def test_a_poorly_measured_quarter_hour_breaks_the_run() -> None:
    run = _charge(T0, 10.0, 20.0, 30.0, count=6)
    run[3].covered_s = 600.0
    learned = LearnedBattery()
    learned.learn(run, NOW)
    assert learned.samples == []


def test_a_soc_jump_inside_a_run_breaks_it() -> None:
    run = _charge(T0, 10.0, 20.0, 30.0, count=6)
    for bucket in run[3:]:
        bucket.soc_start += 8.0
        bucket.soc_end += 8.0
    learned = LearnedBattery()
    learned.learn(run, NOW)
    assert learned.samples == []


def test_a_soc_that_turns_against_the_power_breaks_the_run() -> None:
    run = _charge(T0, 10.0, 20.0, 30.0, count=6)
    run[3].soc_end = run[3].soc_start - 5.0
    learned = LearnedBattery()
    learned.learn(run, NOW)
    assert learned.samples == []


def test_a_power_that_turns_ends_the_run_and_the_idle_quarter_hours_are_no_run() -> None:
    run = [*_charge(T0, 10.0, 20.0, 20.0, count=4), *_run(T0 + BUCKET * 4, 0.1, 40.0, 0.0, 4)]
    learned = LearnedBattery()
    learned.learn(run, NOW)
    assert len(learned.samples) == 1 and learned.samples[0][1] == pytest.approx(10.0, abs=0.01)


def test_a_run_still_going_is_left_for_later() -> None:
    run = _charge(T0, 10.0, 20.0, 40.0)
    learned = LearnedBattery()
    learned.learn(run, run[-1].end + timedelta(minutes=5))
    assert learned.samples == []
    assert learned.learn(run, run[-1].end + BUCKET)
    assert len(learned.samples) == 1


def test_runs_that_disagree_teach_no_size() -> None:
    learned = LearnedBattery()
    learned.learn([*_charge(T0, 10.0, 20.0, 40.0), *_charge(T0 + timedelta(hours=6), 20.0, 20.0, 40.0),
                   *_charge(T0 + timedelta(hours=12), 40.0, 20.0, 40.0)], NOW)
    assert len(learned.samples) == 3 and learned.capacity_kwh is None


def test_one_odd_run_among_agreeing_ones_does_not_move_the_median() -> None:
    learned = LearnedBattery()
    learned.learn([*_three_runs(10.0), *_charge(T0 + timedelta(hours=18), 30.0, 20.0, 40.0)], NOW)
    assert len(learned.samples) == 4 and learned.capacity_kwh == pytest.approx(10.0, abs=0.01)


def test_an_impossible_size_is_dropped() -> None:
    learned = LearnedBattery()
    learned.learn(_charge(T0, 1000.0, 20.0, 40.0), NOW)
    assert learned.samples == []


def test_only_the_latest_samples_are_kept() -> None:
    learned = LearnedBattery()
    for day in range(20):
        learned.learn(_charge(T0 + timedelta(days=day), 10.0, 20.0, 40.0), NOW + timedelta(days=30))
    assert len(learned.samples) == 15


def test_the_rated_power_is_the_highest_steady_one_and_only_rises() -> None:
    learned = LearnedBattery()
    learned.learn(
        [*_run(T0, 3.0, 20.0, 1.0, 3), *_run(T0 + BUCKET * 3, 4.5, 23.0, 1.0, 1),  # 3 kW held, then one 4.5 quarter
         *_run(T0 + timedelta(hours=3), -2.0, 80.0, -1.0, 2)],
        NOW,
    )
    # The single 4.5 kW quarter hour is not steady: the floor is what two in a row held.
    assert learned.rated_charge_kw == 3.0 and learned.rated_discharge_kw == 2.0
    assert not learned.learn(_run(T0 + timedelta(hours=8), 1.0, 20.0, 0.5, 4), NOW)
    assert learned.rated_charge_kw == 3.0
    assert learned.learn(_run(T0 + timedelta(hours=9), 4.0, 20.0, 0.5, 2), NOW)
    assert learned.rated_charge_kw == 4.0


def test_a_run_of_fully_measured_quarter_hours_only_counts_for_the_rated_power() -> None:
    learned = LearnedBattery()
    learned.learn(_run(T0, 6.0, 20.0, 1.0, 4, covered=600.0), NOW)
    assert learned.rated_charge_kw is None


def test_it_survives_a_restart() -> None:
    learned = LearnedBattery()
    learned.learn(_three_runs(), NOW)
    learned.learn(_run(T0 + timedelta(days=1), 3.0, 20.0, 1.0, 2), NOW)

    again = LearnedBattery.from_dict(learned.as_dict())

    assert [s[0] for s in again.samples] == [s[0] for s in learned.samples]
    assert again.capacity_kwh == learned.capacity_kwh == pytest.approx(10.0, abs=0.01)
    assert (again.rated_charge_kw, again.rated_discharge_kw) == (learned.rated_charge_kw, learned.rated_discharge_kw)
    assert not again.learn(_three_runs(), NOW)


@pytest.mark.parametrize("raw", [None, "x", {}, {"samples": "x"}, {"samples": [["bad", 1], [None, 2], ["2026-11-10T00:00:00", 9]]},
                                 {"rated_charge_kw": True, "rated_discharge_kw": -1}])
def test_a_broken_record_starts_empty(raw: object) -> None:
    learned = LearnedBattery.from_dict(raw)
    assert learned.samples == [] and learned.rated_charge_kw is None and learned.rated_discharge_kw is None


# ---- on facts: GoodWe (core) has no size; a Victron without BMS limits has no rated power --------------------------


def _goodwe() -> BatteryFacts:
    facts = _facts("goodwe")
    return facts


def test_what_was_learnt_fills_only_what_the_integration_does_not_read() -> None:
    goodwe = _goodwe()
    assert goodwe.capacity_kwh is None
    learned = LearnedBattery()
    learned.learn(_three_runs(), NOW)
    learned.learn(_run(T0 + timedelta(days=1), 3.0, 20.0, 1.0, 2), NOW)
    learned.learn(_run(T0 + timedelta(days=1, hours=3), -2.5, 80.0, -1.0, 2), NOW)

    filled = with_learned(goodwe, learned)

    assert filled.capacity_kwh == pytest.approx(10.0, abs=0.01) and EST_CAPACITY_LEARNED in filled.estimates
    assert filled.sources["capacity"] == "learned"
    # GoodWe's rating is the BMS's present limit x the voltage (2.56 kW): 3.0 kW seen held is higher, so it counts.
    assert goodwe.rated_charge_kw == pytest.approx(2.56) and "rated_from_current" in goodwe.estimates
    assert filled.rated_charge_kw == 3.0 and EST_RATED_LEARNED in filled.estimates
    assert filled.sources["rated_charge"] == "learned"
    assert set(goodwe.estimates) <= set(filled.estimates)


def test_a_read_value_is_never_replaced() -> None:
    victron = _facts("victron")
    assert victron.capacity_kwh == pytest.approx(10.24)
    learned = LearnedBattery()
    learned.learn(_three_runs(20.0), NOW)

    filled = with_learned(victron, learned)

    assert filled.capacity_kwh == pytest.approx(10.24) and EST_CAPACITY_LEARNED not in filled.estimates
    # Its ratings are the BMS's present limits: only a higher power seen held lifts them, a lower one never lowers.
    assert (filled.rated_charge_kw or 0.0) >= (victron.rated_charge_kw or 0.0)
    assert (filled.rated_discharge_kw or 0.0) >= (victron.rated_discharge_kw or 0.0)


def test_a_rating_the_plant_states_is_never_replaced_but_a_bms_limit_is_lifted_by_what_was_seen() -> None:
    from dataclasses import replace as replaced

    learned = LearnedBattery()
    learned.learn([*_run(T0, 6.0, 20.0, 1.0, 3), *_run(T0 + timedelta(hours=3), -6.0, 80.0, -1.0, 3)], NOW)
    stated = replaced(_goodwe(), rated_charge_kw=5.0, rated_discharge_kw=5.0,
                      sources={"rated_charge": "sensor.plant_rated_charge", "rated_discharge": "sensor.plant_rated_discharge"},
                      estimates=())
    assert with_learned(stated, learned).rated_charge_kw == 5.0
    assert with_learned(stated, learned).rated_discharge_kw == 5.0
    bms = replaced(stated, sources={"rated_charge": "sensor.bms_charge_limit x sensor.bms_voltage",
                                    "rated_discharge": "sensor.bms_discharge_limit x sensor.bms_voltage"},
                   rated_charge_kw=3.7, rated_discharge_kw=11.0, estimates=("rated_from_current",))
    filled = with_learned(bms, learned)
    assert filled.rated_charge_kw == 6.0, "a BMS limit lowered for its own reasons is lifted by what was seen held"
    assert filled.rated_discharge_kw == 11.0, "a BMS limit above what was seen stays"


def test_a_victron_without_bms_limits_gets_its_rating_learnt_and_the_plan_then_runs() -> None:
    gone = {
        "sensor.victron_battery_max_charge_current": {"state": "unavailable"},
        "sensor.victron_battery_max_discharge_current": {"state": "unavailable"},
    }
    facts = _facts("victron", **gone)
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert battery_params(facts)[1] == "no_rated_power"
    learned = LearnedBattery()
    learned.learn([*_run(T0, 3.0, 20.0, 1.0, 3), *_run(T0 + timedelta(hours=3), -2.0, 80.0, -1.0, 3)], NOW)

    filled = with_learned(facts, learned)
    params, reason, _estimates = battery_params(filled)

    assert reason is None and params is not None
    assert (params.max_charge_kw, params.max_discharge_kw) == (3.0, 2.0)
    assert EST_RATED_LEARNED in filled.estimates and "capacity_from_ah" in filled.estimates


def test_the_plan_runs_for_a_goodwe_once_the_size_and_the_rating_are_learnt() -> None:
    goodwe = _goodwe()
    learned = LearnedBattery()
    learned.learn(_three_runs(), NOW)
    learned.learn(_run(T0 + timedelta(days=1), 3.0, 20.0, 1.0, 2), NOW)
    learned.learn(_run(T0 + timedelta(days=1, hours=3), -2.5, 80.0, -1.0, 2), NOW)

    params, reason, _estimates = battery_params(with_learned(goodwe, learned))

    assert reason in (None,) and params is not None and params.usable_kwh == pytest.approx(10.0, abs=0.01)


# ---- the round trip -----------------------------------------------------------------------------------------------


def test_the_plan_assumes_ninety_percent_until_the_round_trip_is_learnt_then_uses_it() -> None:
    victron = _facts("victron")
    learned = LearnedBattery()
    params, _reason, estimates = battery_params(with_learned(victron, learned))
    assert params is not None and params.round_trip_efficiency == 0.90 and EST_ROUND_TRIP in estimates

    learned.round_trip.efficiency = 0.84
    filled = with_learned(victron, learned)
    params, _reason, estimates = battery_params(filled)

    # Victron's battery power is read on the DC bus: the plan's round trip is the AC one (the assumed loss).
    assert filled.power_dc
    assert params is not None and params.round_trip_efficiency == round(0.84 * 0.95 / 1.03, 3)
    assert EST_ROUND_TRIP not in estimates and filled.sources["round_trip"] == "learned"
    # Read on the AC side (or not known): as learnt.
    params, _reason, _estimates = battery_params(with_learned(replace(victron, power_dc=False), learned))
    assert params is not None and params.round_trip_efficiency == 0.84


def test_the_size_counts_the_round_trip_learnt() -> None:
    assumed = LearnedBattery()
    assumed.learn(_three_runs(10.0), NOW)
    worse = LearnedBattery()
    worse.round_trip.efficiency = 0.81
    worse.learn(_three_runs(10.0), NOW)

    # The same charge and discharge runs with a lossier battery: less reached the cells on the way in, more left
    # them on the way out; the charge runs (two of three) set the median lower.
    assert worse.capacity_kwh is not None and assumed.capacity_kwh is not None
    assert worse.capacity_kwh == pytest.approx(10.0 * (0.81 / 0.9) ** 0.5, abs=0.01)


def test_the_round_trip_survives_a_restart_in_the_learnt_record() -> None:
    learned = LearnedBattery()
    learned.round_trip.efficiency = 0.86

    restored = LearnedBattery.from_dict(learned.as_dict())

    assert restored.round_trip.efficiency == 0.86 and restored.round_trip_efficiency == 0.86
    assert LearnedBattery().round_trip_efficiency == 0.90
    assert "round_trip" in restored.view()
