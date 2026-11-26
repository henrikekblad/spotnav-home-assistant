"""The day against a perfect plan in hindsight, the battery's work and its cells' temperature. Pure: no Home Assistant.

The night's review (`plan_shadow.review_day`) runs each way of driving the battery over the measured day against plain
self-consumption. This module adds the yardstick a person can read at a glance: the best the battery could have done
that day had everything been known, and each way's share of it.

* **The perfect plan** (`perfect_plans`): the existing planner (`plan.plan_mode`), planned once over the day's
  measured quarter hours with the measured house load, sun and cars (`measured_slot`), the day's real prices with
  their fees, the same battery (its floor, ceiling, rated power and round trip), the mode's wear and the mode's
  selling rule (no selling unless the mode allows it; a quarter hour with a sell price below zero exports nothing, as
  the plan in force has it, `export_curtail.curtail_slots`). One per planning mode (`Facit.perfect`, the ceiling of
  each mode's own rules). A quarter hour the meters lost counts in no run, as in the review: the plan steps over it.
* **The yardstick** (`Facit.best`, `Facit.rules`; owner 2026-11-17): every share is measured against one perfect plan,
  under the most permissive rules any way shown used, so no way can beat it but by the model's ideal battery. Where
  any way sold from the battery that day (`Work.to_grid_kwh` of `SELL_KWH` or more: the battery's own optimiser, or
  a SpotNav mode run beside it) that is Max profit's (`RULES_SELL`: selling allowed, the true wear). Otherwise it is
  a perfect plan that does not sell, with the true wear (`RULES_NO_SELL`). What the battery sold is each quarter
  hour's discharge that went to the grid (the lesser of the two), not the site's export: the sun's surplus and the
  grid meter's few watt-hours while a battery follows the house (0.01-0.03 kWh a day on the owner's plant) are not
  the battery selling. A day stored before this (`RULES_MODE`) measured each SpotNav mode against its own mode's plan
  and the rest against the site's (Balanced's: twice the wear); read back, it is measured against Max profit's
  perfect plan where a way sold, and where none did and Max profit's own plan sold nothing that plan is the no-sell
  one with the true wear (`RULES_NO_SELL`); otherwise it is kept as it was.
* **The day boundary** (fairness): every way (what really happened, which is the battery's own optimiser on a battery
  that runs its own way; each SpotNav mode; the perfect plan) starts the day at the same measured SoC, and the energy
  each leaves at midnight is valued the same way (`end_value_next_day`): at the next day's mean buy price with its
  fees, less the round trip's loss, and at most what putting it back costs (`plan.end_worth`, the plan's own rule a
  day on, at the true wear): the cheapest hours that fill the battery on the next day, on the reviewed day and on
  the day before, the dearest of the three (of two at least: the next day alone gives no refill to count on), with
  the wear of the charge, over the charge's efficiency. That is a price a real controller could know at the time
  (the next day's prices are published at about 13:00), not hindsight about the dear hours ahead. Until the next
  day's prices are held the review's own end value (the same on the day's own prices and the days before it) stands
  in, and the figure says so (`basis`); it is worked
  out again once they are. The perfect plan values its own end the same way, so it plans for
  the night after.
* **Whole days only** (`Facit.whole`): a day counts when at least `WHOLE_DAY_SHARE` of its quarter hours were
  measured. A partial day has no share (the card says "incomplete day") and no sum counts it.
* **Shares** (`Facit.candidates`): each way's saving against the day with no battery at all (`review_baselines`),
  with the energy left at midnight counted at that value, as a share of the yardstick's. A share is given only where
  the perfect plan saves at least `MIN_PERFECT_SAVING` a day (5 kr): below it the difference is too small to compare
  and the share is `None`. Shares are clamped to 0-100 % (`clamped` says when one was): above 100 only by the model's
  ideal battery or the wear the perfect plan weighs but money does not count; below 0 a way that cost more than no
  battery.
* **Work** (`Work`): per way, the kWh discharged and the cycles (that over the usable capacity), how often it switched
  between charging and discharging (idle quarter hours not counted), the kWh charged from the grid and the kWh the
  battery sold to the grid.
* **Cell temperature** (`cell_temperature`): the day's mean, maximum and minutes above `HOT_C`, from the recorder's
  five-minute (else hourly) statistics of the battery's own cell temperature, where the battery reports it.

The battery is counted on the AC side throughout (`Bucket.battery_ac`, `conversion_loss.py`): the house each way
plans around is the same, the real battery's inverter loss is its own cost, and the model's battery has the AC round
trip. Counted as read on a DC-side battery, that loss sat in the house, every other way paid for it and the battery's
own way could come out above its perfect plan (`plans/research_integrations/sigen_ai_above_perfect_2026-11-24.md`).

What it cannot tell: the model's battery has no ramps and an even loss, so what really happened is held to a slightly
kinder standard than it met; an assumed conversion loss (where it is not learnt) is only as good as the assumption;
the perfect plan sees only the measured quarter hours; a single
day's share still hangs on the energy left at midnight, which a week or a month washes out (`site/perfect_view.py`
sums the days' savings before it divides).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from .export_curtail import curtail_slots
from .plan import REFILL_DAYS, action_power, end_worth, plan_mode, run_slot
from .plan_model import (
    ACTION_SELF_CONSUME,
    DEFAULT_SELL_MARGIN,
    EXPORT_OFF,
    MODE_DEFS,
    MODE_MAX_PROFIT,
    BatteryMode,
    BatteryParams,
    PlanRequest,
    PlanSlot,
)

#: The quarter hour.
QUARTER: Final = timedelta(minutes=15)
#: A day counts when at least this share of its quarter hours was measured.
WHOLE_DAY_SHARE: Final = 0.9
#: A share is given only where the perfect plan saves at least this much a day (minor units: 5 kr).
MIN_PERFECT_SAVING: Final = 500.0
#: A cell temperature above this (°C) counts as warm.
HOT_C: Final = 30.0
#: A battery power below this (kW) is idle: it neither charges nor discharges (`plan_shadow` imports it for its `actual_action`).
IDLE_KW: Final = 0.2

# Where the end value came from.
BASIS_NEXT_DAY: Final = "next_day"
BASIS_DAY: Final = "day_median"
BASES: Final = (BASIS_NEXT_DAY, BASIS_DAY)

# The rules of the yardstick every share is measured against (see the module).
RULES_SELL: Final = "max_profit"
RULES_NO_SELL: Final = "no_sell"
RULES_MODE: Final = "mode"
RULES: Final = (RULES_SELL, RULES_NO_SELL, RULES_MODE)
#: Selling counts as possible on a day where a way's battery sent at least this much (kWh) to the grid: a few times the
#: watt-hours a battery that follows the house leaks to the grid on its own (see the module).
SELL_KWH: Final = 0.1
#: The yardstick where nothing could be sold: Max profit's wear, no selling.
NO_SELL_MODE: Final = BatteryMode(RULES_NO_SELL, plans=True, export=EXPORT_OFF, wear_factor=MODE_DEFS[MODE_MAX_PROFIT].wear_factor)

# The ways compared, by the answer's name: what really happened, self-consumption, each planning mode, the perfect plan.
OWN: Final = "own"
SELF_CONSUMPTION: Final = "self_consumption"
PERFECT: Final = "perfect"
PLANNING_MODES: Final = tuple(name for name, mode in MODE_DEFS.items() if mode.plans)
CANDIDATES: Final = (OWN, SELF_CONSUMPTION, *PLANNING_MODES)


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


class MeasuredQuarter(Protocol):
    """What this module reads of a review's quarter hour (`plan_shadow.Bucket`)."""

    start: datetime
    buy: float | None
    sell: float | None
    cap_kw: float | None
    import_kwh: float
    export_kwh: float
    car_kwh: float
    battery_kwh: float
    covered_s: float
    ran_modes: dict[str, tuple[str, float]]

    @property
    def end(self) -> datetime: ...

    @property
    def net_house_kw(self) -> float | None: ...

    @property
    def battery_ac(self) -> float: ...

    def scaled(self, kwh: float) -> float: ...


