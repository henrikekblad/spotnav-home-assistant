"""Hold the house battery while the car charges from the grid (evcc's "discharge lock"), decided in shadow.

This part decides **when** the battery should be held; **what** a hold writes, and how it is handed back, is the
executor's action `hold` (`executor/`, `commands.py`), the one write path a later opt-in will use. Until wave 7
the part kept its own write: the adapter's discharge-limit number set to 0, with its own snapshot, "unset" rule,
restore value, write cap and control checks. That duplicated the executor and differed from it (on Sigenergy the
executor's hold also turns Remote EMS on in Maximum Self Consumption, which firmware SPC117 needs before it obeys the
limit), so it was removed: at the moment the part would hold, it asks `executor.preview` for the action `hold`, and
logs exactly the writes, refusals and hand-back the executor would make. A future opt-in turns the part's decision
into the executor's action (`requested_action`) instead of writing anything itself. A brand without an executor
control (GoodWe, Enphase) can no longer be held, even in shadow.

The rules follow `plans/research_integrations/battery_control_2026-10-03.md` §3.2 and Predbat's field lessons
(`predbat_lowhanging_2026-11-13.md` item 2):

* Preconditions (the part's own): a SpotNav charger charges from the grid and the battery measurably discharges into
  it, the battery is free (its plain self-consumption: one that runs its own optimiser, a schedule or another planner
  is left alone) and its reading is fresh. A battery that charges (from the grid or the sun) is not held: it is not
  feeding the car, and a discharge cap can stop some inverters grid-charging (Predbat #5419, #3899).
* Preconditions (the executor's, via the preview): every control role found and enabled, the brand's own mode rules,
  on the grid, SpotNav's measurement healthy, the fuse keeps `HEADROOM_MARGIN_A` once the battery stops covering the
  car, the day's write cap (`executor.DAILY_WRITE_CAP`, counted from the previewed writes).
* Edge-triggered with hysteresis: grid charge and discharge must last `ARM_S` before a hold, a new hold waits
  `REARM_S` (the executor's) after a restore.
* The snapshot and the hand-back are the executor's: every role it would write is kept as found and written back
  (a never-set limit, Sigenergy's `unknown`, as the rating; never `unknown` itself).
* Restore at once when the car stops charging from the grid, the battery charges, a reading stays stale past the
  executor's `SILENCE_GRACE_S` (sooner when the headroom falls under `PEAK_COVER_A` meanwhile), the plant
  leaves the grid, the measurement is lost, the headroom falls under `RESTORE_HEADROOM_A`, the battery stops being
  free (judged as the executor judges it: the Remote EMS SpotNav's own hold turns on is not the plant's own
  management, and a reading that blinks is the executor's to judge after its grace) or a control role goes away;
  and first of all after a restart that finds a hold.

Everything is a decision: nothing is written. While shadowed a "hold" does not stop the battery, so the part keeps
judging from the battery's own readings. Each non-write outcome (left alone, not possible) is logged once per grid
charge. The part also counts the kWh the battery gave the car per day: energy discharged while a SpotNav charger drew
from the grid, at most the car's draw.

Pure: no Home Assistant import; times are aware datetimes from the caller.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final, Literal

from ..core.electric import TN_VOLTS_BETWEEN_PHASES
from .commands import SNAPSHOT, Native, control_for
from .executor import (
    BATTERY_MAX_AGE_S,
    PEAK_COVER_A,
    REARM_S,
    RESTORE_HEADROOM_A,
    SILENCE_GRACE_S,
    ExecInputs,
    Preview,
    RoleRead,
    management_problem,
    preview,
)
from .facts import BatteryFacts
from .plan_model import ACTION_HOLD
from .shadow import parse_aware_datetime

#: Discharging means below minus this (kW); charging, above it.
DISCHARGE_KW: Final = 0.2
#: Grid charge and battery discharge must last this long before a hold.
ARM_S: Final = 60.0
#: A reading gap longer than this adds nothing to the energy the battery gave the car.
ENERGY_MAX_GAP_S: Final = 60.0
#: Days of the "battery gave the car" figure kept.
ENERGY_DAYS: Final = 30

HoldAction = Literal["would_write", "would_restore", "left_alone", "not_possible"]


@dataclass(frozen=True, slots=True)
class HoldInputs:
    now: datetime
    grid_charging: bool
    facts: BatteryFacts
    battery_age_s: float | None = None
    measurement_ok: bool = True
    #: The least headroom on any phase now (A), `None` when unknown.
    headroom_a: float | None = None
    volts_between_phases: float = TN_VOLTS_BETWEEN_PHASES
    charger_ids: tuple[str, ...] = ()
    #: What the chargers charging from the grid draw together (kW), `None` when unknown.
    car_kw: float | None = None
    #: The brand's control roles as found now (`executor.role_reads`).
    reads: Mapping[str, RoleRead] = field(default_factory=dict)
    #: The person acknowledged that SpotNav may take the plant over from its own optimiser (Sigenergy's AI mode,
    #: `executor.ExecInputs.takeover`).
    takeover: bool = False
    #: The hold variant control would run (`gate.hold_variant_to_run`; `None`: the brand's first).
    variant: str | None = None


def _natives(raw: Any) -> dict[str, Native]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): value for name, value in raw.items()
        if isinstance(value, (str, bool)) or (isinstance(value, (int, float)) and math.isfinite(value))
    }


def _texts(raw: Any) -> dict[str, str]:
    return {str(k): v for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, Mapping) else {}


@dataclass(frozen=True, slots=True)
class HoldMemory:
    """What the part remembers between decisions; persisted with the shadow book."""

    held: bool = False
    held_since: datetime | None = None
    #: Per role the executor would write: the value found (as read), the entity, and what handing back would write.
    snapshot: dict[str, str] = field(default_factory=dict)
    entities: dict[str, str] = field(default_factory=dict)
    restore: dict[str, Native] = field(default_factory=dict)
    armed_since: datetime | None = None
    last_change: datetime | None = None
    day: str | None = None
    #: The writes the executor would have made today (restores not counted, as the executor counts them).
    writes_today: int = 0
    #: The non-write outcomes already logged in this grid charge.
    noted: tuple[str, ...] = ()
    #: The kWh the battery gave the car per local day (ISO date), and the last time it was counted.
    gave_car_kwh: dict[str, float] = field(default_factory=dict)
    counted_at: datetime | None = None
    #: Since when a hold's battery reading has been missing or stale (ridden out as the executor does); not stored.
    stale_since: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        def iso(moment: datetime | None) -> str | None:
            return None if moment is None else moment.isoformat()

        return {
            "held": self.held,
            "held_since": iso(self.held_since),
            "snapshot": dict(self.snapshot),
            "entities": dict(self.entities),
            "restore": dict(self.restore),
            "armed_since": iso(self.armed_since),
            "last_change": iso(self.last_change),
            "day": self.day,
            "writes_today": self.writes_today,
            "noted": list(self.noted),
            "gave_car_kwh": dict(self.gave_car_kwh),
            "counted_at": iso(self.counted_at),
        }

    @classmethod
    def from_dict(cls, data: Any) -> HoldMemory:
        """A stored memory; an earlier version's single snapshot text (its own discharge-limit write) loads as no
        snapshot: nothing was written either way."""
        if not isinstance(data, Mapping):
            return cls()
        gave = data.get("gave_car_kwh")
        writes = data.get("writes_today")
        day, noted = data.get("day"), data.get("noted")
        noted_reasons = tuple(item for item in noted if isinstance(item, str)) if isinstance(noted, list) else ()
        return cls(
            held=data.get("held") is True,
            held_since=parse_aware_datetime(data.get("held_since")),
            snapshot=_texts(data.get("snapshot")),
            entities=_texts(data.get("entities")),
            restore=_natives(data.get("restore")),
            armed_since=parse_aware_datetime(data.get("armed_since")),
            last_change=parse_aware_datetime(data.get("last_change")),
            day=day if isinstance(day, str) else None,
            writes_today=writes if isinstance(writes, int) and not isinstance(writes, bool) and writes >= 0 else 0,
            noted=noted_reasons,
            gave_car_kwh={
                key: float(value)
                for key, value in (gave.items() if isinstance(gave, Mapping) else ())
                if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0
            },
            counted_at=parse_aware_datetime(data.get("counted_at")),
        )


@dataclass(frozen=True, slots=True)
class HoldDecision:
    action: HoldAction
    reason: str
    #: The entity of the hold's main role (the discharge limit where the brand has one) and the writes as
    #: `{role: value}`.
    entity_id: str | None = None
    value: Any = None
    #: The main entity's state when the decision was made.
    read: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def requested_action(memory: HoldMemory) -> str | None:
    """The executor action this part asks for now (`hold` while it holds), for the opt-in that will run it."""
    return ACTION_HOLD if memory.held else None


def _fresh(inputs: HoldInputs) -> bool:
    age = inputs.battery_age_s
    return inputs.facts.power_kw is not None and age is not None and age <= BATTERY_MAX_AGE_S


def _main_role(brand: str | None, reads: Mapping[str, RoleRead], variant: str | None = None) -> str | None:
    """The first role, in the brand's order, that the hold sets to a value of its own (Sigenergy: the discharge
    limit, or the charge limit in mode 4), as found."""
    control = control_for(brand, variant)
    if control is None:
        return None
    wanted = control.wanted(ACTION_HOLD, 0.0)
    return next(
        (name for name in control.enter_order if wanted.get(name, SNAPSHOT) != SNAPSHOT and name in reads), None
    )


def _main(inputs: HoldInputs) -> tuple[str | None, str | None]:
    """The main role's entity and state."""
    name = _main_role(inputs.facts.control_brand, inputs.reads, inputs.variant)
    read = None if name is None else inputs.reads[name]
    return (None, None) if read is None else (read.entity_id, read.state)


