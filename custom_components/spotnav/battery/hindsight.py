"""The day against a perfect plan in hindsight, the battery's work and its cells' temperature. Pure: no Home Assistant.

The night's review (`plan_shadow.review_day`) runs each way of driving the battery over the measured day against plain
self-consumption. This module adds the yardstick a person can read at a glance: the best the battery could have done
that day had everything been known, and each way's share of it.

* **The perfect plan** (`perfect_plans`): the existing planner (`plan.plan_mode`), planned once over the day's
  measured quarter hours with the measured house load, sun and cars (`measured_slot`), the day's real prices with
  their fees, the same battery (its floor, ceiling, rated power and round trip), the mode's wear and the mode's
  selling rule (no selling unless the mode allows it; a quarter hour with a sell price below zero exports nothing, as
  the plan in force has it, `export_curtail.curtail_slots`). One per planning mode; the site's mode is the headline's.
  A quarter hour the meters lost counts in no run, as in the review: the plan steps over it.
* **The day boundary** (fairness): every way (what really happened, which is the battery's own optimiser on a battery
  that runs its own way; each SpotNav mode; the perfect plan) starts the day at the same measured SoC, and the energy
  each leaves at midnight is valued the same way (`end_value_ahead`): at its best use over the next `AHEAD_HOURS` of
  known prices (the following days' prices are known for a finished day: the next day's are published at 13:00),
  less the round trip's loss. Its best use is the dearest quarter hours that a full battery could cover, each at most
  the battery's rated discharge and the house's own load at that time of day (the finished day's, as measured). Until
  those prices are known the review's own end value (the day's median buy price times the round trip) stands in,
  and the figure says so (`basis`); it is worked out again once they are. The perfect plan values its own end the
  same way, so it plans for the night after.
* **Shares** (`Facit.candidates`): each way's saving against the day with no battery at all (`review_baselines`),
  with the energy left at midnight counted at that value, as a share of the perfect plan's: the site's mode's for
  what really happened and for self-consumption, the mode's own for each SpotNav mode. Shares are clamped to 0-100 %
  (`clamped` says when one was): above 100 only by the model's ideal battery or the wear the perfect plan weighs but
  money does not count; below 0 a way that cost more than no battery. `None` on a day when the perfect plan saves
  nothing (nothing to gain).
* **Work** (`Work`): per way, the kWh discharged and the cycles (that over the usable capacity), how often it switched
  between charging and discharging (idle quarter hours not counted), the kWh charged from the grid and the kWh the
  battery sold to the grid.
* **Cell temperature** (`cell_temperature`): the day's mean, maximum and minutes above `HOT_C`, from the recorder's
  five-minute (else hourly) statistics of the battery's own cell temperature, where the battery reports it.

What it cannot tell: the model's battery is ideal (the real one has its own losses and ramps), so what really happened
is held to a slightly kinder standard than it met; the perfect plan sees only the measured quarter hours.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from .export_curtail import curtail_slots
from .plan import action_power, plan_mode, run_slot
from .plan_model import (
    ACTION_SELF_CONSUME,
    MODE_DEFS,
    BatteryParams,
    PlanRequest,
    PlanSlot,
)

#: How far after the day's end the energy left is valued (h), and the quarter hour.
AHEAD_HOURS: Final = 48
QUARTER: Final = timedelta(minutes=15)
#: A cell temperature above this (°C) counts as warm.
HOT_C: Final = 30.0
#: A battery power below this (kW) is idle: it neither charges nor discharges (`plan_shadow.IDLE_KW`).
IDLE_KW: Final = 0.2

# Where the end value came from.
BASIS_AHEAD: Final = "ahead"
BASIS_DAY: Final = "day_median"
BASES: Final = (BASIS_AHEAD, BASIS_DAY)

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


def house_profile(buckets: Sequence[MeasuredQuarter], day_start: datetime) -> dict[int, float]:
    """The finished day's house load (kW, the house less the sun, never below 0) by quarter hour of the day."""
    out: dict[int, float] = {}
    for bucket in buckets:
        net = bucket.net_house_kw
        if net is None:
            continue
        index = int((bucket.start - day_start) / QUARTER) % 96
        out[index] = max(0.0, net)
    return out


