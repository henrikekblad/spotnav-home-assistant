"""A heat pump's facts, read only: its temperatures, degree minutes, compressor, immersion heater and priority, its
own price control (then SpotNav only watches), and the control paths it offers. Nothing is written.

Pure: the shell (`detect.py`) hands over the registry entities of the supported integrations with their states.
A pump is one anchor entity of an adapter (`adapters.py`); its other entities are the ones in the anchor's config
entry, on the anchor's own device first, else the only one in the entry while the entry holds one pump.

A reading is a value only when it is one: `unknown`, `unavailable`, a disabled entity, a wrong unit, NIBE's
"no sensor" marker (-32768, scaled -3276.8) and a figure outside the role's plausible band all read as missing, with
the reason in `missing`, never as a number (EffektGuard's lesson: a mis-scaled BT1 of -105 °C is not weather).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from .adapters import ADAPTERS, Adapter, Capability, CapabilityKind, FactRole, Match, PriceRule, adapters_for

ControlState = Literal["free", "watch_only", "unknown"]
PriceState = Literal["active", "off", "unknown", "none"]
Priority = Literal["heating", "hot_water", "cooling", "pool", "off"]

#: A state that is no reading.
_NO_READING: Final = frozenset({"", "unavailable", "unknown", "none"})
#: NIBE's "no sensor" value, raw and scaled by 10.
_NO_SENSOR: Final = frozenset({-32768.0, -3276.8})

#: Plausible bands per numeric role (EffektGuard's, `const.py` NIBE_*_PLAUSIBLE_*): outside them a reading is
#: implausible, not weather.
_BANDS: Final[dict[FactRole, tuple[float, float]]] = {
    "outdoor_temp": (-50.0, 50.0),
    "supply_temp": (0.0, 100.0),
    "return_temp": (0.0, 100.0),
    "room_temp": (0.0, 45.0),
    "hot_water_temp": (0.0, 100.0),
    "degree_minutes": (-30000.0, 30000.0),
    # Hz (NIBE, Panasonic) or rps (CTC).
    "compressor_speed": (0.0, 200.0),
    "immersion_power": (0.0, 30.0),
}
_TEMPERATURE_ROLES: Final = frozenset({"outdoor_temp", "supply_temp", "return_temp", "room_temp", "hot_water_temp"})
_TEXT_ROLES: Final = frozenset({"compressor_state", "immersion_state", "priority"})
#: Roles whose readable entities are summed (CTC's upper and lower immersion heaters).
_SUM_ROLES: Final = frozenset({"immersion_power"})

MISSING_NOT_FOUND: Final = "not_found"
MISSING_DISABLED: Final = "disabled"
MISSING_UNAVAILABLE: Final = "unavailable"
MISSING_IMPLAUSIBLE: Final = "implausible"


@dataclass(frozen=True, slots=True)
class HeatEntity:
    """One registry entity of a heat pump integration and its state now."""

    entity_id: str
    platform: str
    unique_id: str = ""
    device_id: str | None = None
    config_entry_id: str | None = None
    disabled: bool = False
    state: str | None = None
    unit: str | None = None
    #: A number's range and step, a select's options (its attributes).
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def available(self) -> bool:
        return not self.disabled and self.state is not None and self.state.strip().lower() not in _NO_READING


@dataclass(frozen=True, slots=True)
class PriceControl:
    """The pump's own price control: `active` (it runs, SpotNav only watches), `off`, `unknown` (the adapter knows
    where it is shown but nothing there reads) or `none` (the brand has none SpotNav knows of)."""

    state: PriceState
    reason: str
    sources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason, "sources": list(self.sources)}


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """A control path found on the pump. Listed only: nothing is written to it."""

    kind: CapabilityKind
    entity_id: str
    method: str
    enabled: bool
    available: bool
    current: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = ()
    volatile: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "entity_id": self.entity_id,
            "method": self.method,
            "enabled": self.enabled,
            "available": self.available,
            "current": self.current if self.available else None,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "options": list(self.options),
            "volatile": self.volatile,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class HeatPumpFacts:
    integration: str
    brand: str
    label: str
    anchor_entity_id: str
    cloud: bool = False
    device_id: str | None = None
    outdoor_temp_c: float | None = None
    supply_temp_c: float | None = None
    return_temp_c: float | None = None
    room_temp_c: float | None = None
    hot_water_temp_c: float | None = None
    degree_minutes: float | None = None
    compressor_speed: float | None = None
    compressor_speed_unit: str | None = None
    #: From the compressor's state where readable, else from its speed (> 0); `None` when neither reads.
    compressor_running: bool | None = None
    immersion_kw: float | None = None
    #: From the heater's state where readable, else from its power (> 0).
    immersion_on: bool | None = None
    priority: Priority | None = None
    price_control: PriceControl = field(default_factory=lambda: PriceControl("none", "no_price_control"))
    #: `watch_only` while the pump's own price control or another heat controller runs, `unknown` while its price
    #: control cannot be read, else `free`. A marker only: SpotNav controls no heat pump in this release.
    control: ControlState = "free"
    control_reason: str = ""
    capabilities: tuple[CapabilityStatus, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)

    @property
    def watch_only(self) -> bool:
        return self.control == "watch_only"

    def as_dict(self) -> dict[str, Any]:
        return {
            "integration": self.integration,
            "brand": self.brand,
            "label": self.label,
            "anchor_entity_id": self.anchor_entity_id,
            "cloud": self.cloud,
            "readings": {
                "outdoor_temp_c": self.outdoor_temp_c,
                "supply_temp_c": self.supply_temp_c,
                "return_temp_c": self.return_temp_c,
                "room_temp_c": self.room_temp_c,
                "hot_water_temp_c": self.hot_water_temp_c,
                "degree_minutes": self.degree_minutes,
                "compressor_speed": self.compressor_speed,
                "compressor_speed_unit": self.compressor_speed_unit,
                "compressor_running": self.compressor_running,
                "immersion_kw": self.immersion_kw,
                "immersion_on": self.immersion_on,
                "priority": self.priority,
            },
            "price_control": self.price_control.as_dict(),
            "control": {"state": self.control, "reason": self.control_reason, "watch_only": self.watch_only},
            "capabilities": [capability.as_dict() for capability in self.capabilities],
            "sources": dict(self.sources),
            "missing": dict(self.missing),
        }


def _number(state: str | None) -> float | None:
    if state is None:
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _celsius(value: float, unit: str | None) -> float | None:
    if unit in (None, "", "°C"):
        return value
    if unit == "°F":
        return (value - 32.0) * 5.0 / 9.0
    return None


def _kw(value: float, unit: str | None) -> float | None:
    factor = {None: 1.0, "": 1.0, "kW": 1.0, "W": 0.001}.get(unit)
    return None if factor is None else value * factor


def numeric_reading(role: FactRole, entity: HeatEntity) -> float | None:
    """A role's value from an available entity, `None` when it is no plausible reading of that role."""
    value = _number(entity.state)
    if value is None or value in _NO_SENSOR:
        return None
    if role in _TEMPERATURE_ROLES:
        value = _celsius(value, entity.unit)
    elif role == "immersion_power":
        value = _kw(value, entity.unit)
    if value is None:
        return None
    low, high = _BANDS[role]
    return value if low <= value <= high else None


