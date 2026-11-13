"""A simple switched load (a water heater first) run in the cheapest hours, in shadow: the policy.

The load is one switch on the site with a rated power. The need is a minimum runtime in every rolling 24 hours (Power
Saver's "N hours within every 24 hours", `plans/research_integrations/ha_ecosystem_gap_2026-11-14.md` item 5). Runtime
and not energy, since a water heater's own thermostat cuts it off when the tank is hot: the switch decides when it
may run, not what it draws. Its daily energy is shown as runtime times the rated power.

* **The deadlines** (`deadlines`): from what the load ran in the last 24 hours (`RunHistory`, the shadow's own runs),
  the quarter hours it must have run by each coming moment so that no 24-hour window falls short. Before the history
  starts the need counts as met just then (`RunHistory.tracked_since`), so the first day is planned within 24 hours,
  not at once.
* **The hours** (`plan_switch`): for each deadline in turn, the cheapest quarter hours still free before it, by the car
  planner's own search (`planning/planner.cheapest_slots`, automatic periods: a start cost per run and runs of at
  least half an hour). A quarter hour whose clock hour leaves too little room under the site's import cap (the main
  fuse less its margin, or the import target's held level) beside the house's expected load and the cars' plans is
  never chosen (`blocked`). A deadline past the last published price waits for the next prices (`deferred`).
* **Now** (`decide`): on in a chosen quarter hour; on when a deadline has no slack left (every quarter hour to it is
  needed, also with no prices at all); on below the **comfort floor** (a temperature sensor read below the floor, until
  it is `FLOOR_HYSTERESIS_C` above it: SEM #1338's cold shower), whatever the price. Off whenever the site's measured
  headroom cannot take the load's current: the fuse comes first, also before the floor.

The load's current per phase is an assumption, there being no setting for it: single-phase up to `SINGLE_PHASE_MAX_KW`
(16 A), three-phase above.

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from ..planning.planner import (
    AUTO_PERIODS,
    STEP_MINUTES,
    FiscalChoice,
    PlanningSlot,
    cheapest_slots,
    effective_minor_per_kwh,
)

#: The site entry's key holding the load's settings (`SwitchLoadConfig.as_dict`); absent: no load.
CONF_SWITCH_LOAD: Final = "switch_load"
KEY_ENTITY: Final = "entity_id"
KEY_RATED_KW: Final = "rated_kw"
KEY_RUNTIME_H: Final = "runtime_h"
KEY_TEMPERATURE: Final = "temperature_entity"
KEY_FLOOR_C: Final = "floor_c"

#: The switch domains a load may be.
SWITCH_DOMAINS: Final = ("switch", "input_boolean")

RATED_KW_MIN: Final = 0.1
RATED_KW_MAX: Final = 30.0
RUNTIME_H_MIN: Final = 0.25
RUNTIME_H_MAX: Final = 24.0
FLOOR_C_MIN: Final = 5.0
FLOOR_C_MAX: Final = 85.0

#: The rolling window the runtime is due in.
WINDOW: Final = timedelta(hours=24)
QUARTER: Final = timedelta(minutes=STEP_MINUTES)
QUARTER_S: Final = QUARTER.total_seconds()
#: Below the floor the load runs until the temperature is this much above it.
FLOOR_HYSTERESIS_C: Final = 2.0
#: Up to this rating the load is taken to be single-phase (16 A at 230 V); above it, three-phase.
SINGLE_PHASE_MAX_KW: Final = 3.7

#: Why the load would be on or off now.
REASON_CHEAP: Final = "cheap_hour"
REASON_DEADLINE: Final = "deadline"
REASON_FLOOR: Final = "below_floor"
REASON_FUSE: Final = "fuse"
REASON_WAITING: Final = "waiting_for_cheaper"
REASON_NO_PRICES: Final = "waiting_for_prices"
#: Off with nothing to run within the published prices.
REASON_MET: Final = "need_met"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quarter_start(moment: datetime) -> datetime:
    """The start of the quarter hour `moment` falls in, in UTC."""
    utc = moment.astimezone(timezone.utc)
    return utc.replace(minute=utc.minute - utc.minute % STEP_MINUTES, second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class SwitchLoadConfig:
    """The load's settings, as the options flow stores them."""

    entity_id: str
    rated_kw: float
    runtime_h: float
    temperature_entity: str | None = None
    floor_c: float | None = None

    @property
    def needed_quarters(self) -> int:
        """The runtime in whole quarter hours (rounded up)."""
        return max(1, math.ceil(self.runtime_h * 3600.0 / QUARTER_S - 1e-9))

    @property
    def daily_kwh(self) -> float:
        """What the runtime is as energy at the rated power: the most the load can draw in 24 hours of its need."""
        return self.rated_kw * self.runtime_h

    @property
    def phases(self) -> int:
        return 1 if self.rated_kw <= SINGLE_PHASE_MAX_KW else 3

    def phase_amps(self, volts_between_phases: float) -> float:
        """The current the load draws on each phase it uses (`SINGLE_PHASE_MAX_KW` decides how many)."""
        phase_v = volts_between_phases / math.sqrt(3)
        return self.rated_kw * 1000.0 / (phase_v * self.phases)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {KEY_ENTITY: self.entity_id, KEY_RATED_KW: self.rated_kw, KEY_RUNTIME_H: self.runtime_h}
        if self.temperature_entity is not None and self.floor_c is not None:
            data[KEY_TEMPERATURE] = self.temperature_entity
            data[KEY_FLOOR_C] = self.floor_c
        return data

    @classmethod
    def from_data(cls, data: Any) -> SwitchLoadConfig | None:
        """The load from the entry's `CONF_SWITCH_LOAD`, or `None` when there is none or it is broken."""
        if not isinstance(data, Mapping):
            return None
        entity_id, rated, runtime = data.get(KEY_ENTITY), data.get(KEY_RATED_KW), data.get(KEY_RUNTIME_H)
        if not isinstance(entity_id, str) or entity_id.split(".", 1)[0] not in SWITCH_DOMAINS:
            return None
        if not _finite(rated) or not RATED_KW_MIN <= rated <= RATED_KW_MAX:
            return None
        if not _finite(runtime) or not RUNTIME_H_MIN <= runtime <= RUNTIME_H_MAX:
            return None
        sensor, floor = data.get(KEY_TEMPERATURE), data.get(KEY_FLOOR_C)
        if not (isinstance(sensor, str) and sensor and _finite(floor) and FLOOR_C_MIN <= floor <= FLOOR_C_MAX):
            sensor, floor = None, None
        return cls(entity_id, float(rated), float(runtime), sensor, None if floor is None else float(floor))


