"""The car's plan under the site's soft demand ceiling (plan v3_power_tariff §3, step 6), in shadow: pure, no Home
Assistant. Planned beside the plan in force, which it never changes; the charger's controller logs what it would do
and cost.

* **Caps** (`demand_caps`). In an hour with a priced soft ceiling (`demand_cost.HourCeiling`, S_h) the car counts on
  the ceiling less the house's expected load (the forecast the plan in force counts on, its p75) and the other cars'
  installed plans, never above the hour's cap in force (the import target's per hour, else the flat cap: H_h). Room
  below the charger's minimum is a paused hour (`PAUSED_KW`, the planner's `PAUSED_CAP_KW`: never charged in). Other
  hours keep the caps in force.
* **Two passes** (`plan_under_ceiling`). The capped plan; when it misses its departure, the least raise of the ceiling,
  the same over every counted hour, that is ready in time (`RAISE_STEPS` halvings between none and the raise that
  lifts every cap to the car's own power), at the cost of the day peaks it raises. Without a departure nothing is
  raised: the plan waits for cheaper room as the plan in force waits for prices.
* **Its cost** (`demand_cost_of`). Per local day, the hour with a ceiling where the house, the other cars and this
  plan raise the day's peak most above S_h, at λ_h per kW (`demand_cost.raise_cost`), added over the days. The plan
  in force is costed the same way, so the log shows what the ceiling saves and what its energy costs more.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Final

from .. import time_zones
from .demand_charge import hour_start
from .demand_cost import HourCeiling, raise_cost
from .planner import calculate_plan, PAUSED_CAP_KW, PlanRequest, PlanResult, SLOT_HOURS

#: The cap of an hour whose room is below the charger's minimum: the charge pauses (the planner needs a positive cap).
PAUSED_KW: Final = PAUSED_CAP_KW
#: How many times the raise is halved in the second pass.
RAISE_STEPS: Final = 6
HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class CarCeiling:
    """What the car's plan is held under: per hour start (UTC) the soft ceiling, the house's expected load and the
    other cars' planned power (kW)."""

    ceilings: Mapping[datetime, HourCeiling]
    house_kw: Mapping[datetime, float]
    others_kw: Mapping[datetime, float]
    tz: str


def demand_caps(
    inputs: CarCeiling,
    caps: Mapping[datetime, float] | None,
    flat_cap_kw: float | None,
    minimum_kw: float,
    raise_kw: float = 0.0,
) -> dict[datetime, float]:
    """The per-hour caps the car plans on under the ceiling raised by `raise_kw` (see the module)."""
    found = dict(caps or {})
    for start, ceiling in inputs.ceilings.items():
        house = inputs.house_kw.get(start)
        if ceiling.price <= 0 or house is None:
            continue
        room = ceiling.kw + raise_kw - house - inputs.others_kw.get(start, 0.0)
        cap = room if room >= minimum_kw else PAUSED_KW
        hard = found.get(start, flat_cap_kw)
        found[start] = max(PAUSED_KW, cap if hard is None else min(cap, hard))
    return found


def car_kw_by_hour(result: PlanResult, tz: str) -> dict[datetime, float]:
    """The plan's mean power per clock hour (the hour's start on `tz`'s clock, UTC)."""
    by_hour: dict[datetime, float] = {}
    per_slot = [
        result.slot_kwh[index] if result.slot_kwh and len(result.slot_kwh) == len(result.slots) else result.power_kw
        * SLOT_HOURS
        for index in range(len(result.slots))
    ]
    for slot, kwh in zip(result.slots, per_slot, strict=True):
        start = hour_start(slot.start, tz)
        by_hour[start] = by_hour.get(start, 0.0) + kwh
    return by_hour


def demand_cost_of(result: PlanResult | None, inputs: CarCeiling) -> tuple[float, dict[date, float]]:
    """What the plan adds to the month's demand charge against the ceiling (the area's major unit), and per day the
    highest hour over it (kW) where it costs something (see the module)."""
    zone = time_zones.require_zone(inputs.tz)
    car = {} if result is None or not result.has_plan else car_kw_by_hour(result, inputs.tz)
    worst: dict[date, tuple[float, float]] = {}
    for start, ceiling in inputs.ceilings.items():
        house = inputs.house_kw.get(start)
        if house is None or ceiling.price <= 0:
            continue
        kw = house + inputs.others_kw.get(start, 0.0) + car.get(start, 0.0)
        cost = raise_cost(ceiling, kw)
        day = ceiling.day or start.astimezone(zone).date()
        if cost > worst.get(day, (0.0, 0.0))[0]:
            worst[day] = (cost, kw - ceiling.kw)
    return sum(cost for cost, _over in worst.values()), {day: over for day, (cost, over) in worst.items()}