def _exec_inputs(inputs: HoldInputs) -> ExecInputs:
    return ExecInputs(
        now=inputs.now,
        facts=inputs.facts,
        reads=inputs.reads,
        action=ACTION_HOLD,
        battery_age_s=inputs.battery_age_s,
        measurement_ok=inputs.measurement_ok,
        headroom_a=inputs.headroom_a,
        volts_between_phases=inputs.volts_between_phases,
        takeover=inputs.takeover,
    )


def would_hold(inputs: HoldInputs, writes_today: int = 0) -> Preview:
    """What the executor would write for a hold now (`executor.preview`); `no_brand_control` for a brand it has no
    commands for."""
    control = control_for(inputs.facts.control_brand, inputs.variant)
    if control is None:
        return Preview("no_brand_control")
    return preview(control, _exec_inputs(inputs), writes_today=writes_today)


def _count_energy(memory: HoldMemory, inputs: HoldInputs) -> HoldMemory:
    """Add what the battery gave the car since the last reading: its discharge, at most the car's draw."""
    now = inputs.now
    previous = memory.counted_at
    gave = dict(memory.gave_car_kwh)
    power = inputs.facts.power_kw
    if previous is not None and inputs.grid_charging and _fresh(inputs) and power is not None and power < 0:
        seconds = (now - previous).total_seconds()
        if 0 < seconds <= ENERGY_MAX_GAP_S:
            kw = -power if inputs.car_kw is None else min(-power, max(inputs.car_kw, 0.0))
            day = now.date().isoformat()
            gave[day] = gave.get(day, 0.0) + kw * seconds / 3600.0
    if len(gave) > ENERGY_DAYS:
        gave = dict(sorted(gave.items())[-ENERGY_DAYS:])
    return replace(memory, gave_car_kwh=gave, counted_at=now)