class PricedSlot(Protocol):
    start: datetime
    end: datetime
    buy: float


def measured_slot(bucket: MeasuredQuarter) -> PlanSlot:
    """A measured quarter hour as a plan slot: the house less the sun as measured, the cars as measured."""
    net = bucket.net_house_kw or 0.0
    return PlanSlot(
        start=bucket.start,
        end=bucket.end,
        buy=float(bucket.buy or 0.0),
        sell=float(bucket.sell or 0.0),
        house_kw=max(0.0, net),
        solar_kw=max(0.0, -net),
        car_kw=bucket.scaled(bucket.car_kwh) / (QUARTER.total_seconds() / 3600.0),
        import_cap_kw=bucket.cap_kw,
    )


# ------------------------------------------------------------------------------------------------ the end value


def end_value_next_day(
    prices: Sequence[PricedSlot], day_end: datetime, next_end: datetime, battery: BatteryParams, wear: float = 0.0
) -> float | None:
    """What a kWh left in the battery at [day_end] is worth (minor units, see the module): the mean buy price of the
    next day ([day_end] to [next_end]) over its time, times the round trip, at most what a refill costs
    (`plan.end_worth`, with [wear] for the charge) on that day and on the days before it that [prices] hold (the
    reviewed day and the one before: `plan.REFILL_DAYS` in all, and no refill from the next day alone); `None`
    while the prices do not cover the next day."""
    worth = seen = 0.0
    days: list[list[tuple[float, float]]] = [[] for _ in range(REFILL_DAYS)]
    for slot in prices:
        if not _finite(slot.buy):
            continue
        start, end = max(slot.start, day_end), min(slot.end, next_end)
        if end > start:
            seconds = (end - start).total_seconds()
            worth += float(slot.buy) * seconds
            seen += seconds
            days[0].append((float(slot.buy), seconds / 3600.0))
        elif slot.end <= day_end and slot.end > slot.start:
            back = 1 + int((day_end - slot.end).total_seconds() // 86400)
            if back < REFILL_DAYS:
                days[back].append((float(slot.buy), (slot.end - slot.start).total_seconds() / 3600.0))
    if seen <= 0 or seen < (next_end - day_end).total_seconds() - 60:
        return None
    return end_worth(worth / seen, days, battery, wear)


# ------------------------------------------------------------------------------------------------ the runs


@dataclass(frozen=True, slots=True)
class Work:
    """What a way asked of the battery over the day (see the module)."""

    discharged_kwh: float = 0.0
    cycles: float = 0.0
    switches: int = 0
    grid_charge_kwh: float = 0.0
    to_grid_kwh: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "discharged_kwh": round(self.discharged_kwh, 2),
            "cycles": round(self.cycles, 3),
            "switches": self.switches,
            "grid_charge_kwh": round(self.grid_charge_kwh, 2),
            "to_grid_kwh": round(self.to_grid_kwh, 2),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Work:
        return cls(
            discharged_kwh=float(raw["discharged_kwh"]),
            cycles=float(raw["cycles"]),
            switches=int(raw["switches"]),
            grid_charge_kwh=float(raw["grid_charge_kwh"]),
            to_grid_kwh=float(raw["to_grid_kwh"]),
        )


def work_of(steps: Iterable[tuple[float, float, float]], usable_kwh: float) -> Work:
    """The work over [steps]: each `(battery kW + charging, grid kW + import, hours)`."""
    discharged = grid_charge = to_grid = 0.0
    switches = 0
    last: int | None = None
    for battery_kw, grid_kw, hours in steps:
        if battery_kw < 0:
            discharged += -battery_kw * hours
            to_grid += max(0.0, min(-battery_kw, -grid_kw)) * hours
        else:
            grid_charge += max(0.0, min(battery_kw, grid_kw)) * hours
        state = 1 if battery_kw > IDLE_KW else -1 if battery_kw < -IDLE_KW else 0
        if state:
            if last is not None and state != last:
                switches += 1
            last = state
    return Work(
        discharged_kwh=discharged,
        cycles=discharged / usable_kwh if usable_kwh > 0 else 0.0,
        switches=switches,
        grid_charge_kwh=grid_charge,
        to_grid_kwh=to_grid,
    )


def measured_work(buckets: Iterable[MeasuredQuarter], usable_kwh: float) -> Work:
    """What the battery really did, from the meters, on the AC side (`Bucket.battery_ac`) like the simulated ways."""
    steps = []
    for bucket in buckets:
        hours = bucket.covered_s / 3600.0
        if hours <= 0:
            continue
        steps.append((bucket.battery_ac / hours, (bucket.import_kwh - bucket.export_kwh) / hours, QUARTER.total_seconds() / 3600.0))
    return work_of(steps, usable_kwh)


@dataclass(frozen=True, slots=True)
class SimRun:
    """One simulated way over the measured quarter hours: its money, where it left the battery and its work."""

    cost: float
    end_soc_pct: float
    work: Work


def simulate(
    slots: Sequence[PlanSlot], battery: BatteryParams, start_soc_pct: float, actions: Sequence[tuple[str, float]]
) -> SimRun:
    """Run each slot's `(action, kW)` from [start_soc_pct] as the review does (`plan.run_slot`)."""
    kwh = min(battery.max_kwh, max(battery.min_kwh, battery.usable_kwh * start_soc_pct / 100))
    cost = 0.0
    steps = []
    for slot, (action, kw) in zip(slots, actions):
        power = action_power(slot, battery, kwh, action, kw)
        ran = run_slot(slot, battery, kwh, action, power)
        cost += ran.cost
        steps.append((ran.battery_kw, ran.grid_kw, slot.hours))
        kwh = ran.soc_end_pct / 100 * battery.usable_kwh
    return SimRun(cost, kwh / battery.usable_kwh * 100, work_of(steps, battery.usable_kwh))


def _chain(slots: Sequence[PlanSlot]) -> list[PlanSlot]:
    """[slots] laid end to end (a quarter hour the meters lost dropped), as the planner wants them without a gap."""
    out: list[PlanSlot] = []
    for slot in slots:
        start = out[-1].end if out else slot.start
        out.append(replace(slot, start=start, end=start + (slot.end - slot.start)))
    return out


def perfect_plans(
    slots: Sequence[PlanSlot],
    battery: BatteryParams,
    start_soc_pct: float,
    wear: float,
    end_value: float,
    modes: Iterable[str | BatteryMode] = PLANNING_MODES,
    sell_margin: float = DEFAULT_SELL_MARGIN,
) -> dict[str, SimRun]:
    """Each of [modes]' perfect plan in hindsight over the measured [slots] (see the module), run as the review runs
    a way; a mode is named (`MODE_DEFS`) or given."""
    chained = list(curtail_slots(_chain(slots)))
    out: dict[str, SimRun] = {}
    if not chained:
        return out
    soc = min(100.0, max(0.0, start_soc_pct))
    for item in modes:
        mode = MODE_DEFS[item] if isinstance(item, str) else item
        name = mode.name
        request = PlanRequest(slots=tuple(chained), battery=battery, soc_now_pct=soc, end_value=end_value)
        made = plan_mode(request, mode, wear, sell_margin=sell_margin)
        actions = [(decision.action, decision.battery_kw) for decision in made.plan.decisions]
        out[name] = simulate(chained, battery, soc, actions)
    return out


# ------------------------------------------------------------------------------------------------ the facit


@dataclass(frozen=True, slots=True)
class Candidate:
    """One way against no battery: its saving with the energy left at midnight counted, the perfect plan's it is
    measured against, its share (0-100, `None`: a partial day or too small a difference to compare), whether the share
    was clamped, and its work."""

    saving: float
    perfect: float
    share: float | None
    clamped: bool
    work: Work

    def as_dict(self) -> dict[str, Any]:
        return {
            "saving": round(self.saving, 2),
            "perfect": round(self.perfect, 2),
            "share": None if self.share is None else round(self.share, 1),
            "clamped": self.clamped,
            "work": self.work.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Candidate:
        share = raw["share"]
        return cls(
            saving=float(raw["saving"]),
            perfect=float(raw["perfect"]),
            share=None if share is None else float(share),
            clamped=bool(raw["clamped"]),
            work=Work.from_dict(raw["work"]),
        )


def share_of(saving: float, perfect: float, minimum: float = MIN_PERFECT_SAVING) -> tuple[float | None, bool]:
    """[saving] as a percentage of [perfect], clamped to 0-100, and whether it was clamped; `None` when the perfect
    plan saves less than [minimum] (a day's `MIN_PERFECT_SAVING`; a period's, that times its days): too small a
    difference to compare."""
    if perfect < minimum or perfect <= 0:
        return None, False
    share = saving / perfect * 100
    clamped = min(100.0, max(0.0, share))
    return clamped, abs(clamped - share) > 0.05


@dataclass(frozen=True, slots=True)
class Facit:
    """One reviewed day against its perfect plan (see the module)."""

    computed_at: datetime
    #: What a kWh left at midnight was worth (minor units) and where that came from (`BASES`).
    end_value: float
    basis: str
    #: The site's mode that day.
    mode: str
    no_battery_cost: float
    #: The local day's quarter hours and how many of them were measured (`whole`).
    quarters: int
    measured: int
    #: The perfect plan's saving against no battery, per planning mode, and its work.
    perfect: dict[str, Candidate] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    #: The rules of the yardstick every share is measured against (`RULES`) and its run (see the module); without a
    #: run (`RULES_MODE`, a day stored before it) the site's mode's perfect plan stands in.
    rules: str = RULES_MODE
    best: Candidate | None = None

    @property
    def yardstick(self) -> Candidate | None:
        """The perfect plan every share is measured against."""
        return self.best if self.best is not None else self.perfect.get(self.mode)

    @property
    def whole(self) -> bool:
        """Whether the day counts: at least `WHOLE_DAY_SHARE` of its quarter hours measured."""
        return self.quarters > 0 and self.measured >= WHOLE_DAY_SHARE * self.quarters

    @property
    def saving(self) -> float | None:
        """The headline: the yardstick's saving."""
        item = self.yardstick
        return None if item is None else item.saving

    def as_dict(self) -> dict[str, Any]:
        return {
            "computed_at": self.computed_at.isoformat(),
            "end_value": round(self.end_value, 2),
            "basis": self.basis,
            "mode": self.mode,
            "no_battery_cost": round(self.no_battery_cost, 2),
            "quarters": self.quarters,
            "measured": self.measured,
            "perfect": {name: item.as_dict() for name, item in sorted(self.perfect.items())},
            "candidates": {name: item.as_dict() for name, item in sorted(self.candidates.items())},
            "rules": self.rules,
            "best": None if self.best is None else self.best.as_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Facit | None:
        if not isinstance(raw, Mapping):
            return None
        try:
            computed_at = datetime.fromisoformat(raw["computed_at"])
            if computed_at.tzinfo is None or raw["basis"] not in BASES or raw["mode"] not in PLANNING_MODES:
                return None
            made = cls(
                computed_at=computed_at,
                end_value=float(raw["end_value"]),
                basis=raw["basis"],
                mode=raw["mode"],
                no_battery_cost=float(raw["no_battery_cost"]),
                quarters=int(raw["quarters"]),
                measured=int(raw["measured"]),
                perfect={k: Candidate.from_dict(v) for k, v in raw["perfect"].items() if k in PLANNING_MODES},
                candidates={k: Candidate.from_dict(v) for k, v in raw["candidates"].items() if k in CANDIDATES},
            )
            if "rules" not in raw or raw["rules"] == RULES_MODE:
                return made.measured_again()  # stored before the yardstick
            rules, best = raw["rules"], raw["best"]
            if rules not in RULES:
                return None
            return replace(made, rules=rules, best=None if best is None else Candidate.from_dict(best))
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def measured_again(self) -> Facit:
        """A day stored before the yardstick (see the module): against Max profit's perfect plan where a way sold, or
        where none did and that plan sold nothing either (it is then the no-sell plan with the true wear); else as it
        was."""
        best = self.perfect.get(MODE_MAX_PROFIT)
        if best is None:
            return self
        if any(item.work.to_grid_kwh >= SELL_KWH for item in self.candidates.values()):
            rules = RULES_SELL
        elif best.work.to_grid_kwh < SELL_KWH:
            rules = RULES_NO_SELL
        else:
            return self
        return replace(
            self,
            rules=rules,
            best=best,
            candidates={name: against(item, best.saving, self.whole) for name, item in self.candidates.items()},
        )


def against(item: Candidate, perfect: float, whole: bool) -> Candidate:
    """[item] measured against a perfect plan's saving [perfect] (no share on a partial day)."""
    share, clamped = share_of(item.saving, perfect) if whole else (None, False)
    return Candidate(item.saving, perfect, share, clamped, item.work)


def facit_day(
    buckets: Sequence[MeasuredQuarter],
    battery: BatteryParams,
    *,
    start_soc_pct: float,
    actual_cost: float,
    actual_end_soc_pct: float,
    mode: str | None,
    wear: float,
    end_value: float,
    basis: str,
    no_battery_cost: float,
    day_quarters: int,
    now: datetime,
    sell_margin: float = DEFAULT_SELL_MARGIN,
    own_work: Work | None = None,
) -> Facit | None:
    """The day against its perfect plan (see the module). [buckets] are the review's usable quarter hours, oldest
    first, of a local day of [day_quarters]; [actual_cost] and [actual_end_soc_pct] what really happened (the
    review's), [own_work] its work where the quarter hours do not hold the battery (a rebuilt day,
    `review_rebuild.py`); `None` without a quarter hour. A partial day gets its figures and no share."""
    if not buckets:
        return None
    site_mode = mode if mode in PLANNING_MODES else PLANNING_MODES[0]
    slots = [measured_slot(bucket) for bucket in buckets]
    usable = battery.usable_kwh

    def net(cost: float, end_soc: float) -> float:
        return cost - (end_soc - start_soc_pct) / 100 * usable * end_value

    def saving(cost: float, end_soc: float) -> float:
        return no_battery_cost - net(cost, end_soc)

    whole = len(buckets) >= WHOLE_DAY_SHARE * day_quarters

    def perfect_of(run: SimRun) -> Candidate:
        value = saving(run.cost, run.end_soc_pct)
        return Candidate(value, value, 100.0 if whole and value >= MIN_PERFECT_SAVING else None, False, run.work)

    plans = perfect_plans(slots, battery, start_soc_pct, wear, end_value, sell_margin=sell_margin)
    perfect = {name: perfect_of(run) for name, run in plans.items()}
    if site_mode not in perfect or MODE_MAX_PROFIT not in perfect:
        return None

    # Each way's saving, measured against the yardstick once its rules are known.
    ways: dict[str, Candidate] = {}

    def add(name: str, cost: float, end_soc: float, work: Work) -> None:
        ways[name] = Candidate(saving(cost, end_soc), 0.0, None, False, work)

    add(OWN, actual_cost, actual_end_soc_pct, measured_work(buckets, usable) if own_work is None else own_work)
    base = simulate(slots, battery, start_soc_pct, [(ACTION_SELF_CONSUME, 0.0)] * len(slots))
    add(SELF_CONSUMPTION, base.cost, base.end_soc_pct, base.work)
    for name in PLANNING_MODES:
        if name not in perfect or not all(name in bucket.ran_modes for bucket in buckets):
            continue  # a mode not planned beside the plan all day has no run of its own
        run = simulate(slots, battery, start_soc_pct, [bucket.ran_modes[name] for bucket in buckets])
        add(name, run.cost, run.end_soc_pct, run.work)

    if any(item.work.to_grid_kwh >= SELL_KWH for item in ways.values()):
        rules, best = RULES_SELL, perfect[MODE_MAX_PROFIT]
    else:
        runs = perfect_plans(slots, battery, start_soc_pct, wear, end_value, (NO_SELL_MODE,))
        if RULES_NO_SELL not in runs:
            return None
        rules, best = RULES_NO_SELL, perfect_of(runs[RULES_NO_SELL])
    candidates = {name: against(item, best.saving, whole) for name, item in ways.items()}
    return Facit(
        computed_at=now,
        end_value=end_value,
        basis=basis,
        mode=site_mode,
        no_battery_cost=no_battery_cost,
        quarters=day_quarters,
        measured=len(buckets),
        perfect=perfect,
        candidates=candidates,
        rules=rules,
        best=best,
    )


# ------------------------------------------------------------------------------------------------ cell temperature


@dataclass(frozen=True, slots=True)
class CellTemperature:
    """The day's cell temperature (°C): mean, maximum and minutes above `HOT_C`, over `minutes` read."""

    mean_c: float
    max_c: float
    minutes_above: int
    minutes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "mean_c": round(self.mean_c, 1),
            "max_c": round(self.max_c, 1),
            "minutes_above": self.minutes_above,
            "minutes": self.minutes,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CellTemperature | None:
        if not isinstance(raw, Mapping):
            return None
        try:
            return cls(float(raw["mean_c"]), float(raw["max_c"]), int(raw["minutes_above"]), int(raw["minutes"]))
        except (KeyError, TypeError, ValueError):
            return None


def cell_temperature(rows: Iterable[tuple[datetime, float | None, float | None]], step_min: int) -> CellTemperature | None:
    """The day from the recorder's statistics rows `(start, mean, max)` of [step_min] minutes each; a period above
    `HOT_C` on its mean counts whole. `None` without a row."""
    means: list[float] = []
    peak: float | None = None
    above = 0
    for _start, mean, high in rows:
        if not _finite(mean):
            continue
        means.append(float(mean))  # type: ignore[arg-type]
        top = float(high) if _finite(high) else float(mean)  # type: ignore[arg-type]
        peak = top if peak is None else max(peak, top)
        if mean > HOT_C:  # type: ignore[operator]
            above += step_min
    if not means or peak is None:
        return None
    return CellTemperature(sum(means) / len(means), peak, above, len(means) * step_min)