@dataclass(frozen=True, slots=True)
class CarDemandPlan:
    """The car's plan under the ceiling: the plan, the ceiling's raise it needed (kW) and its demand cost."""

    result: PlanResult
    raise_kw: float
    demand_cost: float
    over_kw: Mapping[date, float]
    plans_made: int

    @property
    def fits(self) -> bool:
        return _fits(self.result)


def _fits(result: PlanResult) -> bool:
    """Ready by the departure under the caps: a plan whose caps were set aside (`per_hour_caps_dropped`) is no plan
    under the ceiling, so it never ends the raise search."""
    return result.has_plan and not result.short_of_deadline and not result.per_hour_caps_dropped


def plan_under_ceiling(
    request: PlanRequest,
    inputs: CarCeiling,
    minimum_kw: float,
    plan: Callable[[PlanRequest], PlanResult] = calculate_plan,
) -> CarDemandPlan | None:
    """The car's plan under the ceiling in two passes (see the module); `None` when no hour of its horizon has a
    priced ceiling (the plan in force is then the plan under it)."""
    if not any(ceiling.price > 0 and start in inputs.house_kw for start, ceiling in inputs.ceilings.items()):
        return None
    caps = request.power_cap_kw_by_hour
    flat = request.planned_kw

    def under(raise_kw: float) -> PlanResult:
        return plan(replace(request, power_cap_kw_by_hour=demand_caps(inputs, caps, flat, minimum_kw, raise_kw)))

    made = 1
    chosen, raised = under(0.0), 0.0
    if not _fits(chosen) and request.departure is not None:
        # The raise that lifts every counted hour's cap to the car's own power: the ceiling no longer holds anything.
        top = max(
            flat + inputs.house_kw.get(start, 0.0) + inputs.others_kw.get(start, 0.0) - ceiling.kw
            for start, ceiling in inputs.ceilings.items()
        )
        top = max(top, 0.0)
        high = under(top)
        made += 1
        if _fits(high):
            low_kw, high_kw, best = 0.0, top, high
            for _step in range(RAISE_STEPS):
                middle = (low_kw + high_kw) / 2
                found = under(middle)
                made += 1
                if _fits(found):
                    high_kw, best = middle, found
                else:
                    low_kw = middle
            chosen, raised = best, high_kw
        else:
            chosen, raised = high, top
    cost, over = demand_cost_of(chosen, inputs)
    return CarDemandPlan(result=chosen, raise_kw=raised, demand_cost=cost, over_kw=over, plans_made=made)


def shadow_facts(shadow: CarDemandPlan, real: PlanResult, inputs: CarCeiling) -> dict[str, Any]:
    """The diagnostics' and the log's view: what the plan under the ceiling would do and cost against the plan in
    force (the area's major unit)."""
    real_cost, real_over = demand_cost_of(real, inputs)
    result = shadow.result
    energy = result.estimated_cost if result.has_plan else None
    return {
        "reason": result.reason,
        "fits": shadow.fits,
        "raise_kw": round(shadow.raise_kw, 3),
        "plans_made": shadow.plans_made,
        # The search under the caps was too large: the plan is the one without them (`per_hour_caps_dropped`).
        "caps_dropped": result.per_hour_caps_dropped,
        "delivered_kwh": round(result.delivered_kwh, 3),
        "periods": [[start.isoformat(), end.isoformat()] for start, end in result.periods],
        "energy_cost": None if energy is None else round(energy, 2),
        "demand_cost": round(shadow.demand_cost, 2),
        "over_kw": {day.isoformat(): round(kw, 3) for day, kw in sorted(shadow.over_kw.items())},
        "in_force": {
            "energy_cost": round(real.estimated_cost, 2) if real.has_plan else None,
            "demand_cost": round(real_cost, 2),
            "over_kw": {day.isoformat(): round(kw, 3) for day, kw in sorted(real_over.items())},
        },
    }
