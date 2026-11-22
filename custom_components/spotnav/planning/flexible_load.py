"""Every load the plan can move, in one shape (plan `plans/v3_flexible_loads_design.md`, step 1). Pure.

A load answers five questions:

1. **Goal**: what the person wants (`GOAL_*`): energy by a time, a temperature by a time, a comfort band, a run time
   per day, or store and give back.
2. **Handles**: how SpotNav can steer it (`HANDLE_*`): a continuous power in a range, steps, on/off, a setpoint
   offset, or start once. A load may have more than one (a car's current and its phases).
3. **Hard rules**: what is never broken, whatever the price or the ceiling: a code, the key of its plain-language
   reason, and the numbers it holds.
4. **Give**: what bending it costs (`GIVE_*`), with a weight, so loads can be compared.
5. **Choices**: what the person can pick when the goal does not fit (`CHOICE_*`), each with the mode it would set.

Step 1 only describes the loads SpotNav has (the describers beside each load, `site/flexible_loads.py` collects them
for the site's diagnostics): nothing here plans, decides or writes. `Choice.sets` names the existing setting a choice
maps to, or `None` where there is none yet. Every record has stable keys in its JSON (`as_dict`, `from_dict`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from ..core.parse import aware_instant

#: A number a record holds, by name (sorted, so equal records compare and serialise alike).
type Scalar = float | int | str | bool | None
type Values = tuple[tuple[str, Scalar], ...]

KIND_CAR: Final = "car"
KIND_HOME_BATTERY: Final = "home_battery"
KIND_WATER_HEATER: Final = "water_heater"
KIND_HEAT: Final = "heat"
KIND_HEAT_ZONE: Final = "heat_zone"
KINDS: Final = (KIND_CAR, KIND_HOME_BATTERY, KIND_WATER_HEATER, KIND_HEAT, KIND_HEAT_ZONE)

GOAL_ENERGY_BY: Final = "energy_by"
GOAL_TEMPERATURE_BY: Final = "temperature_by"
GOAL_COMFORT_BAND: Final = "comfort_band"
GOAL_RUNTIME_PER_DAY: Final = "runtime_per_day"
GOAL_STORE: Final = "store"
GOALS: Final = (GOAL_ENERGY_BY, GOAL_TEMPERATURE_BY, GOAL_COMFORT_BAND, GOAL_RUNTIME_PER_DAY, GOAL_STORE)

HANDLE_CONTINUOUS: Final = "continuous"
HANDLE_STEPS: Final = "steps"
HANDLE_ON_OFF: Final = "on_off"
HANDLE_SETPOINT_OFFSET: Final = "setpoint_offset"
HANDLE_START_ONCE: Final = "start_once"
HANDLES: Final = (HANDLE_CONTINUOUS, HANDLE_STEPS, HANDLE_ON_OFF, HANDLE_SETPOINT_OFFSET, HANDLE_START_ONCE)

GIVE_KWH_SHORT: Final = "kwh_short"
GIVE_COMFORT_DEG_H: Final = "comfort_deg_h"
GIVE_MINUTES_LATE: Final = "minutes_late"
GIVE_HOURS_PAUSED: Final = "hours_paused"
GIVE_KR: Final = "kr"
GIVES: Final = (GIVE_KWH_SHORT, GIVE_COMFORT_DEG_H, GIVE_MINUTES_LATE, GIVE_HOURS_PAUSED, GIVE_KR)

CHOICE_TAKES_WHAT_FITS: Final = "takes_what_fits"
CHOICE_LATER: Final = "later"
CHOICE_LESS_COMFORT: Final = "less_comfort"
CHOICE_ACCEPT_PEAK: Final = "accept_peak"
CHOICE_PAUSE_NOW: Final = "pause_now"
CHOICES: Final = (CHOICE_TAKES_WHAT_FITS, CHOICE_LATER, CHOICE_LESS_COMFORT, CHOICE_ACCEPT_PEAK, CHOICE_PAUSE_NOW)

#: Every give weighs the same until the plan learns to compare them (the person's shed order is still to come).
DEFAULT_WEIGHT: Final = 1.0

#: What a load is doing now (`FlexibleLoad.state`): steered as planned, paused (or lowered) by a rule of SpotNav's
#: such as the shed order under the ceiling, kept from a step by its own hard rule, or only watched.
STATE_RUNNING: Final = "running"
STATE_PAUSED: Final = "paused"
STATE_BLOCKED: Final = "blocked"
STATE_WATCHED: Final = "watched"
STATES: Final = (STATE_RUNNING, STATE_PAUSED, STATE_BLOCKED, STATE_WATCHED)


def values(**items: Scalar) -> Values:
    """Named numbers as a record holds them."""
    return tuple(sorted(items.items()))


def _values_dict(items: Values) -> dict[str, Scalar]:
    return dict(items)


def _values_from(raw: Any) -> Values:
    if not isinstance(raw, Mapping):
        return ()
    return values(**{str(key): item for key, item in raw.items() if item is None or isinstance(item, (bool, int, float, str))})


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _text(raw: Any, default: str = "") -> str:
    return raw if isinstance(raw, str) else default


def _number(raw: Any) -> float | None:
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


@dataclass(frozen=True, slots=True)
class Goal:
    """What the person wants (`GOALS`); `by` the time it is wanted by, where there is one."""

    kind: str
    by: datetime | None = None
    values: Values = ()

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "by": _iso(self.by), "values": _values_dict(self.values)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Goal:
        return cls(kind=_text(raw.get("kind")), by=aware_instant(raw.get("by")), values=_values_from(raw.get("values")))


@dataclass(frozen=True, slots=True)
class Handle:
    """How SpotNav can steer the load (`HANDLES`): a range `low`..`high` in `unit`, or the `steps` it can take."""

    kind: str
    unit: str | None = None
    low: float | None = None
    high: float | None = None
    steps: tuple[float, ...] = ()
    values: Values = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "unit": self.unit,
            "low": self.low,
            "high": self.high,
            "steps": list(self.steps),
            "values": _values_dict(self.values),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Handle:
        steps = raw.get("steps")
        return cls(
            kind=_text(raw.get("kind")),
            unit=raw.get("unit") if isinstance(raw.get("unit"), str) else None,
            low=_number(raw.get("low")),
            high=_number(raw.get("high")),
            steps=tuple(float(step) for step in steps if _number(step) is not None) if isinstance(steps, list) else (),
            values=_values_from(raw.get("values")),
        )


@dataclass(frozen=True, slots=True)
class HardRule:
    """A rule never broken: its code, the key of its plain-language reason and the numbers it holds."""

    code: str
    reason_key: str
    values: Values = ()

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason_key": self.reason_key, "values": _values_dict(self.values)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> HardRule:
        return cls(code=_text(raw.get("code")), reason_key=_text(raw.get("reason_key")), values=_values_from(raw.get("values")))


def rule(code: str, **items: Scalar) -> HardRule:
    """A hard rule whose reason key is its code's (`flexible_load.rule.<code>`)."""
    return HardRule(code=code, reason_key=f"flexible_load.rule.{code}", values=values(**items))


