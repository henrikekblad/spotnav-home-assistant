"""The spa's settings block inside the heat settings (`heat/config.py`, field `spa`). Pure.

Stored shape (every key optional but `entity_id`; a missing or broken one reads as its default):

    {entity_id: climate / water_heater id, family: one of `FAMILIES`,
     bath_c: 20..40 in 0.5 steps (38), idle_c: 10..40 in 0.5 steps (the spa's setpoint found at setup, else 30),
     floor_c: 5..30 in 0.5 steps (10: well above the controllers' own freeze protection),
     baths: [{weekdays: [0..6] (Monday 0), time: "HH:MM"}] (up to 4), bath_once: ISO instant with offset | null,
     heater_kw: 0.5..12 | null (entered where no power sensor is found), control: bool (false)}

`family` says which integration the spa is read through (`spa/detect.py`) and so how it would be steered; `generic`,
`control_my_spa`, `arctic_spa` and `joyonway` are watched only (`control` cannot be turned on). The baths are the times
the spa should be at `bath_c` by, like a car's departures: each weekly one on its weekdays at its local time, and one
one-off date. Between them it is held at `idle_c`, never below `floor_c`.

`control` is the opt-in for the control to come; nothing in this release writes to the spa. The write (`apply_spa`)
changes each key named and keeps the rest (a new spa starts watched, its `idle_c` the setpoint found); `null` removes the
spa. The household's own controls (the bath temperature, the idle temperature, the bath times) are also written by
`spotnav/spa` (`api/pool_spa.py`) through `apply_spa`, for any user who can see the card.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

from ..core.parse import finite_number

F_SPA: Final = "spa"

FAMILY_BALBOA: Final = "balboa"
FAMILY_SMARTTUB: Final = "smarttub"
FAMILY_MSPA: Final = "mspa"
FAMILY_GECKO: Final = "gecko"
FAMILY_BESTWAY: Final = "bestway"
FAMILY_INTEX: Final = "intex_spa"
FAMILY_TUYA_INTEX: Final = "tuya_intex"
FAMILY_GENERIC: Final = "generic"
FAMILY_CONTROL_MY_SPA: Final = "control_my_spa"
FAMILY_ARCTIC_SPA: Final = "arctic_spa"
FAMILY_JOYONWAY: Final = "joyonway"
#: The families SpotNav could steer (spec section 6.2), and those it only watches.
CONTROLLABLE: Final = (
    FAMILY_BALBOA, FAMILY_SMARTTUB, FAMILY_MSPA, FAMILY_GECKO, FAMILY_BESTWAY, FAMILY_INTEX, FAMILY_TUYA_INTEX,
)
WATCH_ONLY: Final = (FAMILY_GENERIC, FAMILY_CONTROL_MY_SPA, FAMILY_ARCTIC_SPA, FAMILY_JOYONWAY)
FAMILIES: Final = (*CONTROLLABLE, *WATCH_ONLY)
#: Families read through the maker's cloud: slow planned actions only (spec section 6).
CLOUD: Final = frozenset({FAMILY_SMARTTUB, FAMILY_MSPA, FAMILY_BESTWAY, FAMILY_CONTROL_MY_SPA})

SPA_DOMAINS: Final = ("climate", "water_heater")

BATH_MIN_C: Final = 20.0
BATH_MAX_C: Final = 40.0
DEFAULT_BATH_C: Final = 38.0
IDLE_MIN_C: Final = 10.0
IDLE_MAX_C: Final = 40.0
DEFAULT_IDLE_C: Final = 30.0
FLOOR_MIN_C: Final = 5.0
FLOOR_MAX_C: Final = 30.0
DEFAULT_FLOOR_C: Final = 10.0
HEATER_KW: Final = (0.5, 12.0)
MAX_BATHS: Final = 4
#: How far ahead a one-off bath may be booked.
BATH_ONCE_MAX_DAYS: Final = 30
STEP: Final = 0.5

KEYS: Final = frozenset(
    {"entity_id", "family", "bath_c", "idle_c", "floor_c", "baths", "bath_once", "heater_kw", "control"}
)

ERR_INVALID: Final = "invalid_value"
ERR_RANGE: Final = "out_of_range"
ERR_STEP: Final = "not_a_step"
ERR_UNKNOWN_ENTITY: Final = "entity_not_found"
ERR_WRONG_KIND: Final = "wrong_kind"
ERR_TOO_MANY: Final = "too_many"
ERR_DUPLICATE: Final = "duplicate"
ERR_UNKNOWN_FIELD: Final = "unknown_field"
ERR_BATH_PAST: Final = "bath_in_past"
ERR_BATH_FAR: Final = "bath_too_far"
#: `control` on a family SpotNav only watches.
ERR_WATCH_ONLY: Final = "watch_only"

_CLOCK: Final = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: The judge's entity check (`heat/config.EntityCheck`), role `spa`.
EntityCheck = Callable[[str, str], bool | None]


@dataclass(frozen=True, slots=True)
class Bath:
    """A weekly bath: on `weekdays` (Monday 0) at the local `time`."""

    weekdays: tuple[int, ...]
    time: str

    def as_dict(self) -> dict[str, Any]:
        return {"weekdays": list(self.weekdays), "time": self.time}

    def minute_of_day(self) -> int:
        match = _CLOCK.match(self.time)
        assert match is not None
        return int(match.group(1)) * 60 + int(match.group(2))


@dataclass(frozen=True, slots=True)
class SpaConfig:
    entity_id: str
    family: str = FAMILY_GENERIC
    bath_c: float = DEFAULT_BATH_C
    idle_c: float = DEFAULT_IDLE_C
    floor_c: float = DEFAULT_FLOOR_C
    baths: tuple[Bath, ...] = ()
    bath_once: str | None = None
    heater_kw: float | None = None
    control: bool = False

    @property
    def controllable(self) -> bool:
        return self.family in CONTROLLABLE

    @property
    def hold_c(self) -> float:
        """What the spa is held at between baths: its idle temperature, never below its floor."""
        return max(self.idle_c, self.floor_c)

    def bath_once_at(self) -> datetime | None:
        return parse_instant(self.bath_once)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "family": self.family,
            "bath_c": self.bath_c,
            "idle_c": self.idle_c,
            "floor_c": self.floor_c,
            "baths": [bath.as_dict() for bath in self.baths],
            "bath_once": self.bath_once,
            "heater_kw": self.heater_kw,
            "control": self.control,
        }


def parse_instant(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _on_step(value: float) -> bool:
    return abs(value / STEP - round(value / STEP)) < 1e-9


def _entity(value: Any, domains: tuple[str, ...]) -> str | None:
    if isinstance(value, str) and "." in value and value.split(".", 1)[0] in domains:
        return value
    return None


def _number_in(value: Any, low: float, high: float, *, step: bool = True) -> float | None:
    number = finite_number(value)
    if number is None or not low <= number <= high or (step and not _on_step(number)):
        return None
    return number


def _bath(raw: Any) -> Bath | None:
    """A weekly bath, `None` when it is not one."""
    if not isinstance(raw, Mapping) or set(raw) != {"weekdays", "time"}:
        return None
    days, time = raw["weekdays"], raw["time"]
    if not isinstance(time, str) or not _CLOCK.match(time) or not isinstance(days, list) or not days:
        return None
    if any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in days):
        return None
    if len(set(days)) != len(days):
        return None
    return Bath(tuple(sorted(days)), time)


def read_spa(raw: Any) -> SpaConfig | None:
    """The stored block, every missing or broken key read as its default (see the module)."""
    if not isinstance(raw, Mapping):
        return None
    entity_id = _entity(raw.get("entity_id"), SPA_DOMAINS)
    if entity_id is None:
        return None
    family = raw.get("family") if raw.get("family") in FAMILIES else FAMILY_GENERIC
    bath = _number_in(raw.get("bath_c"), BATH_MIN_C, BATH_MAX_C)
    idle = _number_in(raw.get("idle_c"), IDLE_MIN_C, IDLE_MAX_C)
    floor = _number_in(raw.get("floor_c"), FLOOR_MIN_C, FLOOR_MAX_C)
    stored = raw.get("baths")
    baths = tuple(b for b in (_bath(item) for item in stored[:MAX_BATHS]) if b is not None) if isinstance(stored, list) else ()
    return SpaConfig(
        entity_id=entity_id,
        family=family,
        bath_c=DEFAULT_BATH_C if bath is None else bath,
        idle_c=DEFAULT_IDLE_C if idle is None else idle,
        floor_c=DEFAULT_FLOOR_C if floor is None else floor,
        baths=baths,
        bath_once=raw.get("bath_once") if parse_instant(raw.get("bath_once")) is not None else None,
        heater_kw=_number_in(raw.get("heater_kw"), *HEATER_KW, step=False),
        control=raw.get("control") is True and family in CONTROLLABLE,
    )


def idle_from_setpoint(setpoint_c: float | None) -> float:
    """The idle temperature a new spa starts with: the setpoint found, to the half degree within range, else 30."""
    if setpoint_c is None or setpoint_c != setpoint_c:
        return DEFAULT_IDLE_C
    return min(IDLE_MAX_C, max(IDLE_MIN_C, round(setpoint_c / STEP) * STEP))


def judge_baths(raw: Any) -> tuple[tuple[Bath, ...] | None, str | None]:
    """The weekly baths written, or the error code."""
    if raw is None:
        return (), None
    if not isinstance(raw, list):
        return None, ERR_INVALID
    if len(raw) > MAX_BATHS:
        return None, ERR_TOO_MANY
    baths = [_bath(item) for item in raw]
    if any(bath is None for bath in baths):
        return None, ERR_INVALID
    found = tuple(bath for bath in baths if bath is not None)
    if len(set(found)) != len(found):
        return None, ERR_DUPLICATE
    return found, None


def judge_bath_once(raw: Any, now: datetime) -> tuple[str | None, str | None]:
    """The one-off bath written (ISO, `None` to clear), or the error code."""
    if raw is None:
        return None, None
    moment = parse_instant(raw)
    if moment is None:
        return None, ERR_INVALID
    if moment <= now:
        return None, ERR_BATH_PAST
    if moment - now > timedelta(days=BATH_ONCE_MAX_DAYS):
        return None, ERR_BATH_FAR
    return moment.isoformat(), None


def _judge_number(errors: list[tuple[str, str]], name: str, value: Any, low: float, high: float, *, step: bool, nullable: bool) -> tuple[bool, float | None]:
    if value is None and nullable:
        return True, None
    number = finite_number(value)
    if number is None:
        errors.append((name, ERR_INVALID))
    elif not low <= number <= high:
        errors.append((name, ERR_RANGE))
    elif step and not _on_step(number):
        errors.append((name, ERR_STEP))
    else:
        return True, number
    return False, None


def apply_spa(
    current: SpaConfig | None,
    raw: Any,
    check: EntityCheck,
    *,
    now: datetime,
    setpoint_of: Callable[[str], float | None] = lambda _entity_id: None,
) -> tuple[SpaConfig | None, list[tuple[str, str]]]:
    """`current` with the block `raw` applied, and every `(field, code)` error (nothing applies unless there is none).
    Field names are `spa.<key>`. A new spa (or another entity) starts watched, its `idle_c` from `setpoint_of`."""
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return current, [(F_SPA, ERR_INVALID)]
    errors: list[tuple[str, str]] = [(f"{F_SPA}.{key}", ERR_UNKNOWN_FIELD) for key in raw if key not in KEYS]
    if errors:
        return current, errors
    entity_id = raw.get("entity_id", None if current is None else current.entity_id)
    if entity_id is None:
        return current, [(f"{F_SPA}.entity_id", ERR_INVALID)]
    name = f"{F_SPA}.entity_id"
    if _entity(entity_id, SPA_DOMAINS) is None:
        return current, [(name, ERR_WRONG_KIND if isinstance(entity_id, str) else ERR_INVALID)]
    fits = check(entity_id, "spa")
    if fits is None:
        return current, [(name, ERR_UNKNOWN_ENTITY)]
    if not fits:
        return current, [(name, ERR_WRONG_KIND)]
    moved = current is None or current.entity_id != entity_id
    updated = (
        SpaConfig(entity_id=str(entity_id), idle_c=idle_from_setpoint(setpoint_of(str(entity_id))))
        if current is None
        else replace(current, entity_id=str(entity_id))
    )
    if moved:
        updated = replace(updated, control=False)
        if current is not None and "idle_c" not in raw:
            updated = replace(updated, idle_c=idle_from_setpoint(setpoint_of(str(entity_id))))

    if "family" in raw:
        if raw["family"] not in FAMILIES:
            errors.append((f"{F_SPA}.family", ERR_INVALID))
        else:
            updated = replace(updated, family=raw["family"])

    for key, low, high, step, nullable in (
        ("bath_c", BATH_MIN_C, BATH_MAX_C, True, False),
        ("idle_c", IDLE_MIN_C, IDLE_MAX_C, True, False),
        ("floor_c", FLOOR_MIN_C, FLOOR_MAX_C, True, False),
        ("heater_kw", *HEATER_KW, False, True),
    ):
        if key in raw:
            ok, number = _judge_number(errors, f"{F_SPA}.{key}", raw[key], low, high, step=step, nullable=nullable)
            if ok:
                updated = replace(updated, **{key: number})

    if "baths" in raw:
        baths, code = judge_baths(raw["baths"])
        if code is not None:
            errors.append((f"{F_SPA}.baths", code))
        elif baths is not None:
            updated = replace(updated, baths=baths)

    if "bath_once" in raw:
        once, code = judge_bath_once(raw["bath_once"], now)
        if code is not None:
            errors.append((f"{F_SPA}.bath_once", code))
        else:
            updated = replace(updated, bath_once=once)

    if "control" in raw:
        if not isinstance(raw["control"], bool):
            errors.append((f"{F_SPA}.control", ERR_INVALID))
        else:
            updated = replace(updated, control=raw["control"])

    if not errors and updated.control and not updated.controllable:
        errors.append((f"{F_SPA}.control", ERR_WATCH_ONLY))
    if errors:
        return current, list(dict.fromkeys(errors))
    return updated, []