class _Scope:
    """The entities one pump's adapter looks in: its config entry's, the anchor's device first."""

    def __init__(self, anchor: HeatEntity, entities: Sequence[HeatEntity], single: bool) -> None:
        self.anchor = anchor
        self.entities = sorted(
            (entity for entity in entities if entity.platform == anchor.platform), key=lambda item: item.entity_id
        )
        #: The entry holds this one pump only: an entity off the anchor's device is still the pump's.
        self.single = single

    def all(self, match: Match) -> list[HeatEntity]:
        found = [entity for entity in self.entities if match.matches(entity.domain, entity.unique_id)]
        own = [entity for entity in found if self.anchor.device_id is not None and entity.device_id == self.anchor.device_id]
        if own:
            return own
        return found if self.single else []

    def one(self, match: Match) -> HeatEntity | None:
        found = self.all(match)
        if len(found) == 1:
            return found[0]
        own = [entity for entity in found if entity.device_id == self.anchor.device_id]
        return own[0] if own else None


def _why_missing(candidates: Sequence[HeatEntity]) -> str:
    if not candidates:
        return MISSING_NOT_FOUND
    if all(entity.disabled for entity in candidates):
        return MISSING_DISABLED
    if not any(entity.available for entity in candidates):
        return MISSING_UNAVAILABLE
    return MISSING_IMPLAUSIBLE


