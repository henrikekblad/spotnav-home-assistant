"""The house battery plan: what to do with the battery in each slot of the next 24-48 h, and what it saves.

Pure: no `hass`, no clock, no storage. A shadow: it plans and prices, it writes nothing (wave 1 of 3.0).

* **Search.** Dynamic programming over the stored energy, backwards from the horizon. The stored energy between the
  floor (`min_soc_pct`) and the ceiling (`max_soc_pct`) is cut into `soc_steps` steps; the value of the energy a slot
  ends with is read off that grid by linear interpolation, so an action need not land on a step. Forwards, the plan
  then follows the exact energy from `soc_now_pct`, picking in each slot the action with the least cost now plus
  value-to-go.
* **Actions** (`plan_model.ACTIONS`): self-consumption, grid charge at a share of the rated charge power, hold (no
  discharge), discharge to the house only, and, behind `allow_export_discharge`, discharge for export. Actions that
  come to the same battery power are tried once, in that order, and a tie keeps the earlier one: the plan only
  leaves self-consumption for a real gain.
* **Bounds.** Never below the floor or above the ceiling; the AC power within the rated charge and discharge power
  and within what the energy left allows. Never an import above the slot's cap: an action that would break it is cut
  back (less charge, or more discharge), and a slot where even a full discharge cannot keep under it is marked
  `over_cap` (the house breaks the cap there, never the plan). An export above its cap is cut back the same way; sun
  that still does not fit is curtailed.
* **Money.** Import at `buy`, export at `sell`. The cycle cost (`cycle_cost` per kWh charged or discharged) steers the
  search against churn but is reported apart (`wear`), never as money.
* **The horizon's end.** A kWh still stored at the end is worth `end_value` (default: the horizon's median buy price
  times the round trip), so the plan neither empties the battery nor fills it for nothing at the horizon.
* **Baseline.** The same slots with self-consumption only; `BatteryPlan.savings` is the difference with the end
  values counted.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from ..planning.planner import FiscalChoice, effective_minor_per_kwh
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    BatteryParams,
    BatteryPlan,
    PlanRequest,
    PlanRun,
    PlanSlot,
    SlotDecision,
)

#: Two battery powers closer than this are the same action (kW).
_SAME_KW = 1e-6
#: A gain smaller than this does not move the plan off an earlier action (minor units).
_TIE = 1e-6


class _Outcome(NamedTuple):
    action: str
    battery_kw: float
    grid_kw: float
    soc_end_kwh: float
    cost: float
    wear: float
    over_cap: bool
    curtailed_kwh: float


def buy_prices(spot_major_per_kwh: Sequence[float], starts: Sequence[datetime], fiscal: FiscalChoice) -> list[float]:
    """The import price per slot as SpotNav plans a charger against it (minor per kWh, every fee and VAT)."""
    return [effective_minor_per_kwh(spot, fiscal, start) for spot, start in zip(spot_major_per_kwh, starts)]


def sell_prices(spot_major_per_kwh: Sequence[float], credit_minor_per_kwh: float = 0.0) -> list[float]:
    """What an exported kWh earns (minor per kWh): the spot price plus a fixed credit (negative for a fee)."""
    return [spot * 100 + credit_minor_per_kwh for spot in spot_major_per_kwh]


def default_end_value(slots: Sequence[PlanSlot], battery: BatteryParams) -> float:
    """A kWh left at the horizon: the horizon's median buy price, less the round trip's loss to use it later."""
    return statistics.median(slot.buy for slot in slots) * battery.round_trip_efficiency


def _limits(slot: PlanSlot, battery: BatteryParams, soc_kwh: float) -> tuple[float, float]:
    """The AC charge and discharge power open in [slot] at [soc_kwh]: rated power, bounded by the energy left."""
    hours = slot.hours
    eff = battery.one_way_efficiency
    charge_lim = max(0.0, min(battery.max_charge_kw, (battery.max_kwh - soc_kwh) / (eff * hours)))
    discharge_lim = max(0.0, min(battery.max_discharge_kw, (soc_kwh - battery.min_kwh) * eff / hours))
    return charge_lim, discharge_lim


def action_power(
    slot: PlanSlot, battery: BatteryParams, soc_kwh: float, action: str, charge_kw: float = 0.0
) -> float:
    """The AC battery power [action] asks for in [slot] at [soc_kwh], before the grid's caps.

    Every action but a grid charge follows the slot's load and sun, so a plan made on a forecast is run against
    what really happens by calling this with the real slot. A grid charge asks for [charge_kw] (or the sun's whole
    surplus, if more).
    """
    charge_lim, discharge_lim = _limits(slot, battery, soc_kwh)
    net = slot.net_kw
    surplus = max(0.0, -net)
    deficit = max(0.0, net)
    if action == ACTION_SELF_CONSUME:
        return min(surplus, charge_lim) if net <= 0 else -min(deficit, discharge_lim)
    if action == ACTION_HOLD:
        return min(surplus, charge_lim)
    if action == ACTION_DISCHARGE_HOUSE:
        return -min(deficit, discharge_lim)
    if action == ACTION_GRID_CHARGE:
        return min(charge_lim, max(charge_kw, surplus))
    if action == ACTION_EXPORT_DISCHARGE:
        return -discharge_lim
    raise ValueError(f"unknown action {action!r}")


def _candidates(
    slot: PlanSlot, battery: BatteryParams, soc_kwh: float, request: PlanRequest
) -> list[tuple[str, float]]:
    """The actions open in [slot] at [soc_kwh], as (action, wanted AC battery power), duplicates dropped."""
    wanted = [
        (action, action_power(slot, battery, soc_kwh, action))
        for action in (ACTION_SELF_CONSUME, ACTION_HOLD, ACTION_DISCHARGE_HOUSE)
    ]
    for share in request.charge_shares:
        power = action_power(slot, battery, soc_kwh, ACTION_GRID_CHARGE, share * battery.max_charge_kw)
        wanted.append((ACTION_GRID_CHARGE, power))
    if request.allow_export_discharge:
        wanted.append((ACTION_EXPORT_DISCHARGE, action_power(slot, battery, soc_kwh, ACTION_EXPORT_DISCHARGE)))

    out: list[tuple[str, float]] = []
    for action, power in wanted:
        if all(abs(power - seen) > _SAME_KW for _, seen in out):
            out.append((action, power))
    return out


def _apply(
    slot: PlanSlot, battery: BatteryParams, soc_kwh: float, action: str, power: float, cycle_cost: float
) -> _Outcome:
    """Run [action] at [power] for one slot, with the grid's caps enforced, and price it."""
    hours = slot.hours
    eff = battery.one_way_efficiency
    _charge_lim, discharge_lim = _limits(slot, battery, soc_kwh)
    net = slot.net_kw
    over_cap = False
    curtailed = 0.0

    grid = net + power
    cap = slot.import_cap_kw
    if cap is not None and grid > cap:
        power = max(power - (grid - cap), -discharge_lim)
        grid = net + power
        over_cap = grid > cap + _SAME_KW
    cap = slot.export_cap_kw
    if cap is not None and grid < -cap:
        if power < 0:
            power = min(0.0, power + (-cap - grid))
            grid = net + power
        if grid < -cap:
            curtailed = (-cap - grid) * hours
            grid = -cap

    stored = power * eff if power > 0 else power / eff
    soc_end = min(battery.max_kwh, max(min(soc_kwh, battery.min_kwh), soc_kwh + stored * hours))
    if grid >= 0:
        cost = grid * hours * slot.buy
    else:
        cost = grid * hours * slot.sell
    wear = abs(power) * hours * cycle_cost
    return _Outcome(action, power, grid, soc_end, cost, wear, over_cap, curtailed)


