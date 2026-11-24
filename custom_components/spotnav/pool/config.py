"""The pool's settings block inside the heat settings (`heat/config.py`, field `pool`). Pure.

Stored shape (every key optional; a missing or broken one reads as its default, a block without a heat pump and without
a circulation pump reads as no pool):

    {heat_pump: {entity_id, shape: "climate" | "switch_number", switch_id: id | null, number_id: id | null} | null,
     circulation_id: switch / input_boolean id | null,
     water_sensor: sensor id | null          (the standalone sensor, second after the heat pump's own reading),
     target_c: 18..40 in 0.5 steps (27),     season_open: bool (true),
     filter_hours: 1..24 in 0.5 steps | null (null: from the water temperature, `pool/plan.py`),
     heat_pump_kw: 0.3..15 | null, circulation_kw: 0.05..3 | null (entered where no meter is found),
     control_heat_pump: bool (false), control_circulation: bool (false)}

`shape` "climate" is any `climate` entity the detection made the pool heat pump (`entity_id` the climate); "switch_number"
is core `tuya`'s pool heat pump without a climate (category `znrb`): `switch_id` turns it on and off, `number_id` is its
`temp_set`, and `entity_id` names the device (its switch, or a sensor of it).

The opt-ins (`control_heat_pump`, `control_circulation`) are kept here for the controls to come; nothing in this release
writes to the pump or the circulation. `control_heat_pump` needs `circulation_id` (the interlock: the heat pump only ever
runs while the circulation does) and the heat pump; `control_circulation` needs `circulation_id`.

The write (`apply_pool`) changes each key named and keeps the rest; `null` for the block removes the pool. Field errors
name the key inside the block (`pool.target_c`). The household's own controls (the dial, the season) are also written by
`spotnav/pool` (`api/pool_spa.py`) through `apply_pool`, for any user who can see the card.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

from ..core.parse import finite_number

F_POOL: Final = "pool"

SHAPE_CLIMATE: Final = "climate"
SHAPE_SWITCH_NUMBER: Final = "switch_number"
SHAPES: Final = (SHAPE_CLIMATE, SHAPE_SWITCH_NUMBER)

TARGET_MIN_C: Final = 18.0
TARGET_MAX_C: Final = 40.0
DEFAULT_TARGET_C: Final = 27.0
FILTER_MIN_H: Final = 1.0
FILTER_MAX_H: Final = 24.0
HEAT_PUMP_KW: Final = (0.3, 15.0)
CIRCULATION_KW: Final = (0.05, 3.0)
STEP: Final = 0.5

CIRCULATION_DOMAINS: Final = ("switch", "input_boolean")
SENSOR_DOMAINS: Final = ("sensor",)
HEAT_PUMP_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    SHAPE_CLIMATE: ("climate",),
    SHAPE_SWITCH_NUMBER: ("switch", "sensor", "number"),
}
SWITCH_DOMAINS: Final = ("switch",)
NUMBER_DOMAINS: Final = ("number",)

KEYS: Final = frozenset(
    {
        "heat_pump", "circulation_id", "water_sensor", "target_c", "season_open", "filter_hours", "heat_pump_kw",
        "circulation_kw", "control_heat_pump", "control_circulation",
    }
)
HEAT_PUMP_KEYS: Final = frozenset({"entity_id", "shape", "switch_id", "number_id"})

# Error codes beside `heat/config.py`'s (stable, the card words them).
ERR_INVALID: Final = "invalid_value"
ERR_RANGE: Final = "out_of_range"
ERR_STEP: Final = "not_a_step"
ERR_UNKNOWN_ENTITY: Final = "entity_not_found"
ERR_WRONG_KIND: Final = "wrong_kind"
ERR_DUPLICATE: Final = "duplicate"
ERR_UNKNOWN_FIELD: Final = "unknown_field"
#: An opt-in whose entities are not chosen (the circulation for either opt-in, the heat pump for its own).
ERR_ENTITY_REQUIRED: Final = "entity_required"
#: A pool with neither a heat pump nor a circulation pump.
ERR_EMPTY: Final = "nothing_chosen"

#: What an entity id is, for the judge (`heat/config.EntityCheck`): `None` when unknown, else whether it fits the role
#: (`pool_heat_pump`, `pool_switch`, `pool_number`, `pool_circulation`, `pool_water`).
EntityCheck = Callable[[str, str], bool | None]


@dataclass(frozen=True, slots=True)
class PoolHeatPump:
    entity_id: str
    shape: str = SHAPE_CLIMATE
    switch_id: str | None = None
    number_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "shape": self.shape, "switch_id": self.switch_id, "number_id": self.number_id}


@dataclass(frozen=True, slots=True)
class PoolConfig:
    heat_pump: PoolHeatPump | None = None
    circulation_id: str | None = None
    water_sensor: str | None = None
    target_c: float = DEFAULT_TARGET_C
    season_open: bool = True
    filter_hours: float | None = None
    heat_pump_kw: float | None = None
    circulation_kw: float | None = None
    control_heat_pump: bool = False
    control_circulation: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "heat_pump": None if self.heat_pump is None else self.heat_pump.as_dict(),
            "circulation_id": self.circulation_id,
            "water_sensor": self.water_sensor,
            "target_c": self.target_c,
            "season_open": self.season_open,
            "filter_hours": self.filter_hours,
            "heat_pump_kw": self.heat_pump_kw,
            "circulation_kw": self.circulation_kw,
            "control_heat_pump": self.control_heat_pump,
            "control_circulation": self.control_circulation,
        }

    def entities(self) -> tuple[str, ...]:
        """Every entity the block names."""
        found: list[str] = []
        if self.heat_pump is not None:
            found += [self.heat_pump.entity_id, *(e for e in (self.heat_pump.switch_id, self.heat_pump.number_id) if e)]
        found += [e for e in (self.circulation_id, self.water_sensor) if e]
        return tuple(dict.fromkeys(found))


def _on_step(value: float) -> bool:
    return abs(value / STEP - round(value / STEP)) < 1e-9


def _entity(value: Any, domains: tuple[str, ...]) -> str | None:
    if isinstance(value, str) and "." in value and value.split(".", 1)[0] in domains:
        return value
    return None


def _number_in(value: Any, low: float, high: float, *, step: bool) -> float | None:
    number = finite_number(value)
    if number is None or not low <= number <= high or (step and not _on_step(number)):
        return None
    return number


def _heat_pump(raw: Any) -> PoolHeatPump | None:
    if not isinstance(raw, Mapping):
        return None
    shape = raw.get("shape") if raw.get("shape") in SHAPES else SHAPE_CLIMATE
    entity_id = _entity(raw.get("entity_id"), HEAT_PUMP_DOMAINS[shape])
    if entity_id is None:
        return None
    if shape == SHAPE_SWITCH_NUMBER:
        switch_id = _entity(raw.get("switch_id"), SWITCH_DOMAINS)
        number_id = _entity(raw.get("number_id"), NUMBER_DOMAINS)
        if switch_id is None:
            return None
        return PoolHeatPump(entity_id, shape, switch_id, number_id)
    return PoolHeatPump(entity_id, shape)


def read_pool(raw: Any) -> PoolConfig | None:
    """The stored block, every missing or broken key read as its default (see the module)."""
    if not isinstance(raw, Mapping):
        return None
    heat_pump = _heat_pump(raw.get("heat_pump"))
    circulation = _entity(raw.get("circulation_id"), CIRCULATION_DOMAINS)
    if heat_pump is None and circulation is None:
        return None
    target = _number_in(raw.get("target_c"), TARGET_MIN_C, TARGET_MAX_C, step=True)
    filter_hours = _number_in(raw.get("filter_hours"), FILTER_MIN_H, FILTER_MAX_H, step=True)
    return PoolConfig(
        heat_pump=heat_pump,
        circulation_id=circulation,
        water_sensor=_entity(raw.get("water_sensor"), SENSOR_DOMAINS),
        target_c=DEFAULT_TARGET_C if target is None else target,
        season_open=raw.get("season_open") is not False,
        filter_hours=filter_hours,
        heat_pump_kw=_number_in(raw.get("heat_pump_kw"), *HEAT_PUMP_KW, step=False),
        circulation_kw=_number_in(raw.get("circulation_kw"), *CIRCULATION_KW, step=False),
        control_heat_pump=raw.get("control_heat_pump") is True and heat_pump is not None and circulation is not None,
        control_circulation=raw.get("control_circulation") is True and circulation is not None,
    )


def _check(errors: list[tuple[str, str]], name: str, value: Any, role: str, domains: tuple[str, ...], check: EntityCheck) -> bool:
    """Whether `value` is an entity of `domains` Home Assistant knows in that role; else the error is added."""
    if _entity(value, domains) is None:
        errors.append((name, ERR_WRONG_KIND if isinstance(value, str) else ERR_INVALID))
        return False
    fits = check(value, role)
    if fits is None:
        errors.append((name, ERR_UNKNOWN_ENTITY))
        return False
    if not fits:
        errors.append((name, ERR_WRONG_KIND))
        return False
    return True


def _judge_number(
    errors: list[tuple[str, str]], name: str, value: Any, low: float, high: float, *, step: bool, nullable: bool
) -> tuple[bool, float | None]:
    """`(ok, value)` for a number field; `null` where `nullable`."""
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


def apply_pool(current: PoolConfig | None, raw: Any, check: EntityCheck) -> tuple[PoolConfig | None, list[tuple[str, str]]]:
    """`current` with the block `raw` applied, and every `(field, code)` error (nothing applies unless there is none).
    Field names are `pool.<key>`."""
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return current, [(F_POOL, ERR_INVALID)]
    errors: list[tuple[str, str]] = [(f"{F_POOL}.{key}", ERR_UNKNOWN_FIELD) for key in raw if key not in KEYS]
    if errors:
        return current, errors
    updated = current or PoolConfig()

    if "heat_pump" in raw:
        value = raw["heat_pump"]
        name = f"{F_POOL}.heat_pump"
        if value is None:
            updated = replace(updated, heat_pump=None, control_heat_pump=False)
        elif not isinstance(value, Mapping) or set(value) - HEAT_PUMP_KEYS or "entity_id" not in value:
            errors.append((name, ERR_INVALID))
        else:
            shape = value.get("shape", SHAPE_CLIMATE)
            if shape not in SHAPES:
                errors.append((name, ERR_INVALID))
            else:
                ok = _check(errors, name, value["entity_id"], "pool_heat_pump", HEAT_PUMP_DOMAINS[shape], check)
                switch_id = value.get("switch_id")
                number_id = value.get("number_id")
                if shape == SHAPE_SWITCH_NUMBER:
                    if switch_id is None:
                        errors.append((name, ERR_ENTITY_REQUIRED))
                        ok = False
                    else:
                        ok = _check(errors, name, switch_id, "pool_switch", SWITCH_DOMAINS, check) and ok
                    if number_id is not None:
                        ok = _check(errors, name, number_id, "pool_number", NUMBER_DOMAINS, check) and ok
                else:
                    switch_id = number_id = None
                if ok:
                    pump = PoolHeatPump(str(value["entity_id"]), shape, switch_id, number_id)
                    moved = updated.heat_pump is None or updated.heat_pump.entity_id != pump.entity_id
                    # A new heat pump starts watched only.
                    updated = replace(updated, heat_pump=pump, control_heat_pump=False if moved else updated.control_heat_pump)

    if "circulation_id" in raw:
        value = raw["circulation_id"]
        name = f"{F_POOL}.circulation_id"
        if value is None:
            updated = replace(updated, circulation_id=None, control_circulation=False, control_heat_pump=False)
        elif _check(errors, name, value, "pool_circulation", CIRCULATION_DOMAINS, check):
            moved = value != updated.circulation_id
            updated = replace(updated, circulation_id=str(value), control_circulation=False if moved else updated.control_circulation)

    if "water_sensor" in raw:
        value = raw["water_sensor"]
        if value is None:
            updated = replace(updated, water_sensor=None)
        elif _check(errors, f"{F_POOL}.water_sensor", value, "pool_water", SENSOR_DOMAINS, check):
            updated = replace(updated, water_sensor=str(value))

    for key, low, high, step, nullable in (
        ("target_c", TARGET_MIN_C, TARGET_MAX_C, True, False),
        ("filter_hours", FILTER_MIN_H, FILTER_MAX_H, True, True),
        ("heat_pump_kw", *HEAT_PUMP_KW, False, True),
        ("circulation_kw", *CIRCULATION_KW, False, True),
    ):
        if key in raw:
            ok, number = _judge_number(errors, f"{F_POOL}.{key}", raw[key], low, high, step=step, nullable=nullable)
            if ok:
                updated = replace(updated, **{key: number})

    for key in ("season_open", "control_heat_pump", "control_circulation"):
        if key in raw:
            if not isinstance(raw[key], bool):
                errors.append((f"{F_POOL}.{key}", ERR_INVALID))
            else:
                updated = replace(updated, **{key: raw[key]})

    if not errors:
        if updated.heat_pump is None and updated.circulation_id is None:
            errors.append((F_POOL, ERR_EMPTY))
        if updated.control_circulation and updated.circulation_id is None:
            errors.append((f"{F_POOL}.control_circulation", ERR_ENTITY_REQUIRED))
        if updated.control_heat_pump and (updated.circulation_id is None or updated.heat_pump is None):
            # The interlock: the heat pump never runs without the circulation.
            errors.append((f"{F_POOL}.control_heat_pump", ERR_ENTITY_REQUIRED))
        if updated.heat_pump is not None and updated.circulation_id is not None and updated.circulation_id in (
            updated.heat_pump.entity_id, updated.heat_pump.switch_id
        ):
            errors.append((f"{F_POOL}.circulation_id", ERR_DUPLICATE))
    if errors:
        return current, list(dict.fromkeys(errors))
    return updated, []