@dataclass(frozen=True, slots=True)
class Give:
    """What bending the load costs (`GIVES`) and its weight."""

    kind: str
    weight: float = DEFAULT_WEIGHT

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "weight": self.weight}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Give:
        weight = _number(raw.get("weight"))
        return cls(kind=_text(raw.get("kind")), weight=DEFAULT_WEIGHT if weight is None else weight)


@dataclass(frozen=True, slots=True)
class Choice:
    """What the person may pick when the goal does not fit (`CHOICES`): its code, the existing setting it maps to
    (`sets`, `None` where there is none yet: descriptive only in step 1) and whether it is the one chosen now."""

    kind: str
    code: str
    sets: str | None = None
    chosen: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "code": self.code, "sets": self.sets, "chosen": self.chosen}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Choice:
        return cls(
            kind=_text(raw.get("kind")),
            code=_text(raw.get("code")),
            sets=raw.get("sets") if isinstance(raw.get("sets"), str) else None,
            chosen=raw.get("chosen") is True,
        )


@dataclass(frozen=True, slots=True)
class FlexibleLoad:
    """One load of a site in the common shape (see the module). `controlled`: whether SpotNav may steer it (the
    person's opt-in where there is one; a pause or a block of the moment is not counted), else it is only watched."""

    id: str
    kind: str
    site: str
    goal: Goal
    handles: tuple[Handle, ...] = ()
    hard_rules: tuple[HardRule, ...] = ()
    give: Give = field(default_factory=lambda: Give(GIVE_KR))
    choices: tuple[Choice, ...] = ()
    controlled: bool = False
    name: str | None = None
    #: What it is doing now (`STATES`, `None` where nothing says) and why (`ceiling`: the shed order holds it; a hard
    #: rule's code where it keeps the load from a step).
    state: str | None = None
    state_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "site": self.site,
            "name": self.name,
            "controlled": self.controlled,
            "state": self.state,
            "state_reason": self.state_reason,
            "goal": self.goal.as_dict(),
            "handles": [handle.as_dict() for handle in self.handles],
            "hard_rules": [item.as_dict() for item in self.hard_rules],
            "give": self.give.as_dict(),
            "choices": [choice.as_dict() for choice in self.choices],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> FlexibleLoad:
        def items(key: str) -> list[Mapping[str, Any]]:
            found = raw.get(key)
            return [item for item in found if isinstance(item, Mapping)] if isinstance(found, list) else []

        goal = raw.get("goal")
        give = raw.get("give")
        return cls(
            id=_text(raw.get("id")),
            kind=_text(raw.get("kind")),
            site=_text(raw.get("site")),
            name=raw.get("name") if isinstance(raw.get("name"), str) else None,
            controlled=raw.get("controlled") is True,
            state=raw.get("state") if isinstance(raw.get("state"), str) else None,
            state_reason=raw.get("state_reason") if isinstance(raw.get("state_reason"), str) else None,
            goal=Goal.from_dict(goal if isinstance(goal, Mapping) else {}),
            handles=tuple(Handle.from_dict(item) for item in items("handles")),
            hard_rules=tuple(HardRule.from_dict(item) for item in items("hard_rules")),
            give=Give.from_dict(give if isinstance(give, Mapping) else {}),
            choices=tuple(Choice.from_dict(item) for item in items("choices")),
        )