def _read_role(
    role: FactRole, matches: tuple[Match, ...], scope: _Scope
) -> tuple[float | str | None, list[str], str | None]:
    """The role's value, the entities it came from, and why it is missing (`None` when read)."""
    candidates = [entity for match in matches if (entity := scope.one(match)) is not None]
    if role in _TEXT_ROLES:
        for entity in candidates:
            if entity.available:
                return entity.state.strip(), [entity.entity_id], None  # type: ignore[union-attr]
        return None, [], _why_missing(candidates)
    values: list[tuple[float, str]] = []
    for entity in candidates:
        value = numeric_reading(role, entity) if entity.available else None
        if value is None:
            continue
        values.append((value, entity.entity_id))
        if role not in _SUM_ROLES:
            break
    if not values:
        return None, [], _why_missing(candidates)
    return sum(value for value, _ in values), [source for _, source in values], None


_HOT_WATER: Final = re.compile(r"hot.?water|\bdhw\b|\btank\b|varmvatten")
_POOL: Final = re.compile(r"\bpool\b")
_COOLING: Final = re.compile(r"cool|kyla")
_OFF: Final = re.compile(r"\b(?:off|av)\b")
_HEATING: Final = re.compile(r"heat|v[äa]rme|\broom\b")


def priority_of(text: str | None) -> Priority | None:
    """What the pump serves now, from the integration's own words (NIBE "HOT WATER"/"Heating", CTC "Compressor on,
    heating"/"Till varmvatten", Panasonic's 3-way valve "Room"/"Tank"); `None` when the words say neither."""
    if not text:
        return None
    lowered = text.lower()
    if _HOT_WATER.search(lowered):
        return "hot_water"
    if _POOL.search(lowered):
        return "pool"
    if _COOLING.search(lowered):
        return "cooling"
    if _OFF.search(lowered):
        return "off"
    if _HEATING.search(lowered):
        return "heating"
    return None


_RUNNING: Final = re.compile(r"running|starting|compressor on|\btill\b|charge dhw")
_STOPPED: Final = re.compile(r"stopped|stopping|\b(?:off|av)\b|ready|redo|wait|v[äa]ntar|block|alarm|larm")


def running_of(text: str | None) -> bool | None:
    """Whether the compressor runs, from its state's words (NIBE 43427 RUNNING/STOPPED, CTC's heat pump status)."""
    if not text:
        return None
    lowered = text.lower()
    if _RUNNING.search(lowered):
        return True
    if _STOPPED.search(lowered):
        return False
    return None


def switched_on(text: str | None) -> bool | None:
    """An on/off state's sense (a binary sensor's on/off, myUplink's Active/Off/Blocked)."""
    if not text:
        return None
    lowered = text.strip().lower()
    if lowered in ("on", "active", "true", "1"):
        return True
    if lowered in ("off", "blocked", "alarm", "false", "0"):
        return False
    return None


_LEVELS_ON: Final = frozenset({"1", "2", "3", "low", "normal", "high", "låg", "hög"})
_LEVELS_OFF: Final = frozenset({"0", "unknown", "okänd"})


def _rule_state(rule: PriceRule, entity: HeatEntity) -> bool | None:
    """`True` while the rule shows the pump's own price control running, `False` when it shows none."""
    text = (entity.state or "").strip().lower()
    if rule.kind == "on":
        return switched_on(text)
    if rule.kind == "level":
        value = _number(text)
        key = str(int(value)) if value is not None and value.is_integer() else text
        if key in _LEVELS_ON:
            return True
        return False if key in _LEVELS_OFF else None
    if rule.kind == "nonzero":
        value = _number(text)
        return None if value is None else value != 0.0
    # not_normal
    return "normal" not in text


def _price_control(rules: tuple[PriceRule, ...], scope: _Scope) -> PriceControl:
    if not rules:
        return PriceControl("none", "no_price_control")
    active: list[str] = []
    read: list[str] = []
    for rule in rules:
        entity = scope.one(rule.match)
        if entity is None or not entity.available:
            continue
        verdict = _rule_state(rule, entity)
        if verdict is None:
            continue
        read.append(entity.entity_id)
        if verdict:
            active.append(rule.label)
    if active:
        return PriceControl("active", ", ".join(active), tuple(read))
    if read:
        return PriceControl("off", "off", tuple(read))
    return PriceControl("unknown", "unreadable")


def _capabilities(capabilities: tuple[Capability, ...], scope: _Scope) -> tuple[CapabilityStatus, ...]:
    found: list[CapabilityStatus] = []
    for capability in capabilities:
        for entity in scope.all(capability.match):
            found.append(
                CapabilityStatus(
                    kind=capability.kind,
                    entity_id=entity.entity_id,
                    method=entity.domain,
                    enabled=not entity.disabled,
                    available=entity.available,
                    current=entity.state,
                    minimum=entity.minimum,
                    maximum=entity.maximum,
                    step=entity.step,
                    options=entity.options,
                    volatile=capability.volatile,
                    note=capability.note,
                )
            )
    return tuple(found)


