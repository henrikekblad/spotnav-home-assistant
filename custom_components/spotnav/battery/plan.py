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
* **The demand charge** (`PlanSlot.soft_cap_kw`, `peak_price`, `peak_day`; `plans/v3_power_tariff.md` §3): the bill
  counts each day's highest hour, so the plan charges only what raises the day's peak: a slot whose import takes the
  day's highest so far (`PlanRequest.peak_now_kw` for the running day, else nothing) above both it and the slot's
  soft ceiling S costs `peak_price · (max(new peak, S) − max(old peak, S))`, on top of the energy (`PlanRun.demand`,
  reported apart like the wear but money). The search carries the day's peak as a second state from a day's second
  priced slot to its last, at `PlanRequest.peak_steps` steps between the day's lowest ceiling and the highest import
  its slots can reach, read off by linear interpolation as the energy is; forwards it follows the exact peak. A
  quarter's import stands for its hour's mean (the cautious side when the quarters of an hour differ). One action
  more in a priced slot, `ACTION_SHAVE`: discharge just enough to bring the grid down to the ceiling, or to the day's
  peak so far where that is higher. A request without a priced ceiling is planned exactly as before.
* **Money.** Import at `buy`, export at `sell`. The cycle cost (`cycle_cost` per kWh charged or discharged) steers the
  search against churn but is reported apart (`wear`), never as money; so does the spread gate's margin
  (`export_hurdle` per kWh the battery sells, reported as `hurdle`).
* **House first** (`export_reserve`, the spread gate's): a sale from the battery stops at what the house's deficit
  needs from the next slot until the next cheap hours (`house_reserve_kwh`: a slot whose buy price is among the
  horizon's cheapest `CHEAP_SHARE`), so only the surplus beyond it is sold.
* **Modes** (`plan_model.MODE_DEFS`, `plan_mode`): a mode sets the wear, and whether and how the battery may sell;
  Self-consumption does not plan at all (its plan is the baseline).
* **The horizon's end.** A kWh still stored at the end is worth `end_value` (default: the horizon's median buy price
  times the round trip), so the plan neither empties the battery nor fills it for nothing at the horizon.
* **Baseline.** The same slots with self-consumption only; `BatteryPlan.savings` is the difference with the end
  values counted.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import NamedTuple

from ..planning.planner import FiscalChoice, effective_minor_per_kwh
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    ACTION_SHAVE,
    DEFAULT_SELL_MARGIN,
    EXPORT_OFF,
    BatteryMode,
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
#: The next cheap hours for house first: a slot whose buy price is among this share of the horizon's cheapest.
CHEAP_SHARE = 0.25


class _Outcome(NamedTuple):
    action: str
    battery_kw: float
    grid_kw: float
    soc_end_kwh: float
    cost: float
    wear: float
    over_cap: bool
    curtailed_kwh: float
    hurdle: float = 0.0
    peak: float = 0.0


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
    """The AC charge and discharge power open in [slot] at [soc_kwh]: rated power, bounded by the energy left (the
    room counted with the sun's surplus charged first, at its own share, `BatteryParams.sun_charge_efficiency`)."""
    hours = slot.hours
    eff = battery.one_way_efficiency
    room = battery.charge_for_room(battery.max_kwh - soc_kwh, max(0.0, -slot.net_kw), hours)
    charge_lim = max(0.0, min(battery.max_charge_kw, room))
    discharge_lim = max(0.0, min(battery.max_discharge_kw, (soc_kwh - battery.min_kwh) * eff / hours))
    return charge_lim, discharge_lim


def action_power(
    slot: PlanSlot, battery: BatteryParams, soc_kwh: float, action: str, charge_kw: float = 0.0
) -> float:
    """The AC battery power [action] asks for in [slot] at [soc_kwh], before the grid's caps.

    Every action but a grid charge follows the slot's load and sun, so a plan made on a forecast is run against
    what really happens by calling this with the real slot. A grid charge asks for [charge_kw] (or the sun's whole
    surplus, if more). A discharge for export asks for all it can, or at most [charge_kw]'s size when one is given
    (the planned kW, which house first may have held below that). A shave discharges into the deficit above the
    slot's soft ceiling, or (replaying a plan, where [charge_kw] is the planned kW) as much as was planned, never more
    than the deficit; with nothing to shave it stores a sun surplus as a hold does.
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
        want = max(charge_kw, surplus)
        # What control would run: it commands the battery at most the cap in all (`gate.restricted`), the sun's
        # surplus included. Under the cap the charge is the cap; where the cap is not above the surplus it is the
        # surplus alone, a hold, and the search drops the duplicate. The sun's own charge is never held back.
        if slot.grid_charge_cap_kw is not None:
            want = max(surplus, min(want, slot.grid_charge_cap_kw))
        return min(charge_lim, want)
    if action == ACTION_EXPORT_DISCHARGE:
        return -min(discharge_lim, abs(charge_kw)) if charge_kw else -discharge_lim
    if action == ACTION_SHAVE:
        if charge_kw:
            want = abs(charge_kw)
        elif slot.soft_cap_kw is not None:
            want = max(0.0, net - slot.soft_cap_kw)
        else:
            want = 0.0
        return -min(deficit, want, discharge_lim) if want > 0 else min(surplus, charge_lim)
    raise ValueError(f"unknown action {action!r}")


def house_reserve_kwh(slots: Sequence[PlanSlot], battery: BatteryParams) -> list[float]:
    """Per slot: the stored kWh the house's deficit needs after it until the next cheap hours (the first later slot
    whose buy price is among the horizon's cheapest `CHEAP_SHARE`; the horizon's end when none), at most the
    battery's span. A slot's own deficit is not in it: a discharge for export covers that as it runs."""
    buys = sorted(slot.buy for slot in slots)
    cheap = buys[max(0, math.ceil(len(buys) * CHEAP_SHARE) - 1)] if buys else 0.0
    eff = battery.one_way_efficiency
    span = battery.max_kwh - battery.min_kwh
    out = [0.0] * len(slots)
    need = 0.0
    for index in range(len(slots) - 1, -1, -1):
        out[index] = min(span, need)
        slot = slots[index]
        need = 0.0 if slot.buy <= cheap else need + max(0.0, slot.net_kw) * slot.hours / eff
    return out


def _reserves(request: PlanRequest) -> list[float] | None:
    """House first's reserve per slot when the request keeps one, else `None`."""
    if request.allow_export_discharge and request.export_reserve:
        return house_reserve_kwh(request.slots, request.battery)
    return None