def end_value_ahead(
    prices: Sequence[PricedSlot],
    day_end: datetime,
    battery: BatteryParams,
    profile: Mapping[int, float],
    hours: int = AHEAD_HOURS,
) -> float | None:
    """What a kWh left in the battery at [day_end] is worth (minor units, see the module); `None` while the prices do
    not cover the [hours] after it. [profile] is the house's load by quarter hour of the day; a quarter hour it has no
    figure for takes the battery's rated discharge."""
    horizon = day_end + timedelta(hours=hours)
    covered = [slot for slot in prices if slot.end > day_end and slot.start < horizon and _finite(slot.buy)]
    quarters: list[tuple[float, float]] = []
    seen = 0.0
    for slot in sorted(covered, key=lambda item: item.start):
        start, end = max(slot.start, day_end), min(slot.end, horizon)
        seen += (end - start).total_seconds()
        moment = start
        while moment < end:
            step = min(end, moment + QUARTER)
            index = int((moment - day_end) / QUARTER) % 96
            kw = min(battery.max_discharge_kw, profile.get(index, battery.max_discharge_kw))
            quarters.append((float(slot.buy), max(0.0, kw) * (step - moment).total_seconds() / 3600.0))
            moment = step
    if seen < hours * 3600 - 60:
        return None
    room = battery.max_kwh - battery.min_kwh
    left = room * battery.one_way_efficiency  # the AC energy a full battery gives
    worth = used = 0.0
    for price, kwh in sorted(quarters, key=lambda item: -item[0]):
        if left <= 0:
            break
        take = min(kwh, left)
        worth += price * take
        used += take
        left -= take
    if used <= 0:
        return statistics.median(price for price, _kwh in quarters) * battery.round_trip_efficiency if quarters else None
    return worth / used * battery.round_trip_efficiency


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
    """What the battery really did, from the meters."""
    steps = []
    for bucket in buckets:
        hours = bucket.covered_s / 3600.0
        if hours <= 0:
            continue
        steps.append(((bucket.battery_kwh) / hours, (bucket.import_kwh - bucket.export_kwh) / hours, QUARTER.total_seconds() / 3600.0))
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
    modes: Iterable[str] = PLANNING_MODES,
) -> dict[str, SimRun]:
    """Each planning mode's perfect plan in hindsight over the measured [slots] (see the module), run as the review
    runs a way."""
    chained = list(curtail_slots(_chain(slots)))
    out: dict[str, SimRun] = {}
    if not chained:
        return out
    soc = min(100.0, max(0.0, start_soc_pct))
    for name in modes:
        request = PlanRequest(slots=tuple(chained), battery=battery, soc_now_pct=soc, end_value=end_value)
        made = plan_mode(request, MODE_DEFS[name], wear)
        actions = [(decision.action, decision.battery_kw) for decision in made.plan.decisions]
        out[name] = simulate(chained, battery, soc, actions)
    return out


# ------------------------------------------------------------------------------------------------ the facit


@dataclass(frozen=True, slots=True)
class Candidate:
    """One way against no battery: its saving with the energy left at midnight counted, the perfect plan's it is
    measured against, its share (0-100, `None`: nothing to gain), whether the share was clamped, and its work."""

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


