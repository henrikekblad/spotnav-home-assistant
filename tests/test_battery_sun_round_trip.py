"""The sun's own round trip over a hybrid's DC bus (`conversion_loss.sun_round_trip`, `BatteryParams`).

On a DC-coupled hybrid the sun's surplus reaches the battery without crossing the inverter: a kWh of it stored and
given back loses about the battery's own (DC) round trip, not the AC one a kWh bought pays. The plan, the perfect plan
in hindsight and the review price the sun's part of a charge with it; a grid charge and an AC-coupled battery keep one
round trip.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.conversion_loss import (
    SUN_ROUND_TRIP_CAP,
    ConversionLoss,
    ac_kw,
    sun_round_trip,
)
from custom_components.spotnav.battery.facts import BatteryFacts
from custom_components.spotnav.battery.hindsight import perfect_plans, simulate
from custom_components.spotnav.battery.learned import LearnedBattery, with_learned
from custom_components.spotnav.battery.plan import action_power, plan_battery, run_slot
from custom_components.spotnav.battery.plan_model import (
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    BatteryParams,
    PlanInputError,
    PlanRequest,
    PlanSlot,
)
from custom_components.spotnav.battery.plan_shadow import battery_params

T0 = datetime(2026, 6, 21, 6, tzinfo=timezone.utc)
H = timedelta(hours=1)
#: The owner's plant: the AC round trip the plan uses and the loss learnt from its own load.
AC_RT, LOSS, DC_RT = 0.872, (0.0366, 0.0648), 0.967
SUN_RT = sun_round_trip(AC_RT, LOSS, DC_RT)
AC_BATTERY = BatteryParams(10.0, 5.0, 5.0, min_soc_pct=10.0, round_trip_efficiency=AC_RT)
DC_BATTERY = replace(AC_BATTERY, sun_round_trip_efficiency=SUN_RT, discharge_loss=LOSS[1])


def _slot(index: int, *, buy: float, sell: float, house: float, sun: float = 0.0) -> PlanSlot:
    return PlanSlot(T0 + index * H, T0 + (index + 1) * H, buy=buy, sell=sell, house_kw=house, solar_kw=sun)


def _summer() -> tuple[PlanSlot, ...]:
    """Eight hours of 3 kW surplus sold at 50, then eight evening hours of 1.5 kW bought at 55: worth storing at the
    sun's round trip (55 x 0.967 > 50), not at the AC one (55 x 0.872 < 50)."""
    day = [_slot(k, buy=60, sell=50, house=0.5, sun=3.5) for k in range(8)]
    evening = [_slot(8 + k, buy=55, sell=40, house=1.5) for k in range(8)]
    return (*day, *evening)


def _winter() -> tuple[PlanSlot, ...]:
    """No surplus at all: cheap night, dear evening, a little sun under the house."""
    night = [_slot(k, buy=40, sell=30, house=1.0) for k in range(6)]
    day = [_slot(6 + k, buy=90, sell=70, house=2.0, sun=0.5) for k in range(6)]
    evening = [_slot(12 + k, buy=150, sell=110, house=2.5) for k in range(6)]
    return (*night, *day, *evening)


def _request(slots: tuple[PlanSlot, ...], battery: BatteryParams, **kwargs) -> PlanRequest:
    return PlanRequest(slots=slots, battery=battery, soc_now_pct=10.0, end_value=0.0, cycle_cost=0.0, **kwargs)


def _charged(decisions) -> float:
    return sum(max(0.0, d.battery_kw) * (d.end - d.start).total_seconds() / 3600 for d in decisions)


# ---- the round trip itself


def test_the_owners_sun_round_trip_is_about_the_dc_one() -> None:
    assert SUN_RT == pytest.approx(0.967, abs=0.002)
    # Until the counters show it: never above the assumed round trip turned back, nor 0.97.
    assert sun_round_trip(0.90, LOSS) == pytest.approx(0.97)
    assert sun_round_trip(AC_RT, LOSS) == pytest.approx(AC_RT * (1 + LOSS[0]) / (1 - LOSS[1]))
    assert sun_round_trip(0.85, (0.03, 0.05)) == pytest.approx(0.85 * 1.03 / 0.95)
    # Within what the DC counters showed, never above them, and never worse than the AC one.
    assert sun_round_trip(AC_RT, LOSS, dc=0.95) == pytest.approx(0.95)
    assert sun_round_trip(0.95, (0.08, 0.08), dc=0.99) == SUN_ROUND_TRIP_CAP
    assert sun_round_trip(0.90, (0.0, 0.0)) == pytest.approx(0.90)
    assert sun_round_trip(0.90, (0.03, 0.05), dc=0.80) == pytest.approx(0.90)