def _candidates(
    slot: PlanSlot, battery: BatteryParams, soc_kwh: float, request: PlanRequest, reserve: float | None = None
) -> list[tuple[str, float]]:
    """The actions open in [slot] at [soc_kwh], as (action, wanted AC battery power), duplicates dropped. With a
    [reserve] (house first), a discharge for export leaves at least that much stored above the floor, and is not
    open where that leaves nothing beyond the slot's own deficit."""
    wanted = [
        (action, action_power(slot, battery, soc_kwh, action))
        for action in (ACTION_SELF_CONSUME, ACTION_HOLD, ACTION_DISCHARGE_HOUSE)
    ]
    for share in request.charge_shares:
        power = action_power(slot, battery, soc_kwh, ACTION_GRID_CHARGE, share * battery.max_charge_kw)
        wanted.append((ACTION_GRID_CHARGE, power))
    if request.allow_export_discharge:
        power = action_power(slot, battery, soc_kwh, ACTION_EXPORT_DISCHARGE)
        if reserve is not None:
            room = max(0.0, (soc_kwh - battery.min_kwh - reserve) * battery.one_way_efficiency / slot.hours)
            power = max(power, -room)
        if reserve is None or -power > max(0.0, slot.net_kw) + _SAME_KW:
            wanted.append((ACTION_EXPORT_DISCHARGE, power))
    if slot.priced_ceiling and slot.net_kw > (slot.soft_cap_kw or 0.0):
        wanted.append((ACTION_SHAVE, action_power(slot, battery, soc_kwh, ACTION_SHAVE)))

    out: list[tuple[str, float]] = []
    for action, power in wanted:
        if all(abs(power - seen) > _SAME_KW for _, seen in out):
            out.append((action, power))
    return out


