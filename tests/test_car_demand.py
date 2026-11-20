"""The car's plan under the site's soft demand ceiling, in shadow (`planning/car_demand.py`): the caps, the two passes
and what each plan costs against the ceiling. The planner's own requests from `test_planner_hour_caps` (a one-phase
16 A car, 3.68 kW, minimum 1.38 kW; 7 kWh by 07:00; cheap 17-20 and 01-05)."""

from __future__ import annotations

import pytest

from custom_components.spotnav.planning.car_demand import (
    CarCeiling,
    car_kw_by_hour,
    demand_caps,
    demand_cost_of,
    PAUSED_KW,
    plan_under_ceiling,
    shadow_facts,
)
from custom_components.spotnav.planning.demand_cost import HourCeiling
from custom_components.spotnav.planning.planner import calculate_plan

from .test_planner_hour_caps import CAR_KW, hours_of, request, TZ

#: Every hour from noon to 07:00.
ALL = hours_of(12, 12, 24) + hours_of(13, 0, 7)
EVENING = hours_of(12, 17, 20)


def inputs(ceiling_kw: float, hours: list, house_kw: float = 1.0, others: dict | None = None) -> CarCeiling:
    return CarCeiling(
        ceilings={start: HourCeiling(ceiling_kw, 16.0) for start in hours},
        house_kw={start: house_kw for start in ALL},
        others_kw=others or {},
        tz=TZ,
    )


def test_the_caps_are_the_ceiling_less_the_house_and_the_other_cars_never_above_the_cap_in_force() -> None:
    found = demand_caps(inputs(3.0, EVENING, others={EVENING[0]: 0.5}), {EVENING[1]: 1.5}, CAR_KW, 1.38)
    # 3 kW less the house's 1 kW, less another car's 0.5 kW in the first evening hour.
    assert found[EVENING[0]] == pytest.approx(1.5)
    assert found[EVENING[2]] == pytest.approx(2.0)
    # The import target's cap in force is lower: kept.
    assert found[EVENING[1]] == pytest.approx(1.5)
    # Room below the charger's minimum pauses the hour; a raise gives it back.
    assert demand_caps(inputs(2.0, EVENING), None, CAR_KW, 1.38)[EVENING[0]] == PAUSED_KW
    assert demand_caps(inputs(2.0, EVENING), None, CAR_KW, 1.38, raise_kw=0.5)[EVENING[0]] == pytest.approx(1.5)
    # Hours without a ceiling keep the caps in force only.
    assert set(demand_caps(inputs(3.0, EVENING), None, CAR_KW, 1.38)) == set(EVENING)


def test_no_priced_hour_no_plan_under_the_ceiling() -> None:
    assert plan_under_ceiling(request(), inputs(3.0, []), 1.38) is None
    free = CarCeiling({EVENING[0]: HourCeiling(3.0, 0.0)}, {EVENING[0]: 1.0}, {}, TZ)
    assert plan_under_ceiling(request(), free, 1.38) is None


def test_a_ceiling_with_room_moves_the_charge_without_a_raise_and_costs_less_than_the_plan_in_force() -> None:
    real = calculate_plan(request())
    held = inputs(3.0, EVENING)
    shadow = plan_under_ceiling(request(), held, 1.38)
    assert shadow is not None and shadow.fits and shadow.raise_kw == 0.0 and shadow.plans_made == 1
    # Under the ceiling no evening hour goes above 2 kW for the car.
    car = car_kw_by_hour(shadow.result, TZ)
    assert all(car.get(start, 0.0) <= 2.0 + 1e-9 for start in EVENING)
    assert shadow.demand_cost == 0.0
    # The plan in force charges the cheap evening at the car's full power: 3.68 + 1 - 3 = 1.68 kW over, 16 per kW.
    real_cost, over = demand_cost_of(real, held)
    assert real_cost == pytest.approx(16.0 * (CAR_KW + 1.0 - 3.0))
    facts = shadow_facts(shadow, real, held)
    assert facts["in_force"]["demand_cost"] == pytest.approx(round(real_cost, 2))
    assert facts["demand_cost"] == 0.0 and facts["fits"] and facts["energy_cost"] >= facts["in_force"]["energy_cost"]


def test_a_departure_the_ceiling_would_miss_raises_it_by_the_least_that_fits() -> None:
    # The ceiling at the house's own load in every hour: nothing left for the car.
    tight = inputs(1.0, ALL)
    shadow = plan_under_ceiling(request(), tight, 1.38)
    assert shadow is not None and shadow.fits
    # The least raise gives the charger its minimum (1.38 kW) somewhere; the halvings find it within a few percent.
    assert 1.38 <= shadow.raise_kw <= 1.38 + CAR_KW / 2**6 + 1e-9
    assert shadow.plans_made == 2 + 6
    assert shadow.demand_cost > 0
    # No day goes further over the ceiling than the raise.
    assert shadow.over_kw and all(kw <= shadow.raise_kw + 1e-6 for kw in shadow.over_kw.values())


def test_without_a_departure_nothing_is_raised() -> None:
    shadow = plan_under_ceiling(request(departure=None), inputs(1.0, ALL), 1.38)
    assert shadow is not None and shadow.raise_kw == 0.0 and shadow.plans_made == 1


def test_a_raise_that_cannot_help_is_the_ceiling_lifted_whole() -> None:
    # 40 kWh cannot fit by 07:00 at 3.68 kW: short whatever the ceiling.
    shadow = plan_under_ceiling(request(requested_kwh=80.0), inputs(1.0, ALL), 1.38)
    assert shadow is not None and not shadow.fits
    assert shadow.raise_kw == pytest.approx(CAR_KW) and shadow.plans_made == 2
    real = calculate_plan(request(requested_kwh=80.0))
    assert shadow.result.delivered_kwh == pytest.approx(real.delivered_kwh)