@dataclass(slots=True)
class RunHistory:
    """When the load ran (in shadow: when the shadow would have run it), kept for the rolling window.

    `tracked_since` is when the history began: the window before it counts as having had the whole need just before
    then, so a new load (or a new history) is planned within a day instead of at once."""

    tracked_since: datetime | None = None
    runs: list[tuple[datetime, datetime]] = field(default_factory=list)

    def note(self, start: datetime, end: datetime) -> None:
        """The load ran over `[start, end)`; joined to the last run when they touch."""
        if end <= start:
            return
        if self.tracked_since is None:
            self.tracked_since = start
        if self.runs and start <= self.runs[-1][1]:
            first, last = self.runs[-1]
            self.runs[-1] = (first, max(last, end))
        else:
            self.runs.append((start, end))

    def prune(self, now: datetime) -> None:
        oldest = now - WINDOW - QUARTER
        self.runs = [(start, end) for start, end in self.runs if end > oldest]

    def on_seconds(self, start: datetime, end: datetime, *, assumed_s: float = 0.0) -> float:
        """How long the load ran within `[start, end)`, with `assumed_s` counted as run just before the quarter hour
        `tracked_since` falls in."""
        total = 0.0
        runs: list[tuple[datetime, datetime]] = list(self.runs)
        if assumed_s > 0 and self.tracked_since is not None:
            # Ending at the start of its quarter hour, so a plan made in that quarter hour counts all of it.
            anchor = quarter_start(self.tracked_since)
            runs.append((anchor - timedelta(seconds=assumed_s), anchor))
        for first, last in runs:
            overlap = (min(end, last) - max(start, first)).total_seconds()
            if overlap > 0:
                total += overlap
        return total

    def as_dict(self) -> dict[str, Any]:
        return {
            "tracked_since": None if self.tracked_since is None else self.tracked_since.isoformat(),
            "runs": [[start.isoformat(), end.isoformat()] for start, end in self.runs],
        }

    @classmethod
    def from_dict(cls, data: Any) -> RunHistory:
        history = cls()
        if not isinstance(data, Mapping):
            return history
        history.tracked_since = _aware(data.get("tracked_since"))
        rows = data.get("runs")
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, list) and len(row) == 2:
                start, end = _aware(row[0]), _aware(row[1])
                if start is not None and end is not None and end > start:
                    history.runs.append((start, end))
        history.runs.sort()
        return history


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def deadlines(history: RunHistory, now: datetime, runtime_h: float) -> tuple[tuple[datetime, int], ...]:
    """`(moment, quarter hours)`: how many quarter hours the load must run from the running quarter hour on, by each
    moment, for every 24-hour window to hold the runtime; only the moments where that number grows. The last is at
    most one window ahead, needing the whole runtime."""
    need_s = runtime_h * 3600.0
    first = quarter_start(now)
    if history.tracked_since is None:
        # No history yet: it begins now.
        history = RunHistory(now, list(history.runs))
    steps: list[tuple[datetime, int]] = []
    last = 0
    for index in range(1, int(WINDOW / QUARTER) + 1):
        moment = first + QUARTER * index
        ran = history.on_seconds(moment - WINDOW, first, assumed_s=need_s)
        quarters = max(0, math.ceil((need_s - ran) / QUARTER_S - 1e-9))
        if quarters > last:
            steps.append((moment, quarters))
            last = quarters
    return tuple(steps)