def _apply(
    slot: PlanSlot,
    battery: BatteryParams,
    soc_kwh: float,
    action: str,
    power: float,
    cycle_cost: float,
    export_hurdle: float = 0.0,
) -> _Outcome:
    """Run [action] at [power] for one slot, with the grid's caps enforced, and price it."""
    hours = slot.hours
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

    # A charge takes the sun's surplus first (the sun's round trip on a DC bus), the grid's for the rest.
    stored = battery.stored_kwh(power, -net, hours)
    soc_end = min(battery.max_kwh, max(min(soc_kwh, battery.min_kwh), soc_kwh + stored))
    if grid >= 0:
        cost = grid * hours * slot.buy
    else:
        cost = grid * hours * slot.sell
    wear = abs(power) * hours * cycle_cost
    # What the battery itself sold: its discharge beyond the site's own deficit.
    sold = max(0.0, min(-power, -grid)) * hours
    return _Outcome(action, power, grid, soc_end, cost, wear, over_cap, curtailed, sold * export_hurdle)


def _kept_action(slot: PlanSlot, action: str, power: float) -> str:
    """The action a kept first slot runs: a grid charge the slot's grid charge cap leaves nothing of (its power is the
    sun's surplus alone) is a hold, as control would run it."""
    if (
        action == ACTION_GRID_CHARGE
        and slot.grid_charge_cap_kw is not None
        and power <= max(0.0, -slot.net_kw) + _SAME_KW
    ):
        return ACTION_HOLD
    return action


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
    reserve: float | None = None,
) -> tuple[_Outcome, float]:
    battery = request.battery
    best: _Outcome | None = None
    best_total = 0.0
    for action, power in _candidates(slot, battery, soc_kwh, request, reserve):
        outcome = _apply(slot, battery, soc_kwh, action, power, request.cycle_cost, request.export_hurdle)
        total = outcome.cost + outcome.wear + outcome.hurdle + outcome.peak + grid.value(after, outcome.soc_end_kwh)
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
        demand=outcome.peak,
    )


def _run(decisions: list[SlotDecision], outcomes: list[_Outcome], battery: BatteryParams, soc_kwh: float) -> PlanRun:
    return PlanRun(
        decisions=tuple(decisions),
        cost=sum(o.cost for o in outcomes),
        wear=sum(o.wear for o in outcomes),
        end_soc_pct=soc_kwh / battery.usable_kwh * 100,
        hurdle=sum(o.hurdle for o in outcomes),
        demand=sum(o.peak for o in outcomes),
    )


def self_consumption(request: PlanRequest) -> PlanRun:
    """The baseline: every slot left to self-consumption, with the same caps and prices."""
    request.validate()
    battery = request.battery
    soc = battery.usable_kwh * request.soc_now_pct / 100
    days = _PeakDays(request) if _priced(request) else None
    peak = 0.0
    decisions: list[SlotDecision] = []
    outcomes: list[_Outcome] = []
    for index, slot in enumerate(request.slots):
        action, power = _candidates(slot, battery, soc, request)[0]
        outcome = _apply(slot, battery, soc, action, power, request.cycle_cost)
        if days is not None:
            peak = days.start(index, peak)
            charge, peak = _raise(slot, peak, outcome.grid_kw)
            outcome = outcome._replace(peak=charge)
        decisions.append(_decision(slot, battery, outcome))
        outcomes.append(outcome)
        soc = outcome.soc_end_kwh
    return _run(decisions, outcomes, battery, soc)


def objective(run: PlanRun, battery: BatteryParams, end_value: float) -> float:
    """What the search minimises over a run: its money, wear, gate margin and demand charge above the soft ceilings,
    less the stored energy it leaves at `end_value`."""
    return run.cost + run.wear + run.hurdle + run.demand - run.end_kwh(battery) * end_value


