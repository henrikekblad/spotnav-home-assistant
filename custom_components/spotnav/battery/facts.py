"""The home battery's facts, read only: its level, power, size and ratings, who runs it and what could be written.

Pure: the shell (`runtime.py`) hands over the entities of the battery's config entry with their states. The battery
is the site's configured battery entity (`battery_aggregate_power_entity`); its power comes from the site's own
signed reading of it (positive = charging), so the sign settings and a charge/discharge pair stay in one place.

A brand without an adapter gets its level from a `battery` sensor on the battery's own device and no controls.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from .adapters import Adapter, ControlSpec, FactRole, Match, adapter_for

ManagementState = Literal["free", "self_managed", "unknown"]

#: A state that is no reading.
_NO_READING: Final = frozenset({"", "unavailable", "unknown", "none"})


@dataclass(frozen=True, slots=True)
class EntityFact:
    """One registry entity of the battery's integration and its state now."""

    entity_id: str
    platform: str
    unique_id: str = ""
    device_id: str | None = None
    config_entry_id: str | None = None
    disabled: bool = False
    device_class: str | None = None
    state: str | None = None
    unit: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def available(self) -> bool:
        return not self.disabled and self.state is not None and self.state.lower() not in _NO_READING


@dataclass(frozen=True, slots=True)
class Management:
    """Who runs the battery: `free` (its plain self-consumption, SpotNav may act), `self_managed` (its own
    optimiser, a schedule or another system: SpotNav leaves it alone) or `unknown` (SpotNav cannot tell, so it
    leaves it alone as well)."""

    state: ManagementState
    reason: str
    settings: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ControlStatus:
    """A control the adapter knows, as found on this battery."""

    spec: ControlSpec
    entity_id: str | None = None
    found: bool = False
    enabled: bool = False
    available: bool = False
    current: str | None = None
    current_kw: float | None = None
    #: The value now holds no limit (a "never set" register, Victron's -1); `None` when not a number control.
    unset: bool | None = None

    @property
    def usable(self) -> bool:
        if self.spec.method == "service":
            return self.found
        return self.found and self.enabled and self.available

    @property
    def why_not(self) -> str | None:
        if self.usable:
            return None
        if not self.found:
            return "control_not_found"
        if not self.enabled:
            return "control_disabled"
        return "control_unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.spec.method,
            "would_write": self.spec.value,
            "entity_id": self.entity_id,
            "service": self.spec.service,
            "found": self.found,
            "enabled": self.enabled,
            "available": self.available,
            "current": self.current,
            "unset": self.unset,
            "changes_mode": self.spec.changes_mode,
            "note": self.spec.note,
        }


@dataclass(frozen=True, slots=True)
class BatteryFacts:
    battery_entity_id: str | None = None
    integration: str | None = None
    brand: str | None = None
    label: str | None = None
    soc_pct: float | None = None
    power_kw: float | None = None
    capacity_kwh: float | None = None
    rated_charge_kw: float | None = None
    rated_discharge_kw: float | None = None
    firmware: str | None = None
    #: `True` on the grid, `False` off it (backup), `None` when the brand does not say.
    on_grid: bool | None = None
    management: Management = field(default_factory=lambda: Management("unknown", "no_battery"))
    hold: ControlStatus | None = None
    grid_charge: ControlStatus | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "battery_entity_id": self.battery_entity_id,
            "integration": self.integration,
            "brand": self.brand,
            "label": self.label,
            "soc_pct": self.soc_pct,
            "power_kw": None if self.power_kw is None else round(self.power_kw, 3),
            "capacity_kwh": self.capacity_kwh,
            "rated_charge_kw": self.rated_charge_kw,
            "rated_discharge_kw": self.rated_discharge_kw,
            "firmware": self.firmware,
            "on_grid": self.on_grid,
            "management": {
                "state": self.management.state,
                "reason": self.management.reason,
                "settings": dict(self.management.settings),
            },
            "controls": {
                "hold": None if self.hold is None else self.hold.as_dict(),
                "grid_charge": None if self.grid_charge is None else self.grid_charge.as_dict(),
            },
            "sources": dict(self.sources),
        }


def _number(state: str | None) -> float | None:
    if state is None:
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def to_kw(state: str | None, unit: str | None) -> float | None:
    """A power reading in kW; `None` when it is none or its unit is not one of power's."""
    value = _number(state)
    if value is None:
        return None
    factor = {"W": 0.001, "kW": 1.0, "MW": 1000.0}.get(unit or "kW")
    return None if factor is None else value * factor


def to_kwh(state: str | None, unit: str | None) -> float | None:
    """An energy reading in kWh; an amp-hour capacity (Victron, GoodWe) is no energy and reads `None`."""
    value = _number(state)
    if value is None:
        return None
    factor = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}.get(unit or "kWh")
    return None if factor is None else value * factor


def _percent(state: str | None) -> float | None:
    value = _number(state)
    return value if value is not None and 0.0 <= value <= 100.0 else None


def find_entity(match: Match, entities: Sequence[EntityFact], battery: EntityFact) -> EntityFact | None:
    """The entity `match` names: the one on the battery's device, else the only one in its entry, else none
    (two plants in one entry and none on the battery's device cannot be told apart)."""
    found = sorted(
        (entity for entity in entities if entity.platform == battery.platform and match.matches(entity.domain, entity.unique_id)),
        key=lambda entity: entity.entity_id,
    )
    if not found:
        return None
    own = [entity for entity in found if battery.device_id is not None and entity.device_id == battery.device_id]
    if own:
        return own[0]
    return found[0] if len(found) == 1 else None