def test_a_kwh_of_sun_stored_and_given_back_makes_the_sun_round_trip() -> None:
    stored = DC_BATTERY.stored_kwh(1.0, 1.0, 1.0)
    assert stored * DC_BATTERY.discharge_efficiency == pytest.approx(SUN_RT)
    # The grid's part keeps the AC round trip; a charge beyond the surplus is the grid's.
    assert DC_BATTERY.stored_kwh(1.0, 0.0, 1.0) * DC_BATTERY.discharge_efficiency == pytest.approx(AC_RT)
    assert DC_BATTERY.stored_kwh(2.0, 0.5, 1.0) == pytest.approx(
        0.5 * DC_BATTERY.sun_charge_efficiency + 1.5 * DC_BATTERY.charge_efficiency
    )
    # A discharge is the same whatever the sun does.
    assert DC_BATTERY.stored_kwh(-1.0, 3.0, 1.0) == DC_BATTERY.stored_kwh(-1.0, 0.0, 1.0)
    # An AC-coupled battery: one round trip, split equally.
    assert AC_BATTERY.discharge_efficiency == pytest.approx(math.sqrt(AC_RT))
    assert AC_BATTERY.charge_efficiency == pytest.approx(math.sqrt(AC_RT))
    assert AC_BATTERY.stored_kwh(1.0, 1.0, 1.0) == pytest.approx(math.sqrt(AC_RT))


def test_the_sun_round_trip_must_be_a_share() -> None:
    with pytest.raises(PlanInputError):
        replace(AC_BATTERY, sun_round_trip_efficiency=1.2).validate()


def test_the_sun_never_fills_the_battery_past_its_ceiling() -> None:
    slot = _slot(0, buy=60, sell=50, house=0.0, sun=5.0)
    soc = DC_BATTERY.max_kwh - 1.0
    ran = run_slot(slot, DC_BATTERY, soc, ACTION_HOLD, action_power(slot, DC_BATTERY, soc, ACTION_HOLD))
    assert ran.soc_end_pct == pytest.approx(100.0)
    # The charge is held to what fits, the rest sold.
    assert ran.battery_kw == pytest.approx(1.0 / DC_BATTERY.sun_charge_efficiency)
    assert ran.grid_kw == pytest.approx(-(5.0 - ran.battery_kw))


# ---- the facts


def _facts(power_dc: bool, **kwargs) -> BatteryFacts:
    return BatteryFacts(
        battery_entity_id="sensor.battery_power",
        soc_pct=50.0,
        capacity_kwh=10.0,
        rated_charge_kw=5.0,
        rated_discharge_kw=5.0,
        power_dc=power_dc,
        **kwargs,
    )


def test_a_dc_battery_gets_the_sun_round_trip_from_the_learnt_one() -> None:
    learned = LearnedBattery(conversion=ConversionLoss(charge_loss=LOSS[0], discharge_loss=LOSS[1]))
    learned.round_trip.efficiency = 0.967
    facts = with_learned(_facts(True), learned)
    assert facts.round_trip_efficiency == pytest.approx(0.872, abs=0.002)
    assert facts.sun_round_trip_efficiency == pytest.approx(0.967, abs=0.002)
    assert facts.sun_round_trip_efficiency <= 0.967
    assert facts.discharge_loss == LOSS[1]
    params, _reason, _estimates = battery_params(facts)
    assert params is not None and params.sun_round_trip_efficiency == facts.sun_round_trip_efficiency
    assert params.discharge_loss == LOSS[1]


def test_a_dc_battery_not_yet_learnt_takes_the_assumed_loss() -> None:
    facts = with_learned(_facts(True), LearnedBattery())
    assert facts.round_trip_efficiency is None
    # The assumed 90 % turned back with the assumed 3 % in and 5 % out (0.976), held to 0.97 until learnt.
    assert facts.sun_round_trip_efficiency == pytest.approx(0.97)
    assert facts.discharge_loss == 0.05