def run_slot(
    slot: PlanSlot, battery: BatteryParams, soc_kwh: float, action: str, power_kw: float, cycle_cost: float = 0.0
) -> SlotDecision:
    """[action] at [power_kw] run for one slot from [soc_kwh], with the grid's caps enforced: what it costs and
    where it leaves the battery (`soc_end_pct`). For replaying a plan against what really happened."""
    return _decision(slot, battery, _apply(slot, battery, soc_kwh, action, power_kw, cycle_cost))


class _Grid:
    """The stored-energy grid the search runs on, with linear interpolation between its points."""

    def __init__(self, battery: BatteryParams, steps: int) -> None:
        self.low = battery.min_kwh
        self.high = battery.max_kwh
        self.steps = steps
        self.step = (self.high - self.low) / steps
        self.points = [self.low + self.step * k for k in range(steps + 1)]

    def value(self, values: Sequence[float], soc_kwh: float) -> float:
        position = (soc_kwh - self.low) / self.step
        if position <= 0:
            return values[0]
        if position >= self.steps:
            return values[-1]
        k = int(position)
        frac = position - k
        return values[k] + (values[k + 1] - values[k]) * frac


def _best(
    slot: PlanSlot,
    request: PlanRequest,
    soc_kwh: float,
    after: Sequence[float],
    grid: _Grid,
) -> tuple[_Outcome, float]:
    battery = request.battery
    best: _Outcome | None = None
    best_total = 0.0
    for action, power in _candidates(slot, battery, soc_kwh, request):
        outcome = _apply(slot, battery, soc_kwh, action, power, request.cycle_cost)
        total = outcome.cost + outcome.wear + grid.value(after, outcome.soc_end_kwh)
        if best is None or total < best_total - _TIE:
            best, best_total = outcome, total
    assert best is not None  # self-consumption is always open
    return best, best_total


