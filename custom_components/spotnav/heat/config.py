"""The site's heat settings (SpotNav 3.0 heat card, plan `plans/v3_heat_card.md`): the wanted house temperature, home
or away, the cooler night, the sensors, the control paths and the zones. Pure: read from and written to the site
entry's data under `CONF_HEAT` (`api/heat.py`), judged here.

These settings say what SpotNav aims for and which entities it would steer. Nothing here writes to a heat pump, a
thermostat or a relay; only `control_enabled` lets the heat control (`heat/control.py`) switch the relay path's relay.

An indoor sensor's offset is its room's own aim against the house target (a cooler upstairs at -1.5, a sensor that
reads half a degree too warm at +0.5): the coldest-room rule and the comfort review compare each sensor's reading less
its offset with the house's band.

Stored shape (every key optional in storage; a missing or broken one reads as its default, so an older entry or a
hand-edited one never fails a read):

    {target_c: 16..25 in 0.5 steps (21),
     mode: "home" | "away", away_until: ISO instant with offset | null (set while away),
     night: {on: bool, delta_c: 0.5..3 in 0.5 steps, start: "HH:MM", end: "HH:MM"} (off, 1, 23:00, 06:00),
     indoor_sensors: [sensor ids] (up to 8), outdoor_sensor: sensor id | null (null: the house forecast's own),
     indoor_offsets: [{entity_id: one of the indoor sensors, offset_c: -3..3 in 0.5 steps, never 0}] (none),
     control_enabled: bool (false: SpotNav's opt-in to switch the relay itself, `heat/control.py`),
     control_path: "none" | "relay" | "sg_ready" | "offset" | "setpoint", control_entity: id | null,
     control_entity_b: id | null (SG Ready's second contact), hot_water_entity: id | null,
     zones: [{entity_id: climate id, offset_c: -3..3 in 0.5 steps}] (up to 8)}
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final

TARGET_MIN_C: Final = 16.0
TARGET_MAX_C: Final = 25.0
DEFAULT_TARGET_C: Final = 21.0
#: The comfort band around the target (plan section 15: fixed ±0.5 °C first).
BAND_C: Final = 0.5
#: Away: this far below the target, never below the floor.
AWAY_DROP_C: Final = 3.0
AWAY_FLOOR_C: Final = TARGET_MIN_C
#: How far ahead an away date may be.
AWAY_MAX_DAYS: Final = 90
NIGHT_MIN_C: Final = 0.5
NIGHT_MAX_C: Final = 3.0
DEFAULT_NIGHT_C: Final = 1.0
DEFAULT_NIGHT_START: Final = "23:00"
DEFAULT_NIGHT_END: Final = "06:00"
OFFSET_MIN_C: Final = -3.0
OFFSET_MAX_C: Final = 3.0
MAX_SENSORS: Final = 8
MAX_ZONES: Final = 8
STEP_C: Final = 0.5

MODE_HOME: Final = "home"
MODE_AWAY: Final = "away"
MODES: Final = (MODE_HOME, MODE_AWAY)

PATH_NONE: Final = "none"
PATH_RELAY: Final = "relay"
PATH_SG_READY: Final = "sg_ready"
PATH_OFFSET: Final = "offset"
PATH_SETPOINT: Final = "setpoint"
PATHS: Final = (PATH_NONE, PATH_RELAY, PATH_SG_READY, PATH_OFFSET, PATH_SETPOINT)
#: The domains each control path's entity may be in (SG Ready: both contacts).
PATH_DOMAINS: Final[dict[str, tuple[str, ...]]] = {
    PATH_RELAY: ("switch", "input_boolean"),
    PATH_SG_READY: ("switch", "input_boolean"),
    PATH_OFFSET: ("number", "input_number"),
    PATH_SETPOINT: ("climate",),
}
HOT_WATER_DOMAINS: Final = ("switch", "input_boolean", "button", "input_button")

# The fields, as the write names them.
F_TARGET: Final = "target_c"
F_MODE: Final = "mode"
F_AWAY_UNTIL: Final = "away_until"
F_NIGHT: Final = "night"
F_INDOOR: Final = "indoor_sensors"
F_OUTDOOR: Final = "outdoor_sensor"
F_PATH: Final = "control_path"
F_CONTROL: Final = "control_entity"
F_CONTROL_B: Final = "control_entity_b"
F_HOT_WATER: Final = "hot_water_entity"
F_ZONES: Final = "zones"
F_INDOOR_OFFSETS: Final = "indoor_offsets"
F_CONTROL_ENABLED: Final = "control_enabled"
FIELDS: Final = (
    F_TARGET, F_MODE, F_AWAY_UNTIL, F_NIGHT, F_INDOOR, F_OUTDOOR, F_PATH, F_CONTROL, F_CONTROL_B, F_HOT_WATER, F_ZONES,
    F_INDOOR_OFFSETS, F_CONTROL_ENABLED,
)
#: What anyone who can see the card may change (the dial and home/away, a thermostat's own controls); the rest is an
#: administrator's.
USER_FIELDS: Final = frozenset({F_TARGET, F_MODE, F_AWAY_UNTIL})

# Field error codes (stable, the card words them).
ERR_INVALID: Final = "invalid_value"
ERR_RANGE: Final = "out_of_range"
ERR_STEP: Final = "not_a_step"
ERR_UNKNOWN_ENTITY: Final = "entity_not_found"
ERR_WRONG_KIND: Final = "wrong_kind"
ERR_TOO_MANY: Final = "too_many"
ERR_DUPLICATE: Final = "duplicate"
ERR_AWAY_PAST: Final = "away_in_past"
ERR_AWAY_FAR: Final = "away_too_far"
ERR_AWAY_MISSING: Final = "away_until_required"
ERR_UNKNOWN_FIELD: Final = "unknown_field"

_CLOCK: Final = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True, slots=True)
class Night:
    on: bool = False
    delta_c: float = DEFAULT_NIGHT_C
    start: str = DEFAULT_NIGHT_START
    end: str = DEFAULT_NIGHT_END

    def as_dict(self) -> dict[str, Any]:
        return {"on": self.on, "delta_c": self.delta_c, "start": self.start, "end": self.end}

    def minutes(self) -> tuple[int, int]:
        """Start and end as minutes after midnight."""
        return clock_minutes(self.start), clock_minutes(self.end)

    def covers(self, minute_of_day: int) -> bool:
        """Whether a minute of the day lies in the night (it may run over midnight)."""
        start, end = self.minutes()
        if start == end:
            return False
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end


@dataclass(frozen=True, slots=True)
class Zone:
    entity_id: str
    offset_c: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "offset_c": self.offset_c}


@dataclass(frozen=True, slots=True)
class SensorOffset:
    """An indoor sensor's room aim against the house target (°C)."""

    entity_id: str
    offset_c: float

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "offset_c": self.offset_c}


