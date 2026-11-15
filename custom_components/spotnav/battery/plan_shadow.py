"""The house battery plan run in shadow on the site: its inputs, when it plans again, why each slot does what it
does, what the battery really did, and the night's review. Pure: no Home Assistant, the clock is passed in.

The shell (`battery/plan_runtime.py`) reads the sources and calls these; `battery/plan.py` makes the plan. Nothing here
or there writes to a device: the plan is a shadow and a benchmark (wave 2 of 3.0).

* **Inputs** (`build_slots`): the price slots with every fee SpotNav adds (`plan.buy_prices`) and the spot price as
  the sell price (`plan.sell_prices`, plus the price's `sell_adjustment`); the house's expected load per hour; the
  raw solar forecast per hour (Wh, the Hybrid strategy's source); the cars' installed plans as a fixed load; the import cap (the main fuse less
  the safety margin as three-phase power, and the import target's held level where one is set). The first slot is
  cut to start now. The horizon ends where the prices or the house forecast end, at most `plan_model.MAX_SLOTS`.
  A slot whose sell price is below zero exports nothing (`export_curtail.curtail_slots`): the rule beside the plan
  sets the site's export limit to 0 kW there, so the plan stores the sun it cannot sell.
* **House load.** The site's own forecast (`site/house_forecast.py`): on a battery site the house's whole load
  (`grid - car - battery` plus the sun's production), since the import less the chargers counts what the battery
  charged and discharged (Sigenergy's AI mode grid-charging at night would read as a heavy house). Until the site has
  enough hours of it, the plan takes the import less the chargers and says so (`house_counts_battery`). The plan's
  own hour rows of `grid - car - battery` (`PlanActuals.rows`) are kept only to hand them to the site once.
* **Battery** (`battery_params`): usable kWh, SoC and rated powers from `battery/facts.py`; without any of them there
  is no plan, and the reason is logged. The floor (`ASSUMED_MIN_SOC_PCT`) and the round trip are assumed, and said to be.
* **Plan again** (`replan_trigger`): at the start, when the prices change (the next day's at 13:00), when the house
  forecast refreshes (hourly) or the hour turns, when the cars' plans or the battery's facts change, and when the
  SoC is more than `SOC_DEVIATION_PCT` off the plan's (at most every `SOC_REPLAN_MIN_S`).
* **Stability** (`keep_min_gain`): the running slot keeps the action the plan before gave it unless the new plan is
  better by more than `KEEP_MIN_MINOR` or `KEEP_DAY_SHARE` of a day's cost (Predbat item 6).
* **Reasons** (`explain`): each slot's reasons as stable codes with typed params (Predbat item 8, the same pattern
  as `planning/status_compose.py`); a client words them.
* **Actuals** (`PlanActuals`): per quarter hour, what the meters say (import, export, the cars, the battery, its SoC),
  the slot's prices and cap, the first action planned for it and the action the plan had when it began. A battery
  that runs its own optimiser is planned all the same, as a benchmark: its quarter hours show what it really did
  (`actual_action`) beside what the plan would have done.
* **The modes side by side** (`plan_model.MODE_DEFS`): the site's mode (`battery_mode`, Balanced by default) is
  the plan in force; the others are planned on the same inputs as long as the CPU budget allows, so their figures
  are seen before a mode is ever chosen for real. Self-consumption does not plan: it is the baseline.
* **Review** (`review_day`), the night after, side by side over the same measured load and sun from the same
  measured SoC: what really happened, each mode's actions run against it (`plan.run_slot`, with that mode's floor),
  and plain self-consumption; `review_totals` sums the days kept (30). Each run is charged the change of its stored
  energy at the day's end value, so one that empties the battery does not look cheap. `saving` (the site's mode
  against self-consumption) is the one figure to show, `mode_savings` the same per mode (and for the defensive
  variant, `VARIANT_DEFENSIVE`: the site's mode planned on the learnt error band's pessimistic side); `actual_saving` says
  what the battery's own way earned against the same baseline: the figure a battery in its own AI mode is judged
  by before SpotNav is ever given the battery.

What the review cannot tell: the battery's real losses and ramp limits (the sim's battery is ideal and its power is
the battery's own reading, so the plan is flattered by a few percent); a slot where the meters were lost counts in
no run.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

from .. import time_zones
from ..planning.demand_charge import hour_start
from ..planning.planner import FiscalChoice
from .export_curtail import curtail_slots, curtails, would_save
from .facts import BatteryFacts
from .plan import action_power, buy_prices, run_slot, sell_prices
from .plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    ACTIONS,
    DEFAULT_ROUND_TRIP_EFFICIENCY,
    MAX_SLOTS,
    MODES,
    BatteryParams,
    BatteryPlan,
    PlanSlot,
    SlotDecision,
)
from .review_baselines import no_battery_cost, sun_gave
from .shadow import parse_aware_datetime

#: The floor the plan keeps when the battery does not say its own (its reserve or cut-off is not read yet).
ASSUMED_MIN_SOC_PCT: Final = 10.0
#: The SoC off the plan's by more than this (percentage points) plans again.
SOC_DEVIATION_PCT: Final = 10.0
#: ... but at most this often (s): a battery that runs its own way is off the plan all day.
SOC_REPLAN_MIN_S: Final = 900.0
#: A running slot's action changes only for a gain above the larger of these (minor units; share of a day's cost).
KEEP_MIN_MINOR: Final = 5.0
KEEP_DAY_SHARE: Final = 0.02
#: The plan's own house rows (handed to the site's whole load once) are kept this many days.
ROWS_DAYS: Final = 42
#: Actuals are kept per quarter hour for this many days; reviews for this many.
BUCKET: Final = timedelta(minutes=15)
BUCKET_DAYS: Final = 3
REVIEW_DAYS: Final = 30
#: A reading gap longer than this (s) adds nothing to a quarter hour.
MAX_GAP_S: Final = 60.0
#: A quarter hour measured for less than this share counts in no run of the review.
MIN_COVERED_SHARE: Final = 0.5
#: The forecast log's kind for the plan's SoC (`site/forecast_log.py`): the SoC planned at each hour's end.
KIND_BATTERY: Final = "battery"
#: Battery power (kW) below which a quarter hour's battery counts as idle (`actual_action`).
IDLE_KW: Final = 0.2

# Why a plan was made.
TRIGGER_START: Final = "start"
TRIGGER_PRICES: Final = "prices"
TRIGGER_FORECAST: Final = "forecast"
TRIGGER_HOUR: Final = "hour"
TRIGGER_INPUTS: Final = "inputs"
TRIGGER_SOC: Final = "soc_deviation"

# Why there is no plan.
NO_BATTERY: Final = "no_battery"
NO_SOC: Final = "no_soc"
NO_CAPACITY: Final = "no_capacity"
NO_RATED_POWER: Final = "no_rated_power"
NO_PRICES: Final = "no_prices"
NO_FEES: Final = "no_fees"
NO_HOUSE_FORECAST: Final = "no_house_forecast"
FAILED: Final = "failed"

# What the plan assumed (estimate markers, Predbat item 8).
EST_MIN_SOC: Final = "min_soc_assumed"
EST_ROUND_TRIP: Final = "round_trip_assumed"
EST_HOUSE_COUNTS_BATTERY: Final = "house_counts_battery"
EST_NO_SOLAR: Final = "no_solar_forecast"
EST_SOLAR_IN_HOUSE: Final = "solar_in_house_history"
EST_HORIZON_CUT: Final = "horizon_cut_by_forecast"

#: The defensive variant (`battery/defensive.py`): the site's mode planned on the learnt error band's pessimistic side
#: (the house's p90 in the dear hours, the sun's p10). Planned and reviewed beside the modes, never in force.
VARIANT_DEFENSIVE: Final = "defensive"
#: Every plan kept per quarter hour and reviewed beside the plan in force: the modes and the defensive variant.
REVIEWED: Final = (*MODES, VARIANT_DEFENSIVE)

# Each slot's reasons: stable codes, params in fixed units (minor per kWh, kW, kWh, %).
REASON_CHARGE_FOR_LATER: Final = "charge_for_later"
REASON_CHARGE_FOR_END: Final = "charge_for_end"
REASON_HOLD_FOR_LATER: Final = "hold_for_later"
REASON_HOLD_FOR_END: Final = "hold_for_end"
REASON_COVER_HOUSE: Final = "cover_house"
REASON_STORE_SUN: Final = "store_sun"
REASON_IDLE: Final = "idle"
REASON_SELL_SUN: Final = "sell_sun_keep_room"
REASON_EXPORT_DEAR: Final = "export_dear"
REASON_OVER_CAP: Final = "over_cap"
REASON_CURTAILED: Final = "curtailed"
REASON_AT_FLOOR: Final = "at_floor"
REASON_AT_CEILING: Final = "at_ceiling"
REASON_KEPT_RUNNING: Final = "kept_running"
REASON_CODES: Final = (
    REASON_CHARGE_FOR_LATER,
    REASON_CHARGE_FOR_END,
    REASON_HOLD_FOR_LATER,
    REASON_HOLD_FOR_END,
    REASON_COVER_HOUSE,
    REASON_STORE_SUN,
    REASON_IDLE,
    REASON_SELL_SUN,
    REASON_EXPORT_DEAR,
    REASON_OVER_CAP,
    REASON_CURTAILED,
    REASON_AT_FLOOR,
    REASON_AT_CEILING,
    REASON_KEPT_RUNNING,
)

# What a quarter hour's battery really did (`actual_action`).
DID_GRID_CHARGE: Final = "grid_charge"
DID_STORE_SUN: Final = "store_sun"
DID_DISCHARGE: Final = "discharge"
DID_EXPORT_DISCHARGE: Final = "export_discharge"
DID_IDLE: Final = "idle"


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


# ------------------------------------------------------------------------------------------------ inputs


def battery_params(facts: BatteryFacts) -> tuple[BatteryParams | None, str | None, tuple[str, ...]]:
    """The battery as the plan sees it, or why it cannot be planned; with what was assumed."""
    if facts.battery_entity_id is None:
        return None, NO_BATTERY, ()
    if facts.soc_pct is None:
        return None, NO_SOC, ()
    if not _finite(facts.capacity_kwh) or (facts.capacity_kwh or 0) <= 0:
        return None, NO_CAPACITY, ()
    if not _finite(facts.rated_charge_kw) or not _finite(facts.rated_discharge_kw):
        return None, NO_RATED_POWER, ()
    if (facts.rated_charge_kw or 0) <= 0 and (facts.rated_discharge_kw or 0) <= 0:
        return None, NO_RATED_POWER, ()
    params = BatteryParams(
        usable_kwh=float(facts.capacity_kwh),  # type: ignore[arg-type]
        max_charge_kw=max(0.0, float(facts.rated_charge_kw)),  # type: ignore[arg-type]
        max_discharge_kw=max(0.0, float(facts.rated_discharge_kw)),  # type: ignore[arg-type]
        min_soc_pct=ASSUMED_MIN_SOC_PCT,
        round_trip_efficiency=DEFAULT_ROUND_TRIP_EFFICIENCY,
    )
    return params, None, (EST_MIN_SOC, EST_ROUND_TRIP)


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """One price interval as the plan prices it: buy with every fee, sell at spot plus the sold-kWh figure (minor per kWh)."""

    start: datetime
    end: datetime
    buy: float
    sell: float


def price_slots(
    points: Iterable[tuple[datetime, datetime, float]], fiscal: FiscalChoice, sell_credit: float | None = None
) -> list[PriceSlot]:
    """`(start, end, spot in major per kWh)` rows as plan prices (`plan.buy_prices`, `plan.sell_prices`), in order.
    Every exported kWh earns the spot price plus what the electricity price says a sold kWh earns beyond it
    (`FiscalChoice.sell_adjustment`: the grid operator's credit for production, Swedish "nätnytta", plus or minus the
    supplier's terms, as paid out with no VAT); `sell_credit` (minor per kWh) stands in for it where given."""
    rows = sorted(points, key=lambda row: row[0])
    buys = buy_prices([row[2] for row in rows], [row[0] for row in rows], fiscal)
    credit = fiscal.sell_adjustment if sell_credit is None else sell_credit
    sells = sell_prices([row[2] for row in rows], credit)
    return [PriceSlot(row[0], row[1], buy, sell) for row, buy, sell in zip(rows, buys, sells)]


def import_cap_kw(
    main_fuse_a: float | None, margin_a: float | None, volts_between_phases: float, target_kw: float | None
) -> float | None:
    """The most the plan may import (kW): the main fuse less its safety margin as three-phase power, and the import
    target's held level where one is set (the lower of the two); `None` when neither is known."""
    caps: list[float] = []
    if _finite(main_fuse_a) and (main_fuse_a or 0) > 0:
        amps = max(0.0, float(main_fuse_a) - (float(margin_a) if _finite(margin_a) else 0.0))  # type: ignore[arg-type]
        caps.append(amps * math.sqrt(3) * volts_between_phases / 1000.0)
    if _finite(target_kw) and (target_kw or 0) > 0:
        from ..site.import_target import held_level_kw  # noqa: PLC0415 - pure, kept beside its only use

        caps.append(held_level_kw(float(target_kw)))  # type: ignore[arg-type]
    return min(caps) if caps else None


def car_kw_in(start: datetime, end: datetime, windows: Iterable[tuple[datetime, datetime, float]]) -> float:
    """The cars' planned charge over `[start, end)` (kW, the mean): each window's power for its share of the slot."""
    span = (end - start).total_seconds()
    if span <= 0:
        return 0.0
    total = 0.0
    for first, last, kw in windows:
        overlap = (min(end, last) - max(start, first)).total_seconds()
        if overlap > 0 and _finite(kw) and kw > 0:
            total += kw * overlap / span
    return total


def build_slots(
    now: datetime,
    prices: Sequence[PriceSlot],
    house_by_hour: Mapping[datetime, float],
    solar_wh_by_hour: Mapping[datetime, float] | None,
    car_windows: Sequence[tuple[datetime, datetime, float]],
    cap_kw: float | None,
    tz: str,
) -> tuple[tuple[PlanSlot, ...], tuple[str, ...]]:
    """The plan's slots from now (see the module) and what was assumed. `house_by_hour` is keyed by the local hour's
    start (UTC), `solar_wh_by_hour` by the UTC hour's start; an hour the solar forecast does not hold has no sun."""
    estimates: list[str] = []
    if solar_wh_by_hour is None:
        estimates.append(EST_NO_SOLAR)
    slots: list[PlanSlot] = []
    for price in prices:
        if price.end <= now:
            continue
        start = max(price.start, now)
        if (price.end - start).total_seconds() < 60:
            continue
        house = house_by_hour.get(hour_start(start, tz))
        if house is None or not _finite(house):
            if slots:
                estimates.append(EST_HORIZON_CUT)
            break
        if slots and slots[-1].end != start:
            break
        utc_hour = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        wh = None if solar_wh_by_hour is None else solar_wh_by_hour.get(utc_hour)
        slots.append(
            PlanSlot(
                start=start,
                end=price.end,
                buy=price.buy,
                sell=price.sell,
                house_kw=max(0.0, float(house)),
                solar_kw=max(0.0, float(wh) / 1000.0) if _finite(wh) else 0.0,
                car_kw=car_kw_in(start, price.end, car_windows),
                import_cap_kw=cap_kw,
            )
        )
        if len(slots) >= MAX_SLOTS:
            break
    return curtail_slots(slots), tuple(estimates)


def keep_min_gain(kept_cost: float, horizon_h: float) -> float:
    """The gain a new plan needs to change the running slot's action (minor units): `KEEP_MIN_MINOR`, or
    `KEEP_DAY_SHARE` of a day's cost at the plan's pace, whichever is larger."""
    day_cost = abs(kept_cost) * 24.0 / horizon_h if horizon_h > 0 else 0.0
    return max(KEEP_MIN_MINOR, KEEP_DAY_SHARE * day_cost)


# ------------------------------------------------------------------------------------------------ the plan kept


@dataclass(frozen=True, slots=True)
class PlannedSlot:
    """A slot of the plan in force, as the shell keeps it between plans (and across a restart)."""

    start: datetime
    end: datetime
    action: str
    battery_kw: float
    soc_end_pct: float

    def as_row(self) -> list[Any]:
        return [self.start.isoformat(), self.end.isoformat(), self.action, round(self.battery_kw, 3), round(self.soc_end_pct, 2)]

    @classmethod
    def from_row(cls, raw: Any) -> PlannedSlot | None:
        if not isinstance(raw, list) or len(raw) != 5:
            return None
        start, end = parse_aware_datetime(raw[0]), parse_aware_datetime(raw[1])
        if start is None or end is None or end <= start or raw[2] not in ACTIONS:
            return None
        if not (_finite(raw[3]) and _finite(raw[4])):
            return None
        return cls(start, end, raw[2], float(raw[3]), float(raw[4]))


def planned_slots(decisions: Iterable[SlotDecision]) -> tuple[PlannedSlot, ...]:
    return tuple(PlannedSlot(d.start, d.end, d.action, d.battery_kw, d.soc_end_pct) for d in decisions)


def slot_at(slots: Sequence[PlannedSlot], moment: datetime) -> PlannedSlot | None:
    for slot in slots:
        if slot.start <= moment < slot.end:
            return slot
    return None


def expected_soc_at(slots: Sequence[PlannedSlot], soc_at_plan: float, moment: datetime) -> float | None:
    """The SoC the plan expects at `moment` (%), straight between its slots' ends; `None` outside the plan."""
    before = soc_at_plan
    for slot in slots:
        if slot.start <= moment < slot.end:
            share = (moment - slot.start).total_seconds() / (slot.end - slot.start).total_seconds()
            return before + (slot.soc_end_pct - before) * share
        before = slot.soc_end_pct
    return None


def soc_by_hour_end(decisions: Iterable[SlotDecision], tz: str) -> dict[datetime, float]:
    """The planned SoC at each local hour's end, keyed by the hour's start (the forecast log's shape): the SoC of the
    slot that ends the hour."""
    out: dict[datetime, float] = {}
    for decision in decisions:
        if hour_start(decision.end, tz) == decision.end:
            out[hour_start(decision.end - timedelta(seconds=1), tz)] = decision.soc_end_pct
    return out


@dataclass(slots=True)
class TriggerMemory:
    """What the last plan was made from, to tell when to plan again."""

    planned_at: datetime | None = None
    prices_key: Any = None
    forecast_key: Any = None
    hour: datetime | None = None
    inputs_key: Any = None
    soc_replan_at: datetime | None = None


def replan_trigger(
    memory: TriggerMemory,
    now: datetime,
    *,
    prices_key: Any,
    forecast_key: Any,
    hour: datetime,
    inputs_key: Any,
    soc_now: float | None,
    expected_soc: float | None,
) -> str | None:
    """Why to plan again now, or `None` (see the module)."""
    if memory.planned_at is None:
        return TRIGGER_START
    if prices_key != memory.prices_key:
        return TRIGGER_PRICES
    if forecast_key != memory.forecast_key:
        return TRIGGER_FORECAST
    if hour != memory.hour:
        return TRIGGER_HOUR
    if inputs_key != memory.inputs_key:
        return TRIGGER_INPUTS
    if (
        soc_now is not None
        and expected_soc is not None
        and abs(soc_now - expected_soc) > SOC_DEVIATION_PCT
        and (memory.soc_replan_at is None or (now - memory.soc_replan_at).total_seconds() >= SOC_REPLAN_MIN_S)
    ):
        return TRIGGER_SOC
    return None


# ------------------------------------------------------------------------------------------------ reasons


def _reason(code: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "params": {key: (round(value, 2) if isinstance(value, float) else value) for key, value in params.items()}}


def explain(plan: BatteryPlan, slots: Sequence[PlanSlot]) -> list[list[dict[str, Any]]]:
    """Each slot's reasons as `[{code, params}]` (see the module): why the action, then what bounds it."""
    decisions = plan.plan.decisions
    battery = plan.battery
    out: list[list[dict[str, Any]]] = []
    for index, (decision, slot) in enumerate(zip(decisions, slots)):
        reasons: list[dict[str, Any]] = []
        if index == 0 and plan.kept:
            reasons.append(_reason(REASON_KEPT_RUNNING, gain=float(plan.keep_gain or 0.0)))
        if decision.action in (ACTION_GRID_CHARGE, ACTION_HOLD):
            later = next(
                (j for j in range(index + 1, len(decisions)) if decisions[j].battery_kw < -IDLE_KW / 10), None
            )
            charge = decision.action == ACTION_GRID_CHARGE
            if later is None:
                code = REASON_CHARGE_FOR_END if charge else REASON_HOLD_FOR_END
                reasons.append(_reason(code, price=slot.buy, end_value=plan.end_value, kw=decision.battery_kw))
            else:
                code = REASON_CHARGE_FOR_LATER if charge else REASON_HOLD_FOR_LATER
                reasons.append(
                    _reason(
                        code,
                        price=slot.buy,
                        later_price=slots[later].buy,
                        later_start=slots[later].start.isoformat(),
                        kw=decision.battery_kw,
                    )
                )
        elif decision.action == ACTION_DISCHARGE_HOUSE:
            if decision.battery_kw < 0:
                reasons.append(_reason(REASON_COVER_HOUSE, price=slot.buy, kw=-decision.battery_kw))
            reasons.append(_reason(REASON_SELL_SUN, sell=slot.sell))
        elif decision.action == ACTION_EXPORT_DISCHARGE:
            reasons.append(_reason(REASON_EXPORT_DEAR, sell=slot.sell, kw=-decision.battery_kw))
        elif decision.battery_kw < -1e-6:
            reasons.append(_reason(REASON_COVER_HOUSE, price=slot.buy, kw=-decision.battery_kw))
        elif decision.battery_kw > 1e-6:
            reasons.append(_reason(REASON_STORE_SUN, kw=decision.battery_kw))
        else:
            reasons.append(_reason(REASON_IDLE))
        if decision.over_cap:
            reasons.append(_reason(REASON_OVER_CAP, cap_kw=float(slot.import_cap_kw or 0.0)))
        if decision.curtailed_kwh > 0:
            reasons.append(_reason(REASON_CURTAILED, kwh=decision.curtailed_kwh))
        if decision.soc_end_pct <= battery.min_soc_pct + 0.1:
            reasons.append(_reason(REASON_AT_FLOOR, soc_pct=battery.min_soc_pct))
        elif decision.soc_end_pct >= battery.max_soc_pct - 0.1:
            reasons.append(_reason(REASON_AT_CEILING, soc_pct=battery.max_soc_pct))
        out.append(reasons)
    return out


def plan_view(plan: BatteryPlan, slots: Sequence[PlanSlot]) -> dict[str, Any]:
    """The plan for the diagnostics: the totals, and per slot the plan's and the baseline's figures and the reasons."""
    out = plan.as_dict()
    reasons = explain(plan, slots)
    for row, base, slot, why in zip(out["slots"], plan.baseline.decisions, slots, reasons):
        row["buy"] = round(slot.buy, 2)
        row["sell"] = round(slot.sell, 2)
        row["house_kw"] = round(slot.house_kw, 3)
        if slot.solar_kw:
            row["solar_kw"] = round(slot.solar_kw, 3)
        if slot.car_kw:
            row["car_kw"] = round(slot.car_kw, 3)
        row["baseline_cost"] = round(base.cost, 2)
        row["baseline_soc_end_pct"] = round(base.soc_end_pct, 1)
        row["reasons"] = why
    return out


# ------------------------------------------------------------------------------------------------ actuals


def bucket_start(moment: datetime) -> datetime:
    """The UTC quarter hour `moment` falls in (every zone's offset is whole quarter hours)."""
    moment = moment.astimezone(timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 15, second=0, microsecond=0)


@dataclass(slots=True)
class Bucket:
    """One quarter hour: what the meters said, its prices and cap, and what the plan said about it.

    Energies in kWh over the time measured (`covered_s`); the battery's signed `+` charging. `first_*` is the first
    plan that covered it, `ran_*` the plan in force when it began (what would have been written)."""

    start: datetime
    buy: float | None = None
    sell: float | None = None
    cap_kw: float | None = None
    import_kwh: float = 0.0
    export_kwh: float = 0.0
    car_kwh: float = 0.0
    battery_kwh: float = 0.0
    covered_s: float = 0.0
    soc_start: float | None = None
    soc_end: float | None = None
    first_action: str | None = None
    first_soc: float | None = None
    ran_action: str | None = None
    ran_kw: float | None = None
    ran_soc: float | None = None
    #: The same per mode planned beside it: `{mode: (action, kW)}`.
    ran_modes: dict[str, tuple[str, float]] = field(default_factory=dict)

    @property
    def end(self) -> datetime:
        return self.start + BUCKET

    @property
    def covered(self) -> bool:
        return self.covered_s >= MIN_COVERED_SHARE * BUCKET.total_seconds()

    def scaled(self, kwh: float) -> float:
        """A kWh over the time measured, as over the whole quarter hour."""
        return kwh * BUCKET.total_seconds() / self.covered_s if self.covered_s > 0 else 0.0

    @property
    def net_house_kw(self) -> float | None:
        """The house less the sun (kW): the grid less the cars and the battery."""
        if not self.covered:
            return None
        hours = self.covered_s / 3600.0
        return (self.import_kwh - self.export_kwh - self.car_kwh - self.battery_kwh) / hours

    def actual_action(self) -> str | None:
        """What the battery really did, in the plan's words where they fit."""
        if not self.covered:
            return None
        hours = self.covered_s / 3600.0
        battery_kw = self.battery_kwh / hours
        import_kw = self.import_kwh / hours
        export_kw = self.export_kwh / hours
        if battery_kw > IDLE_KW:
            return DID_GRID_CHARGE if import_kw > IDLE_KW and export_kw < IDLE_KW else DID_STORE_SUN
        if battery_kw < -IDLE_KW:
            return DID_EXPORT_DISCHARGE if export_kw > IDLE_KW else DID_DISCHARGE
        return DID_IDLE

    def as_row(self) -> list[Any]:
        def r(value: float | None, digits: int = 4) -> float | None:
            return None if value is None else round(value, digits)

        return [
            self.start.isoformat(),
            r(self.buy, 3),
            r(self.sell, 3),
            r(self.cap_kw, 3),
            r(self.import_kwh),
            r(self.export_kwh),
            r(self.car_kwh),
            r(self.battery_kwh),
            round(self.covered_s, 1),
            r(self.soc_start, 2),
            r(self.soc_end, 2),
            self.first_action,
            r(self.first_soc, 2),
            self.ran_action,
            r(self.ran_kw, 3),
            r(self.ran_soc, 2),
            {mode: [action, round(kw, 3)] for mode, (action, kw) in sorted(self.ran_modes.items())} or None,
        ]

    @classmethod
    def from_row(cls, raw: Any) -> Bucket | None:
        # 18 fields: a row from before the modes, whose export variant is dropped.
        if not isinstance(raw, list) or len(raw) not in (17, 18):
            return None
        start = parse_aware_datetime(raw[0])
        if start is None:
            return None
        numbers = [raw[i] for i in (1, 2, 3, 9, 10, 12, 14, 15)]
        if any(value is not None and not _finite(value) for value in numbers):
            return None
        energies = [raw[i] for i in (4, 5, 6, 7, 8)]
        if not all(_finite(value) for value in energies) or raw[8] < 0:
            return None
        actions = (raw[11], raw[13])
        if any(action is not None and action not in ACTIONS for action in actions):
            return None
        ran_modes: dict[str, tuple[str, float]] = {}
        if len(raw) == 17 and raw[16] is not None:
            if not isinstance(raw[16], Mapping):
                return None
            for mode, pair in raw[16].items():
                if mode not in REVIEWED or not isinstance(pair, list) or len(pair) != 2:
                    return None
                if pair[0] not in ACTIONS or not _finite(pair[1]):
                    return None
                ran_modes[mode] = (pair[0], float(pair[1]))
        return cls(
            start=start.astimezone(timezone.utc),
            buy=raw[1],
            sell=raw[2],
            cap_kw=raw[3],
            import_kwh=float(raw[4]),
            export_kwh=float(raw[5]),
            car_kwh=float(raw[6]),
            battery_kwh=float(raw[7]),
            covered_s=float(raw[8]),
            soc_start=raw[9],
            soc_end=raw[10],
            first_action=raw[11],
            first_soc=raw[12],
            ran_action=raw[13],
            ran_kw=raw[14],
            ran_soc=raw[15],
            ran_modes=ran_modes,
        )

    def view(self) -> dict[str, Any]:
        """The quarter hour for the diagnostics: the plan's action beside what the battery did."""
        hours = self.covered_s / 3600.0
        return {
            "start": self.start.isoformat(),
            "planned": self.ran_action,
            "planned_by_mode": {mode: action for mode, (action, _kw) in sorted(self.ran_modes.items())},
            "planned_soc_pct": None if self.ran_soc is None else round(self.ran_soc, 1),
            "first_planned": self.first_action,
            "actual": self.actual_action(),
            "battery_kw": round(self.battery_kwh / hours, 3) if self.covered else None,
            "grid_kw": round((self.import_kwh - self.export_kwh) / hours, 3) if self.covered else None,
            "soc_pct": None if self.soc_end is None else round(self.soc_end, 1),
        }


@dataclass(slots=True)
class _Sample:
    at: datetime
    grid_kw: float | None
    car_kw: float | None
    battery_kw: float | None


@dataclass(slots=True)
class PlanActuals:
    """The quarter hours measured, the first action planned for those to come, and the plan's own house rows."""

    buckets: dict[datetime, Bucket] = field(default_factory=dict)
    #: The first plan's action and SoC for each quarter hour still to come.
    firsts: dict[datetime, tuple[str, float]] = field(default_factory=dict)
    #: `{local hour start (UTC): the site's mean kW net of the battery and the cars}`.
    rows: dict[datetime, float] = field(default_factory=dict)
    last: _Sample | None = None

    def note_first(self, slots: Sequence[PlannedSlot], soc_at_plan: float) -> None:
        """Keep each future quarter hour's first planned action and the SoC planned at its end (straight between
        the slots' ends)."""
        for slot in slots:
            moment = bucket_start(slot.start)
            if moment < slot.start:
                moment += BUCKET
            while moment < slot.end:
                if moment not in self.firsts and moment not in self.buckets:
                    soc = expected_soc_at(slots, soc_at_plan, moment + BUCKET - timedelta(microseconds=1))
                    self.firsts[moment] = (slot.action, slot.soc_end_pct if soc is None else soc)
                moment += BUCKET

    def open(
        self,
        start: datetime,
        *,
        price: PriceSlot | None,
        cap_kw: float | None,
        ran: PlannedSlot | None,
        ran_modes: Mapping[str, PlannedSlot | None] | None = None,
    ) -> Bucket:
        """The quarter hour starting at `start`, opened with the prices and the plan in force when it began."""
        bucket = self.buckets.get(start)
        if bucket is not None:
            return bucket
        bucket = Bucket(start=start, cap_kw=cap_kw)
        if price is not None:
            bucket.buy, bucket.sell = price.buy, price.sell
        first = self.firsts.pop(start, None)
        if first is not None:
            bucket.first_action, bucket.first_soc = first
        if ran is not None:
            bucket.ran_action = ran.action
            bucket.ran_kw = ran.battery_kw
            bucket.ran_soc = ran.soc_end_pct
        for mode, slot in (ran_modes or {}).items():
            if slot is not None:
                bucket.ran_modes[mode] = (slot.action, slot.battery_kw)
        self.buckets[start] = bucket
        return bucket

    def observe(
        self, now: datetime, grid_kw: float | None, car_kw: float | None, battery_kw: float | None, soc: float | None
    ) -> None:
        """A reading: the last one's power counts until now in its quarter hour (a gap over `MAX_GAP_S` counts
        nothing); the SoC marks the quarter hour's start and end."""
        last = self.last
        if last is not None:
            gap = (now - last.at).total_seconds()
            bucket = self.buckets.get(bucket_start(last.at))
            if (
                bucket is not None
                and 0 < gap <= MAX_GAP_S
                and last.grid_kw is not None
                and last.car_kw is not None
                and last.battery_kw is not None
            ):
                hours = gap / 3600.0
                if last.grid_kw >= 0:
                    bucket.import_kwh += last.grid_kw * hours
                else:
                    bucket.export_kwh -= last.grid_kw * hours
                bucket.car_kwh += max(0.0, last.car_kw) * hours
                bucket.battery_kwh += last.battery_kw * hours
                bucket.covered_s += gap
        bucket = self.buckets.get(bucket_start(now))
        if bucket is not None and soc is not None and _finite(soc):
            if bucket.soc_start is None:
                bucket.soc_start = soc
            bucket.soc_end = soc
        self.last = _Sample(now, grid_kw, car_kw, battery_kw)

    def close_hours(self, now: datetime, tz: str) -> list[datetime]:
        """Turn every local hour that has ended and whose quarter hours are measured into a house row; the hours
        added."""
        current = hour_start(now, tz)
        by_hour: dict[datetime, list[Bucket]] = {}
        for bucket in self.buckets.values():
            start = hour_start(bucket.start, tz)
            if start < current and start not in self.rows:
                by_hour.setdefault(start, []).append(bucket)
        added: list[datetime] = []
        for start, buckets in sorted(by_hour.items()):
            covered = [bucket for bucket in buckets if bucket.covered]
            if len(covered) * 2 < len(buckets) or not covered:
                continue
            kwh = sum(b.import_kwh - b.export_kwh - b.car_kwh - b.battery_kwh for b in covered)
            hours = sum(b.covered_s for b in covered) / 3600.0
            self.rows[start] = kwh / hours
            added.append(start)
        return added

    def prune(self, now: datetime) -> None:
        oldest = now - timedelta(days=BUCKET_DAYS)
        self.buckets = {start: b for start, b in self.buckets.items() if start >= oldest}
        current = bucket_start(now)
        self.firsts = {start: first for start, first in self.firsts.items() if start >= current}
        oldest_row = now - timedelta(days=ROWS_DAYS)
        self.rows = {start: kw for start, kw in self.rows.items() if start >= oldest_row}

    def soc_at_hour_end(self, hour: datetime, tz: str) -> float | None:
        """The SoC read at the end of the local hour starting at `hour` (its last quarter hour's last reading)."""
        end = hour + timedelta(hours=1)
        last = self.buckets.get(end - BUCKET)
        if last is None or hour_start(last.start, tz) != hour:
            return None
        return last.soc_end

    def as_dict(self) -> dict[str, Any]:
        return {
            "buckets": [bucket.as_row() for _start, bucket in sorted(self.buckets.items())],
            "firsts": [[start.isoformat(), action, round(soc, 2)] for start, (action, soc) in sorted(self.firsts.items())],
            "rows": [[start.isoformat(), round(kw, 4)] for start, kw in sorted(self.rows.items())],
        }

    @classmethod
    def from_dict(cls, raw: Any, now: datetime) -> PlanActuals:
        actuals = cls()
        if not isinstance(raw, Mapping):
            return actuals
        for row in raw.get("buckets") or []:
            bucket = Bucket.from_row(row)
            if bucket is not None:
                actuals.buckets[bucket.start] = bucket
        for row in raw.get("firsts") or []:
            if isinstance(row, list) and len(row) == 3 and row[1] in ACTIONS and _finite(row[2]):
                start = parse_aware_datetime(row[0])
                if start is not None:
                    actuals.firsts[start.astimezone(timezone.utc)] = (row[1], float(row[2]))
        for row in raw.get("rows") or []:
            if isinstance(row, list) and len(row) == 2 and _finite(row[1]):
                start = parse_aware_datetime(row[0])
                if start is not None:
                    actuals.rows[start.astimezone(timezone.utc)] = float(row[1])
        actuals.prune(now)
        return actuals


# ------------------------------------------------------------------------------------------------ the review


@dataclass(frozen=True, slots=True)
class Run:
    """One way the day could have gone: what it cost (minor units) and where it left the battery."""

    cost: float
    end_soc_pct: float


@dataclass(frozen=True, slots=True)
class DayReview:
    """One local day side by side (see the module): what really happened, the plan's actions (the site's mode) on the
    same measured load and sun, each mode planned beside it that ran the whole day (`modes`), and plain
    self-consumption. Each run's `net` charges it the change of its stored energy at `end_value`; the savings are
    against self-consumption's net, so a positive figure is money saved."""

    day: date
    reviewed_at: datetime
    quarters: int
    covered: int
    unplanned: int
    start_soc_pct: float
    end_value: float
    usable_kwh: float
    actual: Run
    plan: Run
    self_consumption: Run
    modes: dict[str, Run] = field(default_factory=dict)
    soc_mae: float | None = None
    #: The site's mode the day was planned in (the last one in force).
    mode: str | None = None
    #: What the site exported in quarter hours with a sell price below zero (kWh) and what it cost (minor units):
    #: what the export-limit rule (`export_curtail.py`) would have saved. Apart from the runs above.
    curtail_kwh: float = 0.0
    curtail_saving: float = 0.0
    #: What the covered quarter hours would have cost with no battery (minor units; `review_baselines.py`); `None` in
    #: a review from before it.
    no_battery_cost: float | None = None
    #: What the sun gave over the `sun_quarters` quarter hours that had a production figure (minor units). `None`
    #: quarters: not asked for yet (the production comes from the statistics a little later); 0: no figure.
    sun_gave: float = 0.0
    sun_quarters: int | None = None

    def net(self, run: Run) -> float:
        return run.cost - (run.end_soc_pct - self.start_soc_pct) / 100 * self.usable_kwh * self.end_value

    @property
    def saving(self) -> float:
        """The plan against self-consumption: the one figure to show."""
        return self.net(self.self_consumption) - self.net(self.plan)

    @property
    def mode_savings(self) -> dict[str, float]:
        """Each mode planned beside the plan, against self-consumption."""
        base = self.net(self.self_consumption)
        return {mode: base - self.net(run) for mode, run in self.modes.items()}

    @property
    def actual_saving(self) -> float:
        """What the battery's own way (its optimiser, or plain self-consumption) earned against the same baseline."""
        return self.net(self.self_consumption) - self.net(self.actual)

    @property
    def battery_saving(self) -> float | None:
        """What the day really cost against the same day with no battery (positive: money the battery saved): the
        figure to hold up against the battery's own app. `None` for a review from before it."""
        return None if self.no_battery_cost is None else self.no_battery_cost - self.actual.cost

    @property
    def sun_value(self) -> float | None:
        """What the sun gave, `None` while its production is not known."""
        return self.sun_gave if self.sun_quarters else None

    @property
    def against_nothing(self) -> float | None:
        """The sun and the battery together against neither, over a day whose every covered quarter hour has the sun's
        production; `None` otherwise."""
        battery, sun = self.battery_saving, self.sun_value
        return None if battery is None or sun is None or self.sun_quarters != self.covered else battery + sun

    def with_sun(self, usable: Iterable[Bucket], sun_kwh: Mapping[datetime, float]) -> DayReview:
        """This review with what the sun gave over [usable] (the day's covered quarter hours) from [sun_kwh] (per UTC
        quarter hour)."""
        gave, quarters = sun_gave(usable, sun_kwh)
        return replace(self, sun_gave=gave, sun_quarters=quarters)

    def as_dict(self) -> dict[str, Any]:
        def run(item: Run | None) -> dict[str, Any] | None:
            if item is None:
                return None
            return {"cost": round(item.cost, 3), "end_soc_pct": round(item.end_soc_pct, 4), "net": round(self.net(item), 2)}

        return {
            "date": self.day.isoformat(),
            "reviewed_at": self.reviewed_at.isoformat(),
            "quarters": self.quarters,
            "covered": self.covered,
            "unplanned": self.unplanned,
            "start_soc_pct": round(self.start_soc_pct, 4),
            "end_value": round(self.end_value, 2),
            "usable_kwh": self.usable_kwh,
            "actual": run(self.actual),
            "mode": self.mode,
            "plan": run(self.plan),
            "modes": {mode: run(item) for mode, item in sorted(self.modes.items())},
            "self_consumption": run(self.self_consumption),
            "saving": round(self.saving, 2),
            "mode_savings": {mode: round(value, 2) for mode, value in sorted(self.mode_savings.items())},
            "actual_saving": round(self.actual_saving, 2),
            "soc_mae": None if self.soc_mae is None else round(self.soc_mae, 2),
            "curtail": {"kwh": round(self.curtail_kwh, 3), "saving": round(self.curtail_saving, 2)},
            "baselines": {
                "no_battery_cost": None if self.no_battery_cost is None else round(self.no_battery_cost, 3),
                "sun_gave": round(self.sun_gave, 3),
                "sun_quarters": self.sun_quarters,
            },
        }

    @classmethod
    def from_dict(cls, raw: Any) -> DayReview | None:
        if not isinstance(raw, Mapping):
            return None

        def run(item: Any) -> Run:
            return Run(float(item["cost"]), float(item["end_soc_pct"]))

        try:
            reviewed_at = datetime.fromisoformat(raw["reviewed_at"])
            if reviewed_at.tzinfo is None:
                return None
            return cls(
                day=date.fromisoformat(raw["date"]),
                reviewed_at=reviewed_at,
                quarters=int(raw["quarters"]),
                covered=int(raw["covered"]),
                unplanned=int(raw["unplanned"]),
                start_soc_pct=float(raw["start_soc_pct"]),
                end_value=float(raw["end_value"]),
                usable_kwh=float(raw["usable_kwh"]),
                actual=run(raw["actual"]),
                plan=run(raw["plan"]),
                self_consumption=run(raw["self_consumption"]),
                # A review from before the modes kept an export variant (`plan_export`): not a mode, dropped.
                modes={mode: run(item) for mode, item in (raw.get("modes") or {}).items() if mode in REVIEWED},
                soc_mae=None if raw.get("soc_mae") is None else float(raw["soc_mae"]),
                mode=raw.get("mode") if raw.get("mode") in MODES else None,
                **_curtail_from(raw.get("curtail")),
                **_baselines_from(raw.get("baselines")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _curtail_from(raw: Any) -> dict[str, float]:
    """A stored review's curtail figures; a review from before the rule has none."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, name in (("kwh", "curtail_kwh"), ("saving", "curtail_saving")):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            out[name] = float(value)
    return out


def _baselines_from(raw: Any) -> dict[str, Any]:
    """A stored review's baseline figures; a review from before them has none."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    cost, gave, quarters = raw.get("no_battery_cost"), raw.get("sun_gave"), raw.get("sun_quarters")
    if _finite(cost):
        out["no_battery_cost"] = float(cost)
    if _finite(gave) and isinstance(quarters, int) and not isinstance(quarters, bool) and quarters >= 0:
        out["sun_gave"], out["sun_quarters"] = float(gave), quarters
    return out


def review_totals(reviews: Sequence[DayReview]) -> dict[str, Any]:
    """The reviewed days summed (the running total over what is kept, `REVIEW_DAYS`): the nets of each run and the
    savings against self-consumption. Each mode's saving sums only the days it ran (`days`), beside the plan's on
    those same days (`saving_same_days_plan`)."""
    modes: dict[str, dict[str, Any]] = {}
    for mode in REVIEWED:
        ran = [review for review in reviews if mode in review.modes]
        if ran:
            modes[mode] = {
                "days": len(ran),
                "saving": round(sum(review.mode_savings[mode] for review in ran), 2),
                "saving_same_days_plan": round(sum(review.saving for review in ran), 2),
            }
    return {
        "days": len(reviews),
        "first": None if not reviews else reviews[0].day.isoformat(),
        "last": None if not reviews else reviews[-1].day.isoformat(),
        "actual_net": round(sum(review.net(review.actual) for review in reviews), 2),
        "plan_net": round(sum(review.net(review.plan) for review in reviews), 2),
        "self_consumption_net": round(sum(review.net(review.self_consumption) for review in reviews), 2),
        "saving": round(sum(review.saving for review in reviews), 2),
        "actual_saving": round(sum(review.actual_saving for review in reviews), 2),
        "battery_saving": round(sum(review.battery_saving or 0.0 for review in reviews), 2),
        "sun_gave": round(sum(review.sun_value or 0.0 for review in reviews), 2),
        "modes": modes,
        "curtail": {
            "kwh": round(sum(review.curtail_kwh for review in reviews), 3),
            "saving": round(sum(review.curtail_saving for review in reviews), 2),
        },
    }


def _actual_slot(bucket: Bucket) -> PlanSlot:
    net = bucket.net_house_kw or 0.0
    return PlanSlot(
        start=bucket.start,
        end=bucket.end,
        buy=float(bucket.buy or 0.0),
        sell=float(bucket.sell or 0.0),
        house_kw=max(0.0, net),
        solar_kw=max(0.0, -net),
        car_kw=bucket.scaled(bucket.car_kwh) / (BUCKET.total_seconds() / 3600.0),
        import_cap_kw=bucket.cap_kw,
    )


class _Sim:
    """One simulated battery run over the measured quarter hours."""

    def __init__(self, battery: BatteryParams, start_kwh: float) -> None:
        self.battery = battery
        self.kwh = start_kwh
        self.cost = 0.0

    def step(self, slot: PlanSlot, action: str, charge_kw: float) -> None:
        power = action_power(slot, self.battery, self.kwh, action, charge_kw)
        ran = run_slot(slot, self.battery, self.kwh, action, power)
        self.cost += ran.cost
        self.kwh = ran.soc_end_pct / 100 * self.battery.usable_kwh

    def run(self) -> Run:
        return Run(self.cost, self.kwh / self.battery.usable_kwh * 100)


def review_day(
    day: date,
    buckets: Iterable[Bucket],
    battery: BatteryParams,
    tz: str,
    now: datetime,
    modes: Iterable[str] = (),
    mode: str | None = None,
) -> DayReview | None:
    """The day's runs over its measured quarter hours (see the module); `None` when no quarter hour was measured
    with its prices or no SoC was read. [battery] is the battery at its own floor, which every run keeps; [modes] the
    planning modes to review beside the plan, and [mode] the site's. A mode is reviewed when it ran in every planned
    quarter hour."""
    zone = time_zones.require_zone(tz)
    rows = sorted(
        (bucket for bucket in buckets if bucket.start.astimezone(zone).date() == day), key=lambda bucket: bucket.start
    )
    usable = [bucket for bucket in rows if bucket.covered and bucket.buy is not None and bucket.sell is not None]
    socs = [bucket for bucket in rows if bucket.soc_start is not None]
    if not usable or not socs:
        return None
    start_soc = float(socs[0].soc_start)  # type: ignore[arg-type]
    end_soc = float(next(b.soc_end for b in reversed(rows) if b.soc_end is not None))  # type: ignore[arg-type]
    end_value = statistics.median(float(b.buy) for b in usable) * battery.round_trip_efficiency  # type: ignore[arg-type]
    start_kwh = min(battery.max_kwh, max(battery.min_kwh, battery.usable_kwh * start_soc / 100))
    planned = [b for b in usable if b.ran_action is not None]
    reviewed = [name for name in modes if planned and all(name in b.ran_modes for b in planned)]

    actual_cost = 0.0
    plan = _Sim(battery, start_kwh)
    base = _Sim(battery, start_kwh)
    sims = {name: _Sim(battery, start_kwh) for name in reviewed}
    unplanned = 0
    errors: list[float] = []
    curtail_kwh = curtail_saving = 0.0
    for bucket in usable:  # no run counts a quarter hour the meters lost; the sims' battery idles through it
        if curtails(bucket.sell):
            exported = bucket.scaled(bucket.export_kwh)
            curtail_kwh += exported
            curtail_saving += would_save(exported, float(bucket.sell))  # type: ignore[arg-type]
        actual_cost += bucket.scaled(bucket.import_kwh) * bucket.buy - bucket.scaled(bucket.export_kwh) * bucket.sell  # type: ignore[operator]
        slot = _actual_slot(bucket)
        if bucket.ran_action is None:
            unplanned += 1
        plan.step(slot, bucket.ran_action or ACTION_SELF_CONSUME, bucket.ran_kw or 0.0)
        for name, sim in sims.items():
            action, kw = bucket.ran_modes.get(name, (ACTION_SELF_CONSUME, 0.0))
            sim.step(slot, action, kw)
        base.step(slot, ACTION_SELF_CONSUME, 0.0)
        if bucket.ran_soc is not None and bucket.soc_end is not None:
            errors.append(abs(bucket.soc_end - bucket.ran_soc))
    return DayReview(
        day=day,
        reviewed_at=now,
        quarters=len(rows),
        covered=len(usable),
        unplanned=unplanned,
        start_soc_pct=start_soc,
        end_value=end_value,
        usable_kwh=battery.usable_kwh,
        actual=Run(actual_cost, end_soc),
        plan=plan.run(),
        self_consumption=base.run(),
        modes={name: sim.run() for name, sim in sims.items()},
        soc_mae=sum(errors) / len(errors) if errors else None,
        mode=mode,
        curtail_kwh=curtail_kwh,
        curtail_saving=curtail_saving,
        no_battery_cost=no_battery_cost(usable),
    )


def usable_buckets(buckets: Iterable[Bucket], day: date, tz: str) -> list[Bucket]:
    """The day's quarter hours a review counts (measured, with their prices), oldest first."""
    zone = time_zones.require_zone(tz)
    rows = (bucket for bucket in buckets if bucket.start.astimezone(zone).date() == day)
    return sorted(
        (b for b in rows if b.covered and b.buy is not None and b.sell is not None), key=lambda bucket: bucket.start
    )


def day_quarters(day: date, tz: str) -> int:
    """How many quarter hours the local [day] has (96; 92 and 100 on the days the clock changes)."""
    zone = time_zones.require_zone(tz)
    start = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
    end_day = day + timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=zone).astimezone(timezone.utc)
    return int((end - start) / BUCKET)


def days_to_review(buckets: Iterable[Bucket], reviewed: Iterable[date], now: datetime, tz: str) -> list[date]:
    """The local days before today that have quarter hours and no review yet, oldest first."""
    zone = time_zones.require_zone(tz)
    today = now.astimezone(zone).date()
    days = {bucket.start.astimezone(zone).date() for bucket in buckets}
    return sorted(day for day in days - set(reviewed) if day < today)