def _detail(inputs: HoldInputs) -> dict[str, Any]:
    facts = inputs.facts
    return {
        "power_kw": None if facts.power_kw is None else round(facts.power_kw, 3),
        "soc_pct": facts.soc_pct,
        "headroom_a": None if inputs.headroom_a is None else round(inputs.headroom_a, 2),
        "chargers": list(inputs.charger_ids),
        "car_kw": None if inputs.car_kw is None else round(inputs.car_kw, 3),
        "management": facts.management.reason,
        "firmware": facts.firmware,
    }


def _restore_decision(memory: HoldMemory, reason: str, read: str | None, detail: dict[str, Any]) -> HoldDecision:
    """The hand-back the executor would make: every role it wrote back to its snapshot, in the brand's order."""
    main = next(iter(memory.entities.values()), None)
    return HoldDecision(
        "would_restore", reason, main, dict(memory.restore), read,
        detail | {"snapshot": dict(memory.snapshot), "entities": dict(memory.entities)},
    )


def restart(memory: HoldMemory, facts: BatteryFacts, now: datetime) -> tuple[HoldMemory, HoldDecision | None]:
    """After a restart a hold found in memory is restored before anything else."""
    del facts  # the hand-back writes what was found before the hold, whatever the battery reads now
    if not memory.held:
        return memory, None
    return _released(memory, now), _restore_decision(memory, "restart", None, {})


def _released(memory: HoldMemory, now: datetime) -> HoldMemory:
    return replace(
        memory,
        held=False,
        held_since=None,
        snapshot={},
        entities={},
        restore={},
        armed_since=None,
        last_change=now,
        stale_since=None,
    )