def test_an_ac_coupled_battery_has_one_round_trip() -> None:
    learned = LearnedBattery()
    learned.round_trip.efficiency = 0.92
    facts = with_learned(_facts(False), learned)
    assert facts.sun_round_trip_efficiency is None
    params, _reason, _estimates = battery_params(facts)
    assert params is not None and params.sun_round_trip_efficiency is None and params.discharge_loss == 0.0
    assert params.sun_charge_efficiency == params.charge_efficiency == params.one_way_efficiency
    assert params.sun_charge_cap_kw == params.max_charge_kw


# ---- the plan


def test_a_summer_day_on_a_dc_battery_stores_more_sun_and_values_it_higher() -> None:
    slots = _summer()
    before = plan_battery(_request(slots, AC_BATTERY, allow_export_discharge=True))
    after = plan_battery(_request(slots, DC_BATTERY, allow_export_discharge=True))
    assert _charged(after.plan.decisions) > _charged(before.plan.decisions) + 1.0
    assert after.plan.cost < before.plan.cost
    # The baseline (self-consumption stores the surplus either way) is cheaper too: the stored sun gives more back.
    assert after.baseline.cost < before.baseline.cost
    # At the AC round trip, selling the sun beat storing it; at the sun's, storing it pays.
    assert _charged(before.plan.decisions) < 1.0
    assert all(d.action != ACTION_GRID_CHARGE for d in after.plan.decisions[:8])


def test_a_winter_day_without_surplus_is_unchanged() -> None:
    slots = _winter()
    before = plan_battery(_request(slots, AC_BATTERY))
    after = plan_battery(_request(slots, DC_BATTERY))
    # The same actions and about the same money; only the stored kWh are counted as the cells hold them (the frame,
    # `test_the_plans_stored_energy_is_the_cells_on_every_path`: about 1.6 % less energy through a full span).
    assert [d.action for d in after.plan.decisions] == [d.action for d in before.plan.decisions]
    assert after.plan.cost == pytest.approx(before.plan.cost, rel=0.01)
    assert after.savings == pytest.approx(before.savings, rel=0.03)


def test_an_ac_coupled_battery_plans_as_before() -> None:
    slots = _summer()
    plain = BatteryParams(10.0, 5.0, 5.0, min_soc_pct=10.0, round_trip_efficiency=AC_RT)
    again = replace(plain, sun_round_trip_efficiency=None)
    assert plan_battery(_request(slots, plain)).plan == plan_battery(_request(slots, again)).plan


def test_the_review_runs_the_plan_as_it_was_planned() -> None:
    """The review's replay (`hindsight.simulate`) of the plan's actions lands where the plan said, sun and all."""
    slots = _summer()
    made = plan_battery(_request(slots, DC_BATTERY, allow_export_discharge=True))
    ran = simulate(slots, DC_BATTERY, 10.0, [(d.action, d.battery_kw) for d in made.plan.decisions])
    assert ran.end_soc_pct == pytest.approx(made.plan.end_soc_pct, abs=0.01)
    assert ran.cost == pytest.approx(made.plan.cost, abs=0.01)


def test_the_perfect_plan_stores_the_sun_at_its_own_round_trip() -> None:
    slots = _summer()
    before = perfect_plans(slots, AC_BATTERY, 10.0, wear=0.0, end_value=0.0, modes=("max_profit",))["max_profit"]
    after = perfect_plans(slots, DC_BATTERY, 10.0, wear=0.0, end_value=0.0, modes=("max_profit",))["max_profit"]
    assert after.cost < before.cost
    assert after.work.discharged_kwh > before.work.discharged_kwh


def test_a_grid_charge_beside_the_sun_pays_the_ac_round_trip_for_its_own_part() -> None:
    slot = _slot(0, buy=10, sell=5, house=0.0, sun=1.0)
    ran = run_slot(slot, DC_BATTERY, 2.0, ACTION_GRID_CHARGE, 3.0)
    stored = (ran.soc_end_pct / 100 * DC_BATTERY.usable_kwh) - 2.0
    assert stored == pytest.approx(1.0 * DC_BATTERY.sun_charge_efficiency + 2.0 * DC_BATTERY.charge_efficiency)
    assert ran.grid_kw == pytest.approx(2.0)
    selfc = run_slot(slot, AC_BATTERY, 2.0, ACTION_SELF_CONSUME, 1.0)
    assert (selfc.soc_end_pct / 100 * 10.0) - 2.0 == pytest.approx(AC_BATTERY.one_way_efficiency)