def plan_battery(
    request: PlanRequest,
    *,
    keep: tuple[str, float] | None = None,
    keep_min_gain: float = 0.0,
    baseline: PlanRun | None = None,
) -> BatteryPlan:
    """The cheapest way through the horizon, and the baseline it is measured against.

    `keep` is the action (and, for a grid charge, its kW) a plan made before has for the first slot, the one running
    now. The new plan replaces it only when it is better by more than `keep_min_gain` (minor units, on the
    objective): a running action does not flip for a few öre (Predbat's `metric_min_improvement`). The plan then
    says whether it kept the action (`kept`) and what the new one would have gained (`keep_gain`).
    `baseline` is the self-consumption run to measure against (a mode's plan is measured against the battery's own
    floor, not the mode's); by default the request's own.
    """
    request.validate()
    battery = request.battery
    end_value = request.end_value if request.end_value is not None else default_end_value(request.slots, battery)
    grid = _Grid(battery, request.soc_steps)
    reserves = _reserves(request)
    if _priced(request):
        return _plan_peaks(request, keep, keep_min_gain, baseline, end_value, grid, reserves)

    # Backwards: the value-to-go at each grid point, from the horizon's end value.
    values: list[list[float]] = [[] for _ in range(len(request.slots) + 1)]
    values[-1] = [-end_value * point for point in grid.points]
    for index in range(len(request.slots) - 1, -1, -1):
        slot = request.slots[index]
        after = values[index + 1]
        reserve = None if reserves is None else reserves[index]
        values[index] = [_best(slot, request, point, after, grid, reserve)[1] for point in grid.points]

    best = _forward(request, values, grid, reserves=reserves)
    kept = False
    keep_gain: float | None = None
    if keep is not None:
        held = _forward(request, values, grid, first=keep, reserves=reserves)
        keep_gain = objective(held, battery, end_value) - objective(best, battery, end_value)
        if keep_gain <= keep_min_gain:
            best, kept = held, True
    return BatteryPlan(
        plan=best,
        baseline=self_consumption(request) if baseline is None else baseline,
        end_value=end_value,
        battery=battery,
        kept=kept,
        keep_gain=keep_gain,
    )