def _decision(slot: PlanSlot, battery: BatteryParams, outcome: _Outcome) -> SlotDecision:
    return SlotDecision(
        start=slot.start,
        end=slot.end,
        action=outcome.action,
        battery_kw=outcome.battery_kw,
        grid_kw=outcome.grid_kw,
        soc_end_pct=outcome.soc_end_kwh / battery.usable_kwh * 100,
        cost=outcome.cost,
        over_cap=outcome.over_cap,
        curtailed_kwh=outcome.curtailed_kwh,
    )


def _run(decisions: list[SlotDecision], outcomes: list[_Outcome], battery: BatteryParams, soc_kwh: float) -> PlanRun:
    return PlanRun(
        decisions=tuple(decisions),
        cost=sum(o.cost for o in outcomes),
        wear=sum(o.wear for o in outcomes),
        end_soc_pct=soc_kwh / battery.usable_kwh * 100,
    )


def self_consumption(request: PlanRequest) -> PlanRun:
    """The baseline: every slot left to self-consumption, with the same caps and prices."""
    request.validate()
    battery = request.battery
    soc = battery.usable_kwh * request.soc_now_pct / 100
    decisions: list[SlotDecision] = []
    outcomes: list[_Outcome] = []
    for slot in request.slots:
        action, power = _candidates(slot, battery, soc, request)[0]
        outcome = _apply(slot, battery, soc, action, power, request.cycle_cost)
        decisions.append(_decision(slot, battery, outcome))
        outcomes.append(outcome)
        soc = outcome.soc_end_kwh
    return _run(decisions, outcomes, battery, soc)


def objective(run: PlanRun, battery: BatteryParams, end_value: float) -> float:
    """What the search minimises over a run: its money and wear, less the stored energy it leaves at `end_value`."""
    return run.cost + run.wear - run.end_kwh(battery) * end_value


def plan_battery(
    request: PlanRequest, *, keep: tuple[str, float] | None = None, keep_min_gain: float = 0.0
) -> BatteryPlan:
    """The cheapest way through the horizon, and the baseline it is measured against.

    `keep` is the action (and, for a grid charge, its kW) a plan made before has for the first slot, the one running
    now. The new plan replaces it only when it is better by more than `keep_min_gain` (minor units, on the
    objective): a running action does not flip for a few öre (Predbat's `metric_min_improvement`). The plan then
    says whether it kept the action (`kept`) and what the new one would have gained (`keep_gain`).
    """
    request.validate()
    battery = request.battery
    end_value = request.end_value if request.end_value is not None else default_end_value(request.slots, battery)
    grid = _Grid(battery, request.soc_steps)

    # Backwards: the value-to-go at each grid point, from the horizon's end value.
    values: list[list[float]] = [[] for _ in range(len(request.slots) + 1)]
    values[-1] = [-end_value * point for point in grid.points]
    for index in range(len(request.slots) - 1, -1, -1):
        slot = request.slots[index]
        after = values[index + 1]
        values[index] = [_best(slot, request, point, after, grid)[1] for point in grid.points]

    best = _forward(request, values, grid)
    kept = False
    keep_gain: float | None = None
    if keep is not None:
        held = _forward(request, values, grid, first=keep)
        keep_gain = objective(held, battery, end_value) - objective(best, battery, end_value)
        if keep_gain <= keep_min_gain:
            best, kept = held, True
    return BatteryPlan(
        plan=best,
        baseline=self_consumption(request),
        end_value=end_value,
        battery=battery,
        kept=kept,
        keep_gain=keep_gain,
    )


def _forward(
    request: PlanRequest, values: list[list[float]], grid: _Grid, first: tuple[str, float] | None = None
) -> PlanRun:
    """Forwards: the exact energy from now, each slot's best action against the value-to-go; with `first`, the first
    slot runs that action (at its kW, for a grid charge) instead of its best."""
    battery = request.battery
    soc = battery.usable_kwh * request.soc_now_pct / 100
    decisions: list[SlotDecision] = []
    outcomes: list[_Outcome] = []
    for index, slot in enumerate(request.slots):
        if index == 0 and first is not None:
            action, charge_kw = first
            power = action_power(slot, battery, soc, action, charge_kw)
            outcome = _apply(slot, battery, soc, action, power, request.cycle_cost)
        else:
            outcome, _total = _best(slot, request, soc, values[index + 1], grid)
        decisions.append(_decision(slot, battery, outcome))
        outcomes.append(outcome)
        soc = outcome.soc_end_kwh
    return _run(decisions, outcomes, battery, soc)