@dataclass(frozen=True, slots=True)
class HeatConfig:
    target_c: float = DEFAULT_TARGET_C
    mode: str = MODE_HOME
    away_until: str | None = None
    night: Night = field(default_factory=Night)
    indoor_sensors: tuple[str, ...] = ()
    outdoor_sensor: str | None = None
    control_path: str = PATH_NONE
    control_entity: str | None = None
    control_entity_b: str | None = None
    hot_water_entity: str | None = None
    zones: tuple[Zone, ...] = ()
    indoor_offsets: tuple[SensorOffset, ...] = ()
    control_enabled: bool = False

    def offset_of(self, entity_id: str) -> float:
        """An indoor sensor's offset (0 when it has none)."""
        return next((item.offset_c for item in self.indoor_offsets if item.entity_id == entity_id), 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            F_TARGET: self.target_c,
            F_MODE: self.mode,
            F_AWAY_UNTIL: self.away_until,
            F_NIGHT: self.night.as_dict(),
            F_INDOOR: list(self.indoor_sensors),
            F_OUTDOOR: self.outdoor_sensor,
            F_PATH: self.control_path,
            F_CONTROL: self.control_entity,
            F_CONTROL_B: self.control_entity_b,
            F_HOT_WATER: self.hot_water_entity,
            F_ZONES: [zone.as_dict() for zone in self.zones],
            F_INDOOR_OFFSETS: [item.as_dict() for item in self.indoor_offsets],
            F_CONTROL_ENABLED: self.control_enabled,
        }

    def away_end(self) -> datetime | None:
        """The return time while away (`None` at home or without a readable one)."""
        if self.mode != MODE_AWAY or self.away_until is None:
            return None
        return parse_instant(self.away_until)