def _forward(
    request: PlanRequest,
    values: list[list[float]],
    grid: _Grid,
    first: tuple[str, float] | None = None,
    reserves: Sequence[float] | None = None,
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
            action = _kept_action(slot, action, power)
            outcome = _apply(slot, battery, soc, action, power, request.cycle_cost, request.export_hurdle)
        else:
            reserve = None if reserves is None else reserves[index]
            outcome, _total = _best(slot, request, soc, values[index + 1], grid, reserve)
        decisions.append(_decision(slot, battery, outcome))
        outcomes.append(outcome)
        soc = outcome.soc_end_kwh
    return _run(decisions, outcomes, battery, soc)


def mode_request(
    request: PlanRequest, mode: BatteryMode, wear: float, sell_margin: float = DEFAULT_SELL_MARGIN
) -> PlanRequest:
    """[request] as [mode] plans it with [wear] (minor per kWh each way, the currency's): its multiple of the wear,
    and whether and how the battery may sell (behind the gate: [sell_margin] per kWh, the currency's
    `plan_model.sell_margin_minor_per_kwh`, and the house first)."""
    return replace(
        request,
        cycle_cost=mode.wear(wear),
        allow_export_discharge=mode.export != EXPORT_OFF,
        export_hurdle=mode.hurdle(sell_margin),
        export_reserve=mode.house_first,
    )


def plan_mode(
    request: PlanRequest,
    mode: BatteryMode,
    wear: float,
    *,
    sell_margin: float = DEFAULT_SELL_MARGIN,
    keep: tuple[str, float] | None = None,
    keep_min_gain: float = 0.0,
    baseline: PlanRun | None = None,
) -> BatteryPlan:
    """[mode]'s plan on [request] (the battery at its own floor), measured against self-consumption at that floor.

    A mode that does not plan (Self-consumption) gives the baseline as its plan, so its saving is nil."""
    request.validate()
    base = self_consumption(request) if baseline is None else baseline
    if not mode.plans:
        end_value = (
            request.end_value if request.end_value is not None else default_end_value(request.slots, request.battery)
        )
        return BatteryPlan(plan=base, baseline=base, end_value=end_value, battery=request.battery)
    return plan_battery(mode_request(request, mode, wear, sell_margin), keep=keep, keep_min_gain=keep_min_gain, baseline=base)


# ------------------------------------------------------------------------------------------------ the demand charge


def _priced(request: PlanRequest) -> bool:
    return any(slot.priced_ceiling for slot in request.slots)


def _raise(slot: PlanSlot, peak: float, grid_kw: float) -> tuple[float, float]:
    """What an import of [grid_kw] in [slot] adds to the demand charge when the day's peak so far is [peak], and the
    day's peak after it (see the module). A slot without a priced ceiling adds nothing and leaves the peak."""
    if not slot.priced_ceiling:
        return 0.0, peak
    floor = max(peak, slot.soft_cap_kw)  # type: ignore[type-var]
    charge = slot.peak_price * (grid_kw - floor) if grid_kw > floor else 0.0
    return charge, grid_kw if grid_kw > peak else peak


def _reach(slot: PlanSlot, battery: BatteryParams) -> float:
    """The highest import [slot] can come to: a full grid charge, held to the import cap unless even a full
    discharge breaks it."""
    high = slot.net_kw + battery.max_charge_kw
    cap = slot.import_cap_kw
    if cap is not None:
        high = max(min(high, cap), slot.net_kw - battery.max_discharge_kw)
    return high


class _PeakDays:
    """The days a demand charge counts, as the search tracks their peaks (see the module): per slot, the peaks its
    value-to-go is kept at (`levels`): every step from a day's second priced slot to its last, the day's starting peak
    up to its first, and a single row (the peak changes nothing) after its last."""

    def __init__(self, request: PlanRequest) -> None:
        slots = request.slots
        battery = request.battery
        self.keys: list[object] = [
            slot.peak_day if slot.peak_day is not None else index for index, slot in enumerate(slots)
        ]
        low: dict[object, float] = {}
        high: dict[object, float] = {}
        first: dict[object, int] = {}
        last: dict[object, int] = {}
        for index, slot in enumerate(slots):
            if not slot.priced_ceiling:
                continue
            key = self.keys[index]
            low[key] = min(low.get(key, math.inf), slot.soft_cap_kw)  # type: ignore[type-var]
            high[key] = max(high.get(key, -math.inf), _reach(slot, battery))
            first.setdefault(key, index)
            last[key] = index
        self.init = dict(low)
        head = self.keys[0]
        if request.peak_now_kw is not None and head in low:
            self.init[head] = max(low[head], request.peak_now_kw)
        steps = request.peak_steps
        self.levels: list[tuple[float, ...]] = []
        for index, key in enumerate(self.keys):
            if key in first and first[key] < index <= last[key]:
                bottom, top = low[key], max(high[key], self.init[key])
                if top - bottom > _SAME_KW:
                    self.levels.append(tuple(bottom + (top - bottom) * k / steps for k in range(steps + 1)))
                else:
                    self.levels.append((bottom,))
            elif key in first and index <= first[key]:
                self.levels.append((self.init[key],))
            else:
                self.levels.append((0.0,))

    def start(self, index: int, peak: float) -> float:
        """The day's peak entering slot [index]: its day's starting peak where a new day begins, else [peak]."""
        if index == 0 or self.keys[index] != self.keys[index - 1]:
            return self.init.get(self.keys[index], 0.0)
        return peak


def _soc_at(grid: _Grid, soc_kwh: float) -> tuple[int, float]:
    """Where [soc_kwh] falls on the energy grid: the step below and the share of the way to the next."""
    position = (soc_kwh - grid.low) / grid.step
    if position <= 0:
        return 0, 0.0
    if position >= grid.steps:
        return grid.steps, 0.0
    k = int(position)
    return k, position - k


def _row_value(row: Sequence[float], k: int, frac: float) -> float:
    return row[k] + (row[k + 1] - row[k]) * frac if frac else row[k]


def _value_at(rows: Sequence[Sequence[float]], levels: Sequence[float], peak: float, k: int, frac: float) -> float:
    """The value-to-go at the energy point (k, frac) and the day's [peak], linear between the peak levels."""
    if len(levels) == 1:
        return _row_value(rows[0], k, frac)
    position = (peak - levels[0]) / (levels[-1] - levels[0]) * (len(levels) - 1)
    if position <= 0:
        return _row_value(rows[0], k, frac)
    if position >= len(levels) - 1:
        return _row_value(rows[-1], k, frac)
    j = int(position)
    share = position - j
    below = _row_value(rows[j], k, frac)
    return below + (_row_value(rows[j + 1], k, frac) - below) * share if share else below


def _shave_to(slot: PlanSlot, battery: BatteryParams, soc_kwh: float, peak: float) -> float | None:
    """The discharge that brings [slot]'s import down to the day's [peak] where that is above its soft ceiling;
    `None` where there is nothing of the kind to try."""
    if not slot.priced_ceiling or slot.net_kw <= peak + _SAME_KW:
        return None
    if peak <= slot.soft_cap_kw + _SAME_KW:  # type: ignore[operator]
        return None
    _charge_lim, discharge_lim = _limits(slot, battery, soc_kwh)
    power = -min(slot.net_kw - peak, discharge_lim)
    return power if power < -_SAME_KW else None


def _best_peak(
    slot: PlanSlot,
    request: PlanRequest,
    soc_kwh: float,
    peaks: Sequence[float],
    after: Sequence[Sequence[float]],
    after_levels: Sequence[float],
    grid: _Grid,
    reserve: float | None,
) -> list[tuple[_Outcome, float]]:
    """Per day's peak in [peaks]: the best outcome from [soc_kwh], and its total with the value-to-go."""
    battery = request.battery
    cycle_cost, hurdle = request.cycle_cost, request.export_hurdle
    tried = []
    for action, power in _candidates(slot, battery, soc_kwh, request, reserve):
        outcome = _apply(slot, battery, soc_kwh, action, power, cycle_cost, hurdle)
        tried.append((outcome, outcome.cost + outcome.wear + outcome.hurdle, *_soc_at(grid, outcome.soc_end_kwh)))
    priced = slot.priced_ceiling
    out: list[tuple[_Outcome, float]] = []
    for peak in peaks:
        options = tried
        power = _shave_to(slot, battery, soc_kwh, peak)
        if power is not None:
            outcome = _apply(slot, battery, soc_kwh, ACTION_SHAVE, power, cycle_cost, hurdle)
            extra = (outcome, outcome.cost + outcome.wear + outcome.hurdle, *_soc_at(grid, outcome.soc_end_kwh))
            options = [*tried, extra]
        best: _Outcome | None = None
        best_total = 0.0
        best_charge = 0.0
        for outcome, money, k, frac in options:
            if priced:
                charge, new = _raise(slot, peak, outcome.grid_kw)
            else:
                charge, new = 0.0, peak
            total = money + charge + _value_at(after, after_levels, new, k, frac)
            if best is None or total < best_total - _TIE:
                best, best_total, best_charge = outcome, total, charge
        assert best is not None  # self-consumption is always open
        out.append((best._replace(peak=best_charge), best_total))
    return out


def _plan_peaks(
    request: PlanRequest,
    keep: tuple[str, float] | None,
    keep_min_gain: float,
    baseline: PlanRun | None,
    end_value: float,
    grid: _Grid,
    reserves: Sequence[float] | None,
) -> BatteryPlan:
    """`plan_battery` with the day's peak as a second state (see the module)."""
    battery = request.battery
    days = _PeakDays(request)
    count = len(request.slots)
    values: list[list[list[float]]] = [[] for _ in range(count + 1)]
    values[-1] = [[-end_value * point for point in grid.points]]
    levels: list[Sequence[float]] = [*days.levels, (0.0,)]
    for index in range(count - 1, -1, -1):
        slot = request.slots[index]
        reserve = None if reserves is None else reserves[index]
        peaks = levels[index]
        rows: list[list[float]] = [[] for _ in peaks]
        for point in grid.points:
            found = _best_peak(slot, request, point, peaks, values[index + 1], levels[index + 1], grid, reserve)
            for row, (_outcome, total) in zip(rows, found):
                row.append(total)
        values[index] = rows

    def forward(first: tuple[str, float] | None) -> PlanRun:
        soc = battery.usable_kwh * request.soc_now_pct / 100
        peak = 0.0
        decisions: list[SlotDecision] = []
        outcomes: list[_Outcome] = []
        for index, slot in enumerate(request.slots):
            peak = days.start(index, peak)
            if index == 0 and first is not None:
                action, charge_kw = first
                power = action_power(slot, battery, soc, action, charge_kw)
                action = _kept_action(slot, action, power)
                outcome = _apply(slot, battery, soc, action, power, request.cycle_cost, request.export_hurdle)
                charge, _new = _raise(slot, peak, outcome.grid_kw)
                outcome = outcome._replace(peak=charge)
            else:
                reserve = None if reserves is None else reserves[index]
                found = _best_peak(slot, request, soc, (peak,), values[index + 1], levels[index + 1], grid, reserve)
                outcome = found[0][0]
            _charge, peak = _raise(slot, peak, outcome.grid_kw)
            decisions.append(_decision(slot, battery, outcome))
            outcomes.append(outcome)
            soc = outcome.soc_end_kwh
        return _run(decisions, outcomes, battery, soc)

    best = forward(None)
    kept = False
    keep_gain: float | None = None
    if keep is not None:
        held = forward(keep)
        keep_gain = objective(held, battery, end_value) - objective(best, battery, end_value)
        if keep_gain <= keep_min_gain:
            best, kept = held, True
    return BatteryPlan(
        plan=best,
        baseline=self_consumption(request) if baseline is None else baseline,
        end_value=end_value,
        battery=battery,
        kept=kept,
        keep_gain=keep_gain,
    )