def heat_pump_facts(
    adapter: Adapter,
    anchor: HeatEntity,
    entities: Sequence[HeatEntity],
    *,
    single: bool = True,
    other_controllers: Iterable[str] = (),
) -> HeatPumpFacts:
    """What SpotNav reads of one pump: `entities` are its config entry's, `single` that the entry holds no other
    pump, `other_controllers` the heat controllers found running (EffektGuard)."""
    scope = _Scope(anchor, entities, single)
    values: dict[FactRole, float | str] = {}
    sources: dict[str, str] = {}
    missing: dict[str, str] = {}
    for role, matches in adapter.facts.items():
        value, read, why = _read_role(role, matches, scope)
        if value is None:
            missing[role] = why or MISSING_NOT_FOUND
            continue
        values[role] = value
        sources[role] = " + ".join(read)

    def number(role: FactRole) -> float | None:
        value = values.get(role)
        return value if isinstance(value, float) else None

    speed = number("compressor_speed")
    speed_unit = None
    if speed is not None:
        speed_entity = next((entity for entity in scope.entities if entity.entity_id == sources["compressor_speed"]), None)
        speed_unit = None if speed_entity is None else speed_entity.unit
    running = running_of(values.get("compressor_state"))  # type: ignore[arg-type]
    if running is None and speed is not None:
        running = speed > 0.0
    immersion_kw = number("immersion_power")
    immersion_on = switched_on(values.get("immersion_state"))  # type: ignore[arg-type]
    if immersion_on is None and immersion_kw is not None:
        immersion_on = immersion_kw > 0.0
    price = _price_control(adapter.price_control, scope)
    others = sorted(set(other_controllers))
    if others:
        control, reason = "watch_only", f"controller:{','.join(others)}"
    elif price.state == "active":
        control, reason = "watch_only", f"price_control:{price.reason}"
    elif price.state == "unknown":
        control, reason = "unknown", "price_control_unreadable"
    else:
        control, reason = "free", "own_curve"
    return HeatPumpFacts(
        integration=anchor.platform,
        brand=adapter.brand,
        label=adapter.label,
        anchor_entity_id=anchor.entity_id,
        cloud=adapter.cloud,
        device_id=anchor.device_id,
        outdoor_temp_c=number("outdoor_temp"),
        supply_temp_c=number("supply_temp"),
        return_temp_c=number("return_temp"),
        room_temp_c=number("room_temp"),
        hot_water_temp_c=number("hot_water_temp"),
        degree_minutes=number("degree_minutes"),
        compressor_speed=speed,
        compressor_speed_unit=speed_unit,
        compressor_running=running,
        immersion_kw=immersion_kw,
        immersion_on=immersion_on,
        priority=priority_of(values.get("priority")),  # type: ignore[arg-type]
        price_control=price,
        control=control,
        control_reason=reason,
        capabilities=_capabilities(adapter.capabilities, scope),
        sources=sources,
        missing=missing,
    )


def find_heat_pumps(entities: Sequence[HeatEntity], *, other_controllers: Iterable[str] = ()) -> list[HeatPumpFacts]:
    """Every heat pump among the registry entities of the supported integrations: per config entry, the first
    adapter of its integration with an anchor there, one pump per anchor (a myUplink account with two pumps has
    two). A disabled anchor still marks a pump: its readings are then `disabled`, which says what to enable."""
    others = tuple(other_controllers)
    by_entry: dict[tuple[str, str], list[HeatEntity]] = {}
    for entity in entities:
        by_entry.setdefault((entity.platform, entity.config_entry_id or ""), []).append(entity)
    pumps: list[HeatPumpFacts] = []
    for (platform, _entry), members in sorted(by_entry.items()):
        for adapter in adapters_for(platform):
            anchors = sorted(
                (entity for entity in members if adapter.anchor.matches(entity.domain, entity.unique_id)),
                key=lambda item: item.entity_id,
            )
            if not anchors:
                continue
            for anchor in anchors:
                pumps.append(
                    heat_pump_facts(adapter, anchor, members, single=len(anchors) == 1, other_controllers=others)
                )
            break
    return pumps


__all__ = [
    "ADAPTERS",
    "CapabilityStatus",
    "HeatEntity",
    "HeatPumpFacts",
    "PriceControl",
    "find_heat_pumps",
    "heat_pump_facts",
    "numeric_reading",
    "priority_of",
    "running_of",
    "switched_on",
]