@dataclass(frozen=True, slots=True)
class SwitchPlan:
    """The quarter hours the load would run in, and what is known about the rest of its need."""

    made_at: datetime
    #: The chosen quarter hours' starts (UTC), in time order, with their price (minor unit per kWh, with fees).
    quarters: tuple[tuple[datetime, float], ...]
    steps: tuple[tuple[datetime, int], ...]
    #: The end of the last published price; `None` without prices.
    priced_until: datetime | None
    #: Quarter hours still needed by a deadline after `priced_until`: planned when the next prices come.
    deferred: int = 0
    #: Quarter hours a deadline inside the prices could not have (too few free, or blocked by the import cap).
    short: int = 0
    #: The clock hours (UTC starts) the import cap leaves too little room in for the load.
    blocked_hours: tuple[datetime, ...] = ()

    @property
    def starts(self) -> frozenset[datetime]:
        return frozenset(start for start, _price in self.quarters)

    def cost_minor(self, rated_kw: float) -> float:
        return sum(price * rated_kw * QUARTER_S / 3600.0 for _start, price in self.quarters)

    def as_dict(self, rated_kw: float) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "quarters": [{"start": start.isoformat(), "price_minor_per_kwh": round(price, 3)} for start, price in self.quarters],
            "periods": [[first.isoformat(), last.isoformat()] for first, last in periods(self.starts)],
            "deadlines": [{"by": moment.isoformat(), "quarters": count} for moment, count in self.steps],
            "priced_until": None if self.priced_until is None else self.priced_until.isoformat(),
            "deferred_quarters": self.deferred,
            "short_quarters": self.short,
            "blocked_hours": [start.isoformat() for start in self.blocked_hours],
            "estimated_cost_minor": round(self.cost_minor(rated_kw), 2),
        }


def periods(starts: Iterable[datetime]) -> list[tuple[datetime, datetime]]:
    """Chosen quarter hours merged into `[start, end)` runs."""
    runs: list[list[datetime]] = []
    for start in sorted(starts):
        if runs and runs[-1][1] == start:
            runs[-1][1] = start + QUARTER
        else:
            runs.append([start, start + QUARTER])
    return [(first, last) for first, last in runs]


