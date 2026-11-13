"""Hold the house battery while the car charges from the grid (evcc's "discharge lock"), decided in shadow.

When a SpotNav charger charges from the grid (`execution/grid_charge.py`) and the battery measurably discharges
into it, the part would set the battery's discharge limit to 0 and later write the value it found back. The rules
follow `plans/research_integrations/battery_control_2026-10-03.md` §3.2 and Predbat's field lessons
(`predbat_lowhanging_2026-11-13.md` item 2):

* Preconditions: the battery is free (its plain self-consumption: one that runs its own optimiser, a schedule or
  another planner is left alone), the control is found, enabled and available, the battery reading is fresh, the
  plant is on the grid, SpotNav's own measurement is healthy, and the fuse keeps `HOLD_MIN_HEADROOM_A` once the
  battery stops covering the car. A battery that charges (from the grid or the sun) is not held: it is not feeding
  the car, and a discharge cap can stop some inverters grid-charging (Predbat #5419, #3899).
* Edge-triggered with hysteresis: grid charge and discharge must last `ARM_S` before a hold, a new hold waits
  `REARM_S` after a restore, and at most `DAILY_WRITE_CAP` writes a day (flash wear is unknown).
* Snapshot first: the value found is kept (persisted) before the limit would be written; a value at or above the
  rating, or the register's "never set", is "unset" and is restored as the rated discharge power.
* Restore at once when the car stops charging from the grid, the battery charges, a reading goes stale, the plant
  leaves the grid, the measurement is lost, the headroom falls under `RESTORE_HEADROOM_A`, the battery stops being
  free or the control goes away; and first of all after a restart that finds a hold.

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

from .facts import BatteryFacts, ControlStatus
from .shadow import parse_aware_datetime

#: Discharging means below minus this (kW); charging, above it.
DISCHARGE_KW: Final = 0.2
#: Grid charge and battery discharge must last this long before a hold.
ARM_S: Final = 60.0
#: After a restore, no new hold for this long.
REARM_S: Final = 120.0
DAILY_WRITE_CAP: Final = 20
#: A battery reading older than this is stale.
BATTERY_MAX_AGE_S: Final = 120.0
#: The fuse headroom that must remain once the battery's discharge comes from the grid instead.
HOLD_MIN_HEADROOM_A: Final = 2.0
#: Below this headroom a hold is released.
RESTORE_HEADROOM_A: Final = 1.0
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
    volts_between_phases: float = 400.0
    charger_ids: tuple[str, ...] = ()
    #: What the chargers charging from the grid draw together (kW), `None` when unknown.
    car_kw: float | None = None


@dataclass(frozen=True, slots=True)
class HoldMemory:
    """What the part remembers between decisions; persisted with the shadow book."""

    held: bool = False
    held_since: datetime | None = None
    #: The control's value found before the hold, as its state text, and whether it held no limit.
    snapshot: str | None = None
    snapshot_unset: bool = False
    armed_since: datetime | None = None
    last_change: datetime | None = None
    day: str | None = None
    writes_today: int = 0
    #: The non-write outcomes already logged in this grid charge.
    noted: tuple[str, ...] = ()
    #: The kWh the battery gave the car per local day (ISO date), and the last time it was counted.
    gave_car_kwh: dict[str, float] = field(default_factory=dict)
    counted_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        def iso(moment: datetime | None) -> str | None:
            return None if moment is None else moment.isoformat()

        return {
            "held": self.held,
            "held_since": iso(self.held_since),
            "snapshot": self.snapshot,
            "snapshot_unset": self.snapshot_unset,
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
        if not isinstance(data, Mapping):
            return cls()
        gave = data.get("gave_car_kwh")
        writes = data.get("writes_today")
        snapshot, day, noted = data.get("snapshot"), data.get("day"), data.get("noted")
        noted_reasons = tuple(item for item in noted if isinstance(item, str)) if isinstance(noted, list) else ()
        return cls(
            held=data.get("held") is True,
            held_since=parse_aware_datetime(data.get("held_since")),
            snapshot=snapshot if isinstance(snapshot, str) else None,
            snapshot_unset=data.get("snapshot_unset") is True,
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
    entity_id: str | None = None
    value: Any = None
    #: The control's state when the decision was made.
    read: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _fresh(inputs: HoldInputs) -> bool:
    age = inputs.battery_age_s
    return inputs.facts.power_kw is not None and age is not None and age <= BATTERY_MAX_AGE_S


def discharge_a(power_kw: float | None, volts_between_phases: float) -> float:
    """The amps per phase a discharge (`power_kw` < 0) covers, spread over three phases."""
    if power_kw is None or power_kw >= 0 or volts_between_phases <= 0:
        return 0.0
    return -power_kw * 1000.0 / (math.sqrt(3.0) * volts_between_phases)


def restore_value(memory: HoldMemory, control: ControlStatus | None, rated_discharge_kw: float | None) -> Any:
    """What a restore would write: the value found, or for an unset one the rating (a "never set" register cannot
    be written back), or the unset state itself where it is writable (Victron's -1)."""
    snapshot = memory.snapshot
    if control is None or control.spec.method != "number":
        return snapshot
    if snapshot is not None and snapshot in control.spec.unset_states:
        return float(snapshot)
    if memory.snapshot_unset or snapshot is None:
        return rated_discharge_kw
    try:
        return float(snapshot)
    except ValueError:
        return None


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


def restart(memory: HoldMemory, facts: BatteryFacts, now: datetime) -> tuple[HoldMemory, HoldDecision | None]:
    """After a restart a hold found in memory is restored before anything else."""
    if not memory.held:
        return memory, None
    control = facts.hold
    decision = HoldDecision(
        "would_restore",
        "restart",
        None if control is None else control.entity_id,
        restore_value(memory, control, facts.rated_discharge_kw),
        None if control is None else control.current,
        {"snapshot": memory.snapshot, "snapshot_unset": memory.snapshot_unset},
    )
    return _released(memory, now), decision


def _released(memory: HoldMemory, now: datetime) -> HoldMemory:
    return replace(
        memory,
        held=False,
        held_since=None,
        snapshot=None,
        snapshot_unset=False,
        armed_since=None,
        last_change=now,
        writes_today=memory.writes_today + 1,
    )


def _restore_reason(inputs: HoldInputs) -> str | None:
    facts = inputs.facts
    control = facts.hold
    power = facts.power_kw
    if not inputs.grid_charging:
        return "car_not_charging_from_grid"
    if facts.on_grid is False:
        return "off_grid"
    if not _fresh(inputs):
        return "battery_reading_stale"
    if power is not None and power > DISCHARGE_KW:
        return "battery_charging"
    if not inputs.measurement_ok:
        return "measurement_lost"
    if inputs.headroom_a is None or inputs.headroom_a < RESTORE_HEADROOM_A:
        return "headroom_low"
    if facts.management.state != "free":
        return f"{facts.management.state}:{facts.management.reason}"
    if control is None or not control.usable:
        return "no_hold_control" if control is None else str(control.why_not)
    return None


def _blocker(inputs: HoldInputs, memory: HoldMemory) -> tuple[HoldAction, str] | None:
    facts = inputs.facts
    control = facts.hold
    if facts.management.state != "free":
        return "left_alone", f"{facts.management.state}:{facts.management.reason}"
    if control is None:
        return "not_possible", "no_hold_control"
    if not control.usable:
        return "not_possible", str(control.why_not)
    if facts.on_grid is False:
        return "not_possible", "off_grid"
    if not inputs.measurement_ok:
        return "not_possible", "measurement_lost"
    if inputs.headroom_a is None:
        return "not_possible", "headroom_unknown"
    if inputs.headroom_a - discharge_a(facts.power_kw, inputs.volts_between_phases) < HOLD_MIN_HEADROOM_A:
        return "not_possible", "headroom_too_small"
    if memory.writes_today >= DAILY_WRITE_CAP:
        return "not_possible", "daily_write_cap"
    return None


def step(memory: HoldMemory, inputs: HoldInputs) -> tuple[HoldMemory, HoldDecision | None]:
    """One look: the new memory and the decision to log, if any."""
    now = inputs.now
    facts = inputs.facts
    control = facts.hold
    today = now.date().isoformat()
    if memory.day != today:
        memory = replace(memory, day=today, writes_today=0)
    memory = _count_energy(memory, inputs)
    if not inputs.grid_charging:
        memory = replace(memory, noted=())

    if memory.held:
        reason = _restore_reason(inputs)
        if reason is None:
            return memory, None
        decision = HoldDecision(
            "would_restore",
            reason,
            None if control is None else control.entity_id,
            restore_value(memory, control, facts.rated_discharge_kw),
            None if control is None else control.current,
            _detail(inputs) | {"snapshot": memory.snapshot, "snapshot_unset": memory.snapshot_unset},
        )
        return _released(memory, now), decision

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
    blocker = _blocker(inputs, memory)
    if blocker is not None:
        return _note(memory, blocker[0], blocker[1], inputs)
    if memory.last_change is not None and (now - memory.last_change).total_seconds() < REARM_S:
        return memory, None
    assert control is not None
    decision = HoldDecision(
        "would_write",
        "battery_discharges_into_grid_charge",
        control.entity_id,
        control.spec.value,
        control.current,
        _detail(inputs) | {"snapshot": control.current, "snapshot_unset": bool(control.unset)},
    )
    held = replace(
        memory,
        held=True,
        held_since=now,
        snapshot=control.current,
        snapshot_unset=bool(control.unset),
        armed_since=None,
        last_change=now,
        writes_today=memory.writes_today + 1,
        noted=(),
    )
    return held, decision


def _note(memory: HoldMemory, action: HoldAction, reason: str, inputs: HoldInputs) -> tuple[HoldMemory, HoldDecision | None]:
    """A non-write outcome, logged once per grid charge; it says what would have been written."""
    if reason in memory.noted:
        return memory, None
    control = inputs.facts.hold
    detail = _detail(inputs)
    if control is not None:
        detail["would_write"] = control.spec.value
    decision = HoldDecision(
        action, reason, None if control is None else control.entity_id, None,
        None if control is None else control.current, detail,
    )
    return replace(memory, noted=(*memory.noted, reason)), decision
