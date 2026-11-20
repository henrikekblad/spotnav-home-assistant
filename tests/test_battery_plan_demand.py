"""The battery plan with a demand charge priced in (`battery/plan.py`, plan v3_power_tariff step 5): the soft ceiling's
penalty per slot, the shave action, and nothing changed where no part is priced."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.plan import (
    _candidates,
    action_power,
    objective,
    plan_battery,
    plan_mode,
    self_consumption,
)
from custom_components.spotnav.battery.plan_model import (
    ACTION_GRID_CHARGE,
    ACTION_SHAVE,
    MODE_DEFS,
    BatteryParams,
    PlanInputError,
    PlanRequest,
    PlanSlot,
)

T0 = datetime(2026, 11, 2, tzinfo=timezone.utc)
# A lossless battery keeps the energy sums exact.
BATTERY = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=0, round_trip_efficiency=1.0)
# Göteborg's tidsindelad part: 135 kr per kW over the mean of three days, 45 kr (4 500 öre) per kW of a day's peak.
LAMBDA = 4500.0


def _slots(buy: list[float], house: float | list[float], soft: float | None, price: float = LAMBDA) -> tuple[PlanSlot, ...]:
    return tuple(
        PlanSlot(
            start=T0 + timedelta(hours=i),
            end=T0 + timedelta(hours=i + 1),
            buy=value,
            sell=value - 60,
            house_kw=house[i] if isinstance(house, list) else house,
            soft_cap_kw=soft,
            peak_price=price if soft is not None else 0.0,
        )
        for i, value in enumerate(buy)
    )


def _random_slots(rng: random.Random, n: int = 96) -> tuple[PlanSlot, ...]:
    return tuple(
        PlanSlot(
            start=T0 + timedelta(minutes=15 * i),
            end=T0 + timedelta(minutes=15 * (i + 1)),
            buy=rng.uniform(50, 400),
            sell=rng.uniform(-10, 300),
            house_kw=rng.uniform(0.3, 6),
            solar_kw=max(0.0, rng.uniform(-3, 8)),
            car_kw=rng.choice([0, 0, 0, 7.4]),
            import_cap_kw=rng.choice([None, 13.8, 9.0]),
            export_cap_kw=rng.choice([None, 10.0]),
        )
        for i in range(n)
    )


def _dump(plan) -> str:
    return json.dumps(plan.as_dict(), sort_keys=True)


@pytest.mark.parametrize("seed", range(6))
def test_without_a_priced_part_the_plan_is_byte_identical(seed: int) -> None:
    """A soft ceiling at price 0 (a part with λ = 0: summer, no price) is no ceiling at all, for every mode."""
    rng = random.Random(seed)
    slots = _random_slots(rng)
    battery = BatteryParams(usable_kwh=rng.uniform(5, 40), max_charge_kw=rng.uniform(2, 13), max_discharge_kw=rng.uniform(2, 13))
    bare = PlanRequest(slots=slots, battery=battery, soc_now_pct=rng.uniform(0, 100))
    unpriced = replace(bare, slots=tuple(replace(slot, soft_cap_kw=rng.uniform(0, 4), peak_price=0.0) for slot in slots))
    for name in ("balanced", "max_profit"):
        a = plan_mode(bare, MODE_DEFS[name], 15.0, keep=("hold", 0.0), keep_min_gain=5.0)
        b = plan_mode(unpriced, MODE_DEFS[name], 15.0, keep=("hold", 0.0), keep_min_gain=5.0)
        assert _dump(a) == _dump(b)
        assert "demand" not in a.as_dict()
        assert all(decision.action != ACTION_SHAVE for decision in b.plan.decisions)


def test_shave_spreads_the_battery_over_the_peak_hours() -> None:
    # Four hours of 6 kW against a 4 kW ceiling and 8 kWh stored: covering the house in full empties the battery in
    # 1⅓ h and the rest of the evening imports 6 kW; shaving 2 kW an hour keeps every hour at the ceiling.
    slots = _slots([200] * 4, 6.0, soft=4.0)
    result = plan_battery(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=80, cycle_cost=0, end_value=0))
    decisions = result.plan.decisions
    # The last hour's 2 kWh left cover it to the ceiling as self-consumption does: the same power, the earlier action.
    assert [d.action for d in decisions[:3]] == [ACTION_SHAVE] * 3
    assert all(d.grid_kw == pytest.approx(4.0) for d in decisions)
    assert result.plan.demand == pytest.approx(0.0)
    # Self-consumption on the same slots: 5 and 3 kW from the battery, then two hours of 6 kW, 2 kW above the ceiling.
    assert result.baseline.demand == pytest.approx(2 * 2 * LAMBDA)


def test_the_penalty_is_reported_apart_from_the_money_and_in_the_objective() -> None:
    slots = _slots([200] * 2, 6.0, soft=4.0)
    run = self_consumption(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=0))
    # An empty battery: both hours import 6 kW, 2 kW above the ceiling.
    assert run.demand == pytest.approx(2 * 2 * LAMBDA)
    assert run.cost == pytest.approx(2 * 6 * 200)
    assert [d.demand for d in run.decisions] == pytest.approx([2 * LAMBDA, 2 * LAMBDA])
    assert objective(run, BATTERY, 0.0) == pytest.approx(run.cost + run.wear + run.demand)
    assert run.decisions[0].as_dict()["demand"] == pytest.approx(2 * LAMBDA)


def test_the_grid_charge_stays_under_the_ceiling_in_cheap_hours() -> None:
    # Cheap hours, then dear ones: without the ceiling the plan charges at full power (house 1 + 5 kW = 6 kW import);
    # under a 3 kW ceiling it charges at the share that keeps the import under it.
    buy = [20] * 4 + [400] * 4
    free = plan_battery(PlanRequest(slots=_slots(buy, 1.0, soft=None), battery=BATTERY, soc_now_pct=0, cycle_cost=0))
    assert max(d.grid_kw for d in free.plan.decisions[:4]) == pytest.approx(6.0)
    held = plan_battery(PlanRequest(slots=_slots(buy, 1.0, soft=3.0), battery=BATTERY, soc_now_pct=0, cycle_cost=0))
    charges = [d for d in held.plan.decisions[:4] if d.action == ACTION_GRID_CHARGE]
    assert charges
    assert all(d.grid_kw <= 3.0 + 1e-9 for d in held.plan.decisions)
    assert held.plan.demand == pytest.approx(0.0)


def test_a_small_price_lets_the_plan_pass_the_ceiling() -> None:
    # A ceiling that costs 1 öre per kW is cheaper than giving up the spread: the plan charges at full power as before.
    # Two cheap hours fill the battery only at full power.
    buy = [20] * 2 + [400] * 4
    plan = plan_battery(PlanRequest(slots=_slots(buy, 1.0, soft=3.0, price=1.0), battery=BATTERY, soc_now_pct=0, cycle_cost=0))
    assert max(d.grid_kw for d in plan.plan.decisions[:2]) == pytest.approx(6.0)
    assert plan.plan.demand > 0


def test_shave_is_offered_only_above_a_priced_ceiling() -> None:
    slot = _slots([200], 6.0, soft=4.0)[0]
    soc = 5.0
    offered = dict(_candidates(slot, BATTERY, soc, PlanRequest(slots=(slot,), battery=BATTERY, soc_now_pct=50)))
    assert offered[ACTION_SHAVE] == pytest.approx(-2.0)
    for other in (replace(slot, peak_price=0.0), replace(slot, soft_cap_kw=None), replace(slot, soft_cap_kw=7.0)):
        actions = dict(_candidates(other, BATTERY, soc, PlanRequest(slots=(other,), battery=BATTERY, soc_now_pct=50)))
        assert ACTION_SHAVE not in actions


def test_shave_replayed_takes_the_planned_kw_and_never_exports() -> None:
    slot = _slots([200], 3.0, soft=None)[0]
    assert action_power(slot, BATTERY, 5.0, ACTION_SHAVE, -2.0) == pytest.approx(-2.0)
    assert action_power(slot, BATTERY, 5.0, ACTION_SHAVE, -4.5) == pytest.approx(-3.0)
    # Nothing to shave and a sun surplus: stored, as a hold stores it.
    sunny = replace(slot, solar_kw=5.0)
    assert action_power(sunny, BATTERY, 5.0, ACTION_SHAVE) == pytest.approx(2.0)


def test_a_negative_peak_price_or_ceiling_is_refused() -> None:
    slot = _slots([200], 1.0, soft=2.0)[0]
    with pytest.raises(PlanInputError):
        replace(slot, peak_price=-1.0).validate()
    with pytest.raises(PlanInputError):
        replace(slot, soft_cap_kw=-1.0).validate()