def test_the_ac_side_count_of_a_sun_charge_gives_back_what_the_cells_do() -> None:
    """A sun charge read at the battery (DC) is counted on the AC side at the way-out loss (`ac_kw`); stored and given
    back at the sun's round trip, the plan returns what the cells really give the house at the DC round trip."""
    dc_charge, dc_rt = 2.0, DC_RT
    counted = ac_kw(dc_charge, LOSS, grid_kw=-1.0)
    given_back_by_plan = DC_BATTERY.stored_kwh(counted, counted, 1.0) * DC_BATTERY.discharge_efficiency
    given_back_by_cells = dc_charge * dc_rt * (1 - LOSS[1])
    assert given_back_by_plan == pytest.approx(given_back_by_cells, rel=1e-3)


#: The owner's battery: 40.3 kWh of cells, 13.2 kW rated at the battery, kept between 10 and 100 %.
OWNER = BatteryParams(
    40.3, 13.2, 13.2, min_soc_pct=10.0, round_trip_efficiency=AC_RT, sun_round_trip_efficiency=SUN_RT,
    discharge_loss=LOSS[1],
)


def test_the_plans_stored_energy_is_the_cells_on_every_path() -> None:
    """Per AC kWh: a grid charge stores √DC/(1+c), a sun charge √DC/(1-d), a stored kWh gives √DC·(1-d) (the cells'
    way, then the inverter's); the plan's frame is the cells', not a battery a few percent bigger."""
    c, d = LOSS
    root = math.sqrt(DC_RT)
    assert OWNER.charge_efficiency == pytest.approx(root / (1 + c), rel=2e-3)
    assert OWNER.sun_charge_efficiency == pytest.approx(root / (1 - d), rel=1e-3)
    assert OWNER.discharge_efficiency == pytest.approx(root * (1 - d), rel=1e-3)


def test_six_kw_of_sun_from_ten_percent_climbs_as_the_cells_do() -> None:
    """The review's sunny day: 6 kW of AC surplus from 10 %, hour by hour, against a cell model."""
    root = math.sqrt(DC_RT)
    soc = OWNER.min_kwh
    for hour in range(6):
        slot = _slot(hour, buy=60, sell=50, house=0.0, sun=6.0)
        ran = run_slot(slot, OWNER, soc, ACTION_HOLD, action_power(slot, OWNER, soc, ACTION_HOLD))
        soc = ran.soc_end_pct / 100 * OWNER.usable_kwh
        cells = min(100.0, 10.0 + 6.0 * (hour + 1) / (1 - LOSS[1]) * root / 40.3 * 100)
        assert ran.soc_end_pct == pytest.approx(cells, abs=0.05)
    assert soc == pytest.approx(OWNER.max_kwh)
    # What the full span gives back on the AC side: the cells' way out and the inverter's.
    assert (OWNER.max_kwh - OWNER.min_kwh) * OWNER.discharge_efficiency == pytest.approx(
        36.27 * root * (1 - LOSS[1]), abs=0.01
    )


def test_the_sun_charges_no_faster_than_the_rating_at_the_battery() -> None:
    """13.2 kW is the rating at the battery: 13.2 × (1 - d) of AC surplus at most, the rest sold."""
    slot = _slot(0, buy=60, sell=50, house=0.0, sun=15.0)
    soc = OWNER.min_kwh
    power = action_power(slot, OWNER, soc, ACTION_HOLD)
    assert power == pytest.approx(13.2 * (1 - LOSS[1]))
    ran = run_slot(slot, OWNER, soc, ACTION_HOLD, power)
    stored = ran.soc_end_pct / 100 * OWNER.usable_kwh - soc
    assert stored == pytest.approx(13.2 * math.sqrt(DC_RT), rel=1e-3)
    assert ran.grid_kw == pytest.approx(-(15.0 - power))
    # A grid charge beside it takes no more than the sun's cap; below the cap the grid tops up to the rating.
    assert action_power(slot, OWNER, soc, ACTION_GRID_CHARGE, 13.2) == pytest.approx(13.2 * (1 - LOSS[1]))
    small = _slot(0, buy=60, sell=50, house=0.0, sun=4.0)
    assert action_power(small, OWNER, soc, ACTION_GRID_CHARGE, 13.2) == pytest.approx(13.2)