def _restore_reason(inputs: HoldInputs, memory: HoldMemory) -> str | None:
    facts = inputs.facts
    power = facts.power_kw
    if not inputs.grid_charging:
        return "car_not_charging_from_grid"
    if facts.on_grid is False:
        return "off_grid"
    if not _fresh(inputs) and (
        memory.stale_since is None or (inputs.now - memory.stale_since).total_seconds() >= SILENCE_GRACE_S
        or inputs.headroom_a is None or inputs.headroom_a < PEAK_COVER_A
    ):
        # A plant silent a while (its link drops for a minute or two) is ridden out as the executor rides it out.
        return "battery_reading_stale"
    if power is not None and power > DISCHARGE_KW:
        return "battery_charging"
    if not inputs.measurement_ok:
        return "measurement_lost"
    if inputs.headroom_a is None or inputs.headroom_a < RESTORE_HEADROOM_A:
        return "headroom_low"
    control = control_for(facts.control_brand)
    if control is None:
        return "no_brand_control"
    # The hold's own writes turn Remote EMS on: that is SpotNav's, not the plant's own management. A reading that
    # blinks (`unknown`) is the executor's to judge after its grace.
    management = management_problem(facts, control, active=True, takeover=inputs.takeover)
    if management is not None and management != "unknown":
        return management
    for name in memory.entities:
        read = inputs.reads.get(name)
        if read is None or not read.found:
            return f"control_not_found:{name}"
    return None


def step(memory: HoldMemory, inputs: HoldInputs) -> tuple[HoldMemory, HoldDecision | None]:
    """One look: the new memory and the decision to log, if any."""
    now = inputs.now
    facts = inputs.facts
    today = now.date().isoformat()
    if memory.day != today:
        memory = replace(memory, day=today, writes_today=0)
    memory = _count_energy(memory, inputs)
    if not inputs.grid_charging:
        memory = replace(memory, noted=())

    if memory.held:
        memory = replace(memory, stale_since=None if _fresh(inputs) else memory.stale_since or now)
        reason = _restore_reason(inputs, memory)
        if reason is None:
            return memory, None
        _entity, read = _main(inputs)
        return _released(memory, now), _restore_decision(memory, reason, read, _detail(inputs))

    power = facts.power_kw
    if not (inputs.grid_charging and _fresh(inputs) and power is not None):
        return replace(memory, armed_since=None), None
    if power > DISCHARGE_KW:
        # The battery charges (from the grid or the sun): it is not feeding the car, so no hold is decided.
        return _note(replace(memory, armed_since=None), "not_possible", "battery_charging", inputs)
    if power >= -DISCHARGE_KW:
        return replace(memory, armed_since=None), None
    if memory.armed_since is None:
        return replace(memory, armed_since=now), None
    if (now - memory.armed_since).total_seconds() < ARM_S:
        return memory, None
    control = control_for(facts.control_brand)
    if control is None:
        management = None if facts.management.state == "free" else f"{facts.management.state}:{facts.management.reason}"
    else:
        management = management_problem(facts, control, active=True, takeover=inputs.takeover)
    if management is not None:
        return _note(memory, "left_alone", management, inputs)
    seen = would_hold(inputs, memory.writes_today)
    if seen.blocker is not None:
        return _note(memory, "not_possible", seen.blocker, inputs)
    if memory.last_change is not None and (now - memory.last_change).total_seconds() < REARM_S:
        return memory, None
    entity_id, read = _main(inputs)
    restores = {op.role: op.value for op in seen.restores}
    entities = {op.role: op.entity_id for op in seen.writes if op.entity_id is not None}
    snapshot = {name: value for name, value in seen.snapshot.items() if name in restores}
    decision = HoldDecision(
        "would_write",
        "battery_discharges_into_grid_charge",
        entity_id,
        {op.role: op.value for op in seen.writes},
        read,
        _detail(inputs) | {
            "writes": [op.as_dict() | {"read": _state(inputs, op.role)} for op in seen.writes],
            "restore": [op.as_dict() for op in seen.restores],
            "snapshot": snapshot,
        },
    )
    held = replace(
        memory,
        held=True,
        held_since=now,
        snapshot=snapshot,
        entities=entities,
        restore=restores,
        armed_since=None,
        last_change=now,
        writes_today=memory.writes_today + len(seen.writes),
        noted=(),
    )
    return held, decision


def _state(inputs: HoldInputs, role: str) -> str | None:
    read = inputs.reads.get(role)
    return None if read is None else read.state


def _note(memory: HoldMemory, action: HoldAction, reason: str, inputs: HoldInputs) -> tuple[HoldMemory, HoldDecision | None]:
    """A non-write outcome, logged once per grid charge; it says what the executor's hold would set."""
    if reason in memory.noted:
        return memory, None
    detail = _detail(inputs)
    control = control_for(inputs.facts.control_brand, inputs.variant)
    if control is not None:
        detail["would_write"] = control.wanted(ACTION_HOLD, 0.0)
    entity_id, read = _main(inputs)
    decision = HoldDecision(action, reason, entity_id, None, read, detail)
    return replace(memory, noted=(*memory.noted, reason)), decision