def clock_minutes(text: str) -> int:
    match = _CLOCK.match(text)
    if match is None:
        raise ValueError(text)
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_instant(text: Any) -> datetime | None:
    """An ISO instant with its offset, else `None`."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _on_step(value: float) -> bool:
    return abs(value / STEP_C - round(value / STEP_C)) < 1e-9


def _step_in(value: Any, low: float, high: float) -> float | None:
    number = _number(value)
    if number is None or not low <= number <= high or not _on_step(number):
        return None
    return number


def _entity(value: Any, domains: tuple[str, ...]) -> str | None:
    if isinstance(value, str) and "." in value and value.split(".", 1)[0] in domains:
        return value
    return None


def _night(raw: Any) -> Night:
    if not isinstance(raw, Mapping):
        return Night()
    delta = _step_in(raw.get("delta_c"), NIGHT_MIN_C, NIGHT_MAX_C)
    start, end = raw.get("start"), raw.get("end")
    return Night(
        on=raw.get("on") is True,
        delta_c=DEFAULT_NIGHT_C if delta is None else delta,
        start=start if isinstance(start, str) and _CLOCK.match(start) else DEFAULT_NIGHT_START,
        end=end if isinstance(end, str) and _CLOCK.match(end) else DEFAULT_NIGHT_END,
    )


def _zones(raw: Any) -> tuple[Zone, ...]:
    if not isinstance(raw, list):
        return ()
    zones: list[Zone] = []
    for item in raw[:MAX_ZONES]:
        if not isinstance(item, Mapping):
            continue
        entity_id = _entity(item.get("entity_id"), ("climate",))
        if entity_id is None or any(zone.entity_id == entity_id for zone in zones):
            continue
        offset = _step_in(item.get("offset_c"), OFFSET_MIN_C, OFFSET_MAX_C)
        zones.append(Zone(entity_id, 0.0 if offset is None else offset))
    return tuple(zones)


def _offsets(raw: Any, sensors: tuple[str, ...]) -> tuple[SensorOffset, ...]:
    """The stored offsets of the chosen sensors, a zero, broken or duplicate one left out."""
    if not isinstance(raw, list):
        return ()
    out: dict[str, SensorOffset] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        entity_id = item.get("entity_id")
        offset = _step_in(item.get("offset_c"), OFFSET_MIN_C, OFFSET_MAX_C)
        if entity_id in sensors and entity_id not in out and offset is not None and offset != 0:
            out[entity_id] = SensorOffset(entity_id, offset)
    return tuple(out[entity_id] for entity_id in sensors if entity_id in out)


def read_config(raw: Any) -> HeatConfig:
    """The stored settings, every missing or broken key read as its default (see the module)."""
    if not isinstance(raw, Mapping):
        return HeatConfig()
    target = _step_in(raw.get(F_TARGET), TARGET_MIN_C, TARGET_MAX_C)
    mode = raw.get(F_MODE) if raw.get(F_MODE) in MODES else MODE_HOME
    away_until = raw.get(F_AWAY_UNTIL) if parse_instant(raw.get(F_AWAY_UNTIL)) is not None else None
    if mode == MODE_AWAY and away_until is None:
        mode = MODE_HOME
    sensors = raw.get(F_INDOOR)
    indoor = tuple(dict.fromkeys(s for s in sensors if _entity(s, ("sensor",)))) if isinstance(sensors, list) else ()
    path = raw.get(F_PATH) if raw.get(F_PATH) in PATHS else PATH_NONE
    domains = PATH_DOMAINS.get(path, ())
    control = _entity(raw.get(F_CONTROL), domains) if path != PATH_NONE else None
    control_b = _entity(raw.get(F_CONTROL_B), domains) if path == PATH_SG_READY else None
    return HeatConfig(
        target_c=DEFAULT_TARGET_C if target is None else target,
        mode=mode,
        away_until=away_until if mode == MODE_AWAY else None,
        night=_night(raw.get(F_NIGHT)),
        indoor_sensors=indoor[:MAX_SENSORS],
        indoor_offsets=_offsets(raw.get(F_INDOOR_OFFSETS), indoor[:MAX_SENSORS]),
        control_enabled=raw.get(F_CONTROL_ENABLED) is True,
        outdoor_sensor=_entity(raw.get(F_OUTDOOR), ("sensor", "weather")),
        control_path=path,
        control_entity=control,
        control_entity_b=control_b,
        hot_water_entity=_entity(raw.get(F_HOT_WATER), HOT_WATER_DOMAINS),
        zones=_zones(raw.get(F_ZONES)),
    )


def away_target(target_c: float) -> float:
    """The temperature held while away: `AWAY_DROP_C` below the target, never below the floor."""
    return max(AWAY_FLOOR_C, target_c - AWAY_DROP_C)


# ------------------------------------------------------------------------------------------------ the write


@dataclass(frozen=True, slots=True)
class FieldError:
    field: str
    code: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code}


#: What an entity id is, for the judge: `None` when Home Assistant knows no such entity, else whether it fits the
#: role named (`indoor`, `outdoor`, `zone`, `relay`, `sg_ready`, `offset`, `setpoint`, `hot_water`).
EntityCheck = Callable[[str, str], bool | None]


def _check_entity(errors: list[FieldError], name: str, value: Any, role: str, domains: tuple[str, ...], check: EntityCheck) -> None:
    if _entity(value, domains) is None:
        errors.append(FieldError(name, ERR_WRONG_KIND if isinstance(value, str) else ERR_INVALID))
        return
    fits = check(value, role)
    if fits is None:
        errors.append(FieldError(name, ERR_UNKNOWN_ENTITY))
    elif not fits:
        errors.append(FieldError(name, ERR_WRONG_KIND))


def apply_changes(
    current: HeatConfig, changes: Mapping[str, Any], *, now: datetime, check: EntityCheck
) -> tuple[HeatConfig, list[FieldError]]:
    """`current` with `changes` applied, and every field error (nothing applies unless there is none).

    A field set to `null` returns to its default (`away_until`, the sensors and entities: none). Entities are judged
    by `check` (the shell asks Home Assistant). Going away needs `away_until` in the future, within `AWAY_MAX_DAYS`;
    going home clears it. Changing the control path clears the entities of the old one unless they are named too."""
    errors: list[FieldError] = []
    updated = current
    for name in changes:
        if name not in FIELDS:
            errors.append(FieldError(name, ERR_UNKNOWN_FIELD))
    if errors:
        return current, errors

    if F_TARGET in changes:
        value = changes[F_TARGET]
        number = _number(value)
        if number is None:
            errors.append(FieldError(F_TARGET, ERR_INVALID))
        elif not TARGET_MIN_C <= number <= TARGET_MAX_C:
            errors.append(FieldError(F_TARGET, ERR_RANGE))
        elif not _on_step(number):
            errors.append(FieldError(F_TARGET, ERR_STEP))
        else:
            updated = replace(updated, target_c=number)

    if F_MODE in changes or F_AWAY_UNTIL in changes:
        mode = changes.get(F_MODE, current.mode)
        until_raw = changes.get(F_AWAY_UNTIL, current.away_until if mode == MODE_AWAY else None)
        if mode not in MODES:
            errors.append(FieldError(F_MODE, ERR_INVALID))
        elif mode == MODE_HOME:
            updated = replace(updated, mode=MODE_HOME, away_until=None)
        elif until_raw is None:
            errors.append(FieldError(F_AWAY_UNTIL, ERR_AWAY_MISSING))
        else:
            until = parse_instant(until_raw)
            if until is None:
                errors.append(FieldError(F_AWAY_UNTIL, ERR_INVALID))
            elif until <= now:
                errors.append(FieldError(F_AWAY_UNTIL, ERR_AWAY_PAST))
            elif until - now > timedelta(days=AWAY_MAX_DAYS):
                errors.append(FieldError(F_AWAY_UNTIL, ERR_AWAY_FAR))
            else:
                updated = replace(updated, mode=MODE_AWAY, away_until=until.isoformat())

    if F_NIGHT in changes:
        raw = changes[F_NIGHT]
        if raw is None:
            updated = replace(updated, night=Night())
        elif not isinstance(raw, Mapping) or set(raw) - {"on", "delta_c", "start", "end"}:
            errors.append(FieldError(F_NIGHT, ERR_INVALID))
        else:
            night = current.night
            before = len(errors)
            if "on" in raw and not isinstance(raw["on"], bool):
                errors.append(FieldError(F_NIGHT, ERR_INVALID))
            if "delta_c" in raw and _step_in(raw["delta_c"], NIGHT_MIN_C, NIGHT_MAX_C) is None:
                errors.append(FieldError(F_NIGHT, ERR_RANGE))
            for key in ("start", "end"):
                if key in raw and not (isinstance(raw[key], str) and _CLOCK.match(raw[key])):
                    errors.append(FieldError(F_NIGHT, ERR_INVALID))
            if len(errors) == before:
                night = Night(
                    on=raw.get("on", night.on),
                    delta_c=float(raw.get("delta_c", night.delta_c)),
                    start=raw.get("start", night.start),
                    end=raw.get("end", night.end),
                )
                if night.start == night.end:
                    errors.append(FieldError(F_NIGHT, ERR_INVALID))
                else:
                    updated = replace(updated, night=night)

    if F_INDOOR in changes:
        raw = changes[F_INDOOR]
        if raw is None:
            updated = replace(updated, indoor_sensors=())
        elif not isinstance(raw, list):
            errors.append(FieldError(F_INDOOR, ERR_INVALID))
        elif len(raw) > MAX_SENSORS:
            errors.append(FieldError(F_INDOOR, ERR_TOO_MANY))
        elif len(set(map(str, raw))) != len(raw):
            errors.append(FieldError(F_INDOOR, ERR_DUPLICATE))
        else:
            before = len(errors)
            for item in raw:
                _check_entity(errors, F_INDOOR, item, "indoor", ("sensor",), check)
            if len(errors) == before:
                updated = replace(updated, indoor_sensors=tuple(raw))

    if F_OUTDOOR in changes:
        raw = changes[F_OUTDOOR]
        if raw is None:
            updated = replace(updated, outdoor_sensor=None)
        else:
            before = len(errors)
            _check_entity(errors, F_OUTDOOR, raw, "outdoor", ("sensor", "weather"), check)
            if len(errors) == before:
                updated = replace(updated, outdoor_sensor=raw)

    if F_PATH in changes or F_CONTROL in changes or F_CONTROL_B in changes:
        path = changes.get(F_PATH, current.control_path)
        if path not in PATHS:
            errors.append(FieldError(F_PATH, ERR_INVALID))
        else:
            moved = path != current.control_path
            first = changes.get(F_CONTROL, None if moved else current.control_entity)
            second = changes.get(F_CONTROL_B, None if moved else current.control_entity_b)
            if path == PATH_NONE:
                updated = replace(updated, control_path=PATH_NONE, control_entity=None, control_entity_b=None)
            else:
                before = len(errors)
                domains = PATH_DOMAINS[path]
                if first is not None:
                    _check_entity(errors, F_CONTROL, first, path, domains, check)
                if path == PATH_SG_READY and second is not None:
                    _check_entity(errors, F_CONTROL_B, second, path, domains, check)
                    if second == first:
                        errors.append(FieldError(F_CONTROL_B, ERR_DUPLICATE))
                elif path != PATH_SG_READY:
                    second = None
                if len(errors) == before:
                    updated = replace(updated, control_path=path, control_entity=first, control_entity_b=second)

    if F_HOT_WATER in changes:
        raw = changes[F_HOT_WATER]
        if raw is None:
            updated = replace(updated, hot_water_entity=None)
        else:
            before = len(errors)
            _check_entity(errors, F_HOT_WATER, raw, "hot_water", HOT_WATER_DOMAINS, check)
            if len(errors) == before:
                updated = replace(updated, hot_water_entity=raw)

    if F_ZONES in changes:
        raw = changes[F_ZONES]
        if raw is None:
            updated = replace(updated, zones=())
        elif not isinstance(raw, list):
            errors.append(FieldError(F_ZONES, ERR_INVALID))
        elif len(raw) > MAX_ZONES:
            errors.append(FieldError(F_ZONES, ERR_TOO_MANY))
        else:
            zones: list[Zone] = []
            before = len(errors)
            for item in raw:
                if not isinstance(item, Mapping) or set(item) - {"entity_id", "offset_c"} or "entity_id" not in item:
                    errors.append(FieldError(F_ZONES, ERR_INVALID))
                    continue
                entity_id = item["entity_id"]
                _check_entity(errors, F_ZONES, entity_id, "zone", ("climate",), check)
                offset = item.get("offset_c", 0.0)
                number = _number(offset)
                if number is None:
                    errors.append(FieldError(F_ZONES, ERR_INVALID))
                elif not OFFSET_MIN_C <= number <= OFFSET_MAX_C:
                    errors.append(FieldError(F_ZONES, ERR_RANGE))
                elif not _on_step(number):
                    errors.append(FieldError(F_ZONES, ERR_STEP))
                elif any(zone.entity_id == entity_id for zone in zones):
                    errors.append(FieldError(F_ZONES, ERR_DUPLICATE))
                else:
                    zones.append(Zone(str(entity_id), number))
            if len(errors) == before:
                updated = replace(updated, zones=tuple(zones))

    if F_INDOOR_OFFSETS in changes:
        raw = changes[F_INDOOR_OFFSETS]
        if raw is None:
            updated = replace(updated, indoor_offsets=())
        elif not isinstance(raw, list):
            errors.append(FieldError(F_INDOOR_OFFSETS, ERR_INVALID))
        else:
            offsets: list[SensorOffset] = []
            before = len(errors)
            for item in raw:
                if not isinstance(item, Mapping) or set(item) != {"entity_id", "offset_c"}:
                    errors.append(FieldError(F_INDOOR_OFFSETS, ERR_INVALID))
                    continue
                number = _number(item["offset_c"])
                if item["entity_id"] not in updated.indoor_sensors:
                    errors.append(FieldError(F_INDOOR_OFFSETS, ERR_INVALID))
                elif any(offset.entity_id == item["entity_id"] for offset in offsets):
                    errors.append(FieldError(F_INDOOR_OFFSETS, ERR_DUPLICATE))
                elif number is None:
                    errors.append(FieldError(F_INDOOR_OFFSETS, ERR_INVALID))
                elif not OFFSET_MIN_C <= number <= OFFSET_MAX_C:
                    errors.append(FieldError(F_INDOOR_OFFSETS, ERR_RANGE))
                elif not _on_step(number):
                    errors.append(FieldError(F_INDOOR_OFFSETS, ERR_STEP))
                elif number != 0:
                    offsets.append(SensorOffset(str(item["entity_id"]), number))
            if len(errors) == before:
                updated = replace(updated, indoor_offsets=tuple(offsets))

    if F_CONTROL_ENABLED in changes:
        if not isinstance(changes[F_CONTROL_ENABLED], bool):
            errors.append(FieldError(F_CONTROL_ENABLED, ERR_INVALID))
        else:
            updated = replace(updated, control_enabled=changes[F_CONTROL_ENABLED])

    if errors:
        return current, _unique(errors)
    # A sensor no longer chosen takes its offset with it; the offsets follow the sensors' order.
    kept = {item.entity_id: item for item in updated.indoor_offsets if item.entity_id in updated.indoor_sensors}
    return replace(updated, indoor_offsets=tuple(kept[s] for s in updated.indoor_sensors if s in kept)), []


def _unique(errors: list[FieldError]) -> list[FieldError]:
    return list(dict.fromkeys(errors))


def comparable(name: str, value: Any) -> Any:
    """A field's value as the compare-and-set compares it (numbers as floats, lists as tuples)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return tuple(comparable(name, item) for item in value)
    if isinstance(value, Mapping):
        return tuple(sorted((key, comparable(key, item)) for key, item in value.items()))
    return value