def _fact(adapter: Adapter, role: FactRole, entities: Sequence[EntityFact], battery: EntityFact) -> EntityFact | None:
    match = adapter.facts.get(role)
    if match is None:
        return None
    entity = find_entity(match, entities, battery)
    return entity if entity is not None and entity.available else None


def _management(
    adapter: Adapter | None, entities: Sequence[EntityFact], battery: EntityFact, external_planner: str | None
) -> Management:
    if external_planner is not None:
        return Management("self_managed", f"planner:{external_planner}")
    if adapter is None:
        return Management("unknown", "no_adapter")
    settings: dict[str, str | None] = {}
    managed: list[str] = []
    unknown: list[str] = []
    for rule in adapter.modes:
        entity = find_entity(rule.match, entities, battery)
        if entity is None or entity.disabled:
            if rule.required:
                unknown.append(f"{rule.label}:not_found")
                settings[rule.label] = None
            continue
        settings[rule.label] = entity.state
        if not entity.available:
            unknown.append(f"{rule.label}:unavailable")
        elif entity.state not in rule.free:
            managed.append(f"{rule.label}:{entity.state}")
    if managed:
        return Management("self_managed", ", ".join(managed), settings)
    if unknown:
        return Management("unknown", ", ".join(unknown), settings)
    return Management("free", "self_consumption", settings)


def _control(
    spec: ControlSpec | None, entities: Sequence[EntityFact], battery: EntityFact, rated_discharge_kw: float | None
) -> ControlStatus | None:
    if spec is None:
        return None
    if spec.method == "service":
        return ControlStatus(spec, found=True, enabled=True, available=True)
    assert spec.match is not None
    entity = find_entity(spec.match, entities, battery)
    if entity is None:
        return ControlStatus(spec)
    current_kw = to_kw(entity.state, entity.unit) if spec.method == "number" else None
    unset: bool | None = None
    if spec.method == "number" and entity.available:
        unset = (entity.state or "") in spec.unset_states or (
            current_kw is not None
            and (
                (spec.unset_at_or_above_kw is not None and current_kw >= spec.unset_at_or_above_kw)
                or (rated_discharge_kw is not None and current_kw >= rated_discharge_kw)
            )
        )
    return ControlStatus(
        spec,
        entity_id=entity.entity_id,
        found=True,
        enabled=not entity.disabled,
        available=entity.available,
        current=entity.state,
        current_kw=current_kw,
        unset=unset,
    )


def _unknown_brand_soc(entities: Sequence[EntityFact], battery: EntityFact) -> EntityFact | None:
    for entity in sorted(entities, key=lambda item: item.entity_id):
        if (
            entity.domain == "sensor"
            and entity.device_id is not None
            and entity.device_id == battery.device_id
            and entity.device_class == "battery"
            and entity.available
            and _percent(entity.state) is not None
        ):
            return entity
    return None


def battery_facts(
    battery: EntityFact | None,
    entities: Sequence[EntityFact],
    *,
    manufacturer: str | None = None,
    power_kw: float | None = None,
    external_planner: str | None = None,
) -> BatteryFacts:
    """What SpotNav knows of the site's battery. `entities` are the battery integration's entities (its config
    entry's); `manufacturer` is the battery device's; `external_planner` names a battery planner found running
    (Predbat, EMHASS), which runs the battery whatever its own mode says."""
    if battery is None:
        return BatteryFacts(power_kw=power_kw)
    adapter = adapter_for(battery.platform, manufacturer)
    management = _management(adapter, entities, battery, external_planner)
    if adapter is None:
        soc = _unknown_brand_soc(entities, battery)
        return BatteryFacts(
            battery_entity_id=battery.entity_id,
            integration=battery.platform,
            soc_pct=None if soc is None else _percent(soc.state),
            power_kw=power_kw,
            management=management,
            sources={"power": battery.entity_id} | ({} if soc is None else {"soc": soc.entity_id}),
        )
    found = {role: _fact(adapter, role, entities, battery) for role in adapter.facts}
    sources = {"power": battery.entity_id} | {role: entity.entity_id for role, entity in found.items() if entity is not None}

    def kw(role: FactRole) -> float | None:
        entity = found.get(role)
        return None if entity is None else to_kw(entity.state, entity.unit)

    soc_entity, capacity_entity, firmware_entity = found.get("soc"), found.get("capacity"), found.get("firmware")
    rated_discharge_kw = kw("rated_discharge")
    on_grid: bool | None = None
    if adapter.grid_status is not None:
        status = find_entity(adapter.grid_status[0], entities, battery)
        if status is not None and status.available:
            on_grid = status.state == adapter.grid_status[1]
            sources["grid_status"] = status.entity_id
    return BatteryFacts(
        battery_entity_id=battery.entity_id,
        integration=battery.platform,
        brand=adapter.brand,
        label=adapter.label,
        soc_pct=None if soc_entity is None else _percent(soc_entity.state),
        power_kw=power_kw,
        capacity_kwh=None if capacity_entity is None else to_kwh(capacity_entity.state, capacity_entity.unit),
        rated_charge_kw=kw("rated_charge"),
        rated_discharge_kw=rated_discharge_kw,
        firmware=None if firmware_entity is None else firmware_entity.state,
        on_grid=on_grid,
        management=management,
        hold=_control(adapter.hold, entities, battery, rated_discharge_kw),
        grid_charge=_control(adapter.grid_charge, entities, battery, rated_discharge_kw),
        sources=sources,
    )