def priced_slots(slots: Sequence[PlanningSlot], now: datetime) -> list[PlanningSlot]:
    """The published quarter hours from the running one on, up to the first gap."""
    first = quarter_start(now)
    out: list[PlanningSlot] = []
    for slot in sorted((slot for slot in slots if slot.start >= first), key=lambda slot: slot.start):
        if out and slot.start != out[-1].end:
            break
        if not out and slot.start != first:
            break
        out.append(slot)
    return out


def plan_switch(
    *,
    now: datetime,
    config: SwitchLoadConfig,
    history: RunHistory,
    slots: Sequence[PlanningSlot],
    fiscal: FiscalChoice,
    room_kw_by_hour: Mapping[datetime, float],
    hour_of: Callable[[datetime], datetime],
) -> SwitchPlan:
    """The cheapest quarter hours that keep every deadline (`deadlines`) inside the published prices, by the car
    planner's search; quarter hours in an hour with less room than the rated power (`room_kw_by_hour`, by `hour_of` the slot's
    start; an hour it does not name is not blocked) are left out."""
    steps = deadlines(history, now, config.runtime_h)
    candidates = priced_slots(slots, now)
    priced_until = candidates[-1].end if candidates else None
    blocked = sorted(
        {
            hour_of(slot.start)
            for slot in candidates
            if (room := room_kw_by_hour.get(hour_of(slot.start))) is not None and room < config.rated_kw
        }
    )
    blocked_set = set(blocked)
    usable = [slot for slot in candidates if hour_of(slot.start) not in blocked_set]
    per_slot = config.rated_kw * QUARTER_S / 3600.0

    def pick(pool: list[PlanningSlot], count: int, end: datetime) -> list[PlanningSlot]:
        if count <= 0:
            return []
        if len(pool) <= count:
            return pool
        found = cheapest_slots(pool, count, per_slot, AUTO_PERIODS, fiscal, end)
        return list(found) if found is not None else sorted(pool, key=lambda slot: slot.local_major_per_kwh)[:count]

    def before(starts: Iterable[datetime], moment: datetime) -> int:
        return sum(1 for start in starts if start + QUARTER <= moment)

    inside = [step for step in steps if priced_until is not None and step[0] <= priced_until]
    deferred = steps[-1][1] - inside[-1][1] if steps and inside else (steps[-1][1] if steps else 0)
    fixed: dict[datetime, PlanningSlot] = {}
    chosen: dict[datetime, PlanningSlot] = {}
    short_steps: set[datetime] = set()
    # The whole need by the last deadline in one search (runs of at least half an hour); where that leaves an earlier
    # deadline short, that deadline's need is fixed first in its own search, and the rest searched again.
    for _round in range(len(inside) + 1):
        if not inside:
            break
        last_moment, last_need = inside[-1]
        rest = [slot for slot in usable if slot.end <= last_moment and slot.start not in fixed]
        trial = {**fixed, **{slot.start: slot for slot in pick(rest, last_need - len(fixed), last_moment)}}
        chosen = trial
        violated = next(
            (
                (moment, need)
                for moment, need in inside
                if moment not in short_steps and before(trial, moment) < need
            ),
            None,
        )
        if violated is None:
            break
        moment, need = violated
        pool = [slot for slot in usable if slot.end <= moment and slot.start not in fixed]
        more = need - before(fixed, moment)
        if len(pool) < more:
            short_steps.add(moment)
        fixed.update((slot.start, slot) for slot in pick(pool, more, moment))
    short = max((need - before(chosen, moment) for moment, need in inside), default=0)
    short = max(0, short)
    quarters = tuple(
        (start, effective_minor_per_kwh(chosen[start].local_major_per_kwh, fiscal, start)) for start in sorted(chosen)
    )
    return SwitchPlan(
        made_at=now,
        quarters=quarters,
        steps=steps,
        priced_until=priced_until,
        deferred=deferred,
        short=short,
        blocked_hours=tuple(blocked),
    )


def no_slack(steps: Iterable[tuple[datetime, int]], now: datetime) -> bool:
    """Whether some deadline needs every quarter hour from the running one up to it."""
    first = quarter_start(now)
    return any(need > 0 and need >= round((moment - first) / QUARTER) for moment, need in steps)