def share_of(saving: float, perfect: float) -> tuple[float | None, bool]:
    """[saving] as a percentage of [perfect], clamped to 0-100, and whether it was clamped; `None` when the perfect
    plan saves nothing."""
    if perfect <= 0.5:  # under half an öre: nothing to gain
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
    #: The mode whose rules the headline's perfect plan kept (the site's).
    mode: str
    no_battery_cost: float
    #: The perfect plan's saving against no battery, per planning mode, and its work.
    perfect: dict[str, Candidate] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)

    @property
    def saving(self) -> float | None:
        """The headline: the perfect plan's saving under the site's mode."""
        item = self.perfect.get(self.mode)
        return None if item is None else item.saving

    def as_dict(self) -> dict[str, Any]:
        return {
            "computed_at": self.computed_at.isoformat(),
            "end_value": round(self.end_value, 2),
            "basis": self.basis,
            "mode": self.mode,
            "no_battery_cost": round(self.no_battery_cost, 2),
            "perfect": {name: item.as_dict() for name, item in sorted(self.perfect.items())},
            "candidates": {name: item.as_dict() for name, item in sorted(self.candidates.items())},
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Facit | None:
        if not isinstance(raw, Mapping):
            return None
        try:
            computed_at = datetime.fromisoformat(raw["computed_at"])
            if computed_at.tzinfo is None or raw["basis"] not in BASES or raw["mode"] not in PLANNING_MODES:
                return None
            return cls(
                computed_at=computed_at,
                end_value=float(raw["end_value"]),
                basis=raw["basis"],
                mode=raw["mode"],
                no_battery_cost=float(raw["no_battery_cost"]),
                perfect={k: Candidate.from_dict(v) for k, v in raw["perfect"].items() if k in PLANNING_MODES},
                candidates={k: Candidate.from_dict(v) for k, v in raw["candidates"].items() if k in CANDIDATES},
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


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
    now: datetime,
) -> Facit | None:
    """The day against its perfect plan (see the module). [buckets] are the review's usable quarter hours, oldest
    first; [actual_cost] and [actual_end_soc_pct] what really happened (the review's); `None` without a quarter hour."""
    if not buckets:
        return None
    site_mode = mode if mode in PLANNING_MODES else PLANNING_MODES[0]
    slots = [measured_slot(bucket) for bucket in buckets]
    usable = battery.usable_kwh

    def net(cost: float, end_soc: float) -> float:
        return cost - (end_soc - start_soc_pct) / 100 * usable * end_value

    def saving(cost: float, end_soc: float) -> float:
        return no_battery_cost - net(cost, end_soc)

    perfect_runs = perfect_plans(slots, battery, start_soc_pct, wear, end_value)
    perfect: dict[str, Candidate] = {}
    for name, run in perfect_runs.items():
        value = saving(run.cost, run.end_soc_pct)
        perfect[name] = Candidate(value, value, 100.0 if value > 0.5 else None, False, run.work)
    headline = perfect.get(site_mode)
    if headline is None:
        return None

    candidates: dict[str, Candidate] = {}

    def add(name: str, cost: float, end_soc: float, work: Work, against: float) -> None:
        value = saving(cost, end_soc)
        share, clamped = share_of(value, against)
        candidates[name] = Candidate(value, against, share, clamped, work)

    add(OWN, actual_cost, actual_end_soc_pct, measured_work(buckets, usable), headline.saving)
    base = simulate(slots, battery, start_soc_pct, [(ACTION_SELF_CONSUME, 0.0)] * len(slots))
    add(SELF_CONSUMPTION, base.cost, base.end_soc_pct, base.work, headline.saving)
    for name in PLANNING_MODES:
        if name not in perfect or not all(name in bucket.ran_modes for bucket in buckets):
            continue  # a mode not planned beside the plan all day has no run of its own
        run = simulate(slots, battery, start_soc_pct, [bucket.ran_modes[name] for bucket in buckets])
        add(name, run.cost, run.end_soc_pct, run.work, perfect[name].saving)
    return Facit(
        computed_at=now,
        end_value=end_value,
        basis=basis,
        mode=site_mode,
        no_battery_cost=no_battery_cost,
        perfect=perfect,
        candidates=candidates,
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