@dataclass(frozen=True, slots=True)
class SwitchInputs:
    now: datetime
    config: SwitchLoadConfig
    plan: SwitchPlan | None
    history: RunHistory
    #: The temperature sensor's reading (°C), `None` when there is no sensor or it cannot be read.
    temperature_c: float | None = None
    #: Whether the load was running for the floor at the last decision.
    floor_active: bool = False
    #: The site's least headroom over the phases (A), `None` when unknown.
    headroom_a: float | None = None
    #: Whether the switch reads on now (its draw is then in the headroom already).
    switch_on: bool = False
    volts_between_phases: float = 400.0


@dataclass(frozen=True, slots=True)
class SwitchDecision:
    on: bool
    reason: str
    floor_active: bool
    detail: Mapping[str, Any] = field(default_factory=dict)


def floor_state(config: SwitchLoadConfig, temperature_c: float | None, active: bool) -> bool:
    """Whether the load runs for the comfort floor: from below the floor until `FLOOR_HYSTERESIS_C` above it. An
    unreadable sensor keeps no floor running (it cannot tell when to stop)."""
    if config.floor_c is None or temperature_c is None:
        return False
    if temperature_c < config.floor_c:
        return True
    if temperature_c >= config.floor_c + FLOOR_HYSTERESIS_C:
        return False
    return active


def _off_reason(plan: SwitchPlan | None, first: datetime) -> str:
    if plan is None or plan.priced_until is None:
        return REASON_NO_PRICES
    return REASON_WAITING if any(start > first for start in plan.starts) else REASON_MET


def decide(inputs: SwitchInputs) -> SwitchDecision:
    """On or off now, and why (module docstring)."""
    config = inputs.config
    floor = floor_state(config, inputs.temperature_c, inputs.floor_active)
    first = quarter_start(inputs.now)
    steps = deadlines(inputs.history, inputs.now, config.runtime_h)
    detail: dict[str, Any] = {}
    if floor:
        want = REASON_FLOOR
    elif inputs.plan is not None and first in inputs.plan.starts:
        want = REASON_CHEAP
    elif no_slack(steps, inputs.now):
        want = REASON_DEADLINE
    else:
        return SwitchDecision(False, _off_reason(inputs.plan, first), floor)
    amps = config.phase_amps(inputs.volts_between_phases)
    if inputs.headroom_a is not None:
        room = inputs.headroom_a + (amps if inputs.switch_on else 0.0)
        detail = {"wanted": want, "load_a": round(amps, 1), "room_a": round(room, 1)}
        if room < amps:
            return SwitchDecision(False, REASON_FUSE, floor, detail)
    return SwitchDecision(True, want, floor, detail)


@dataclass(slots=True)
class DayTally:
    """One local day's runtime and its cost at the rated power: the shadow's and the switch's own (what it really did,
    assuming it drew the rated power while on)."""

    shadow_s: float = 0.0
    shadow_minor: float = 0.0
    actual_s: float = 0.0
    actual_minor: float = 0.0

    def add(self, seconds: float, price_minor: float | None, rated_kw: float, *, shadow: bool, actual: bool) -> None:
        cost = 0.0 if price_minor is None else price_minor * rated_kw * seconds / 3600.0
        if shadow:
            self.shadow_s += seconds
            self.shadow_minor += cost
        if actual:
            self.actual_s += seconds
            self.actual_minor += cost

    def as_dict(self) -> dict[str, float]:
        return {
            "shadow_h": round(self.shadow_s / 3600.0, 3),
            "shadow_cost_minor": round(self.shadow_minor, 2),
            "actual_h": round(self.actual_s / 3600.0, 3),
            "actual_cost_minor": round(self.actual_minor, 2),
        }

    @classmethod
    def from_dict(cls, data: Any) -> DayTally:
        if not isinstance(data, Mapping):
            return cls()
        values = [data.get(key) for key in ("shadow_h", "shadow_cost_minor", "actual_h", "actual_cost_minor")]
        shadow_h, shadow_minor, actual_h, actual_minor = (float(value) if _finite(value) else 0.0 for value in values)
        return cls(shadow_h * 3600.0, shadow_minor, actual_h * 3600.0, actual_minor)
