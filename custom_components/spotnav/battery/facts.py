"""The home battery's facts, read only: its level, power, size and ratings, who runs it and what could be written.

Pure: the shell (`runtime.py`) hands over the entities of the battery's config entry with their states. The battery
is the site's configured battery entity (`battery_aggregate_power_entity`); its power comes from the site's own
signed reading of it (positive = charging), so the sign settings and a charge/discharge pair stay in one place.

A brand without an adapter gets its level from a `battery` sensor on the battery's own device and no controls.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from ..core.parse import number_from_state
from .adapters import Adapter, ControlSpec, FactRole, Match, PreheatSpec, ReserveSource, SocPack, adapter_for

ManagementState = Literal["free", "self_managed", "unknown", "externally_controlled"]

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
    #: A number entity's step (its `step` attribute), `None` when it names none.
    step: float | None = None
    #: The state's attributes, for a control role whose device answers in them (`commands.answered`).
    attributes: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def available(self) -> bool:
        return not self.disabled and self.state is not None and self.state.lower() not in _NO_READING


@dataclass(frozen=True, slots=True)
class Management:
    """Who runs the battery: `free` (its plain self-consumption, SpotNav may act), `self_managed` (its own
    optimiser or a schedule: SpotNav leaves it alone), `externally_controlled` (another system drives it: a battery
    planner, an aggregator, or settings and behaviour SpotNav did not cause, `outside_control.py`; SpotNav only
    watches) or `unknown` (SpotNav cannot tell, so it leaves it alone as well)."""

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
    #: The value now holds no limit (a "never set" register, Victron's -1, Sigenergy 2.0.1's `unknown`); `None` when
    #: not a number control or not readable. `current_kw` is then `None`, never 0.
    unset: bool | None = None
    #: The entity's unit and step, for reading a write back (`ledger.py`).
    unit: str | None = None
    step: float | None = None

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
            "unit": self.unit,
            "step": self.step,
            "changes_mode": self.spec.changes_mode,
            "note": self.spec.note,
        }


#: The preheating switch as found: `on`, `off`, `disabled` (in Home Assistant's entity registry) or `unavailable`.
PreheatState = Literal["on", "off", "disabled", "unavailable"]


@dataclass(frozen=True, slots=True)
class PreheatFacts:
    """The battery's own heater as found (`adapters.PreheatSpec`): the switch, its state and device, and the entities
    a schedule would be written to (by role: `mode`, `advance`, `reserved_soc`, `slot_<n>_<start|end|power>`), found
    ones only, enabled or not."""

    enable_entity: str
    state: PreheatState
    device_id: str | None = None
    entities: Mapping[str, str] = field(default_factory=dict)
    #: The roles among `entities` that are disabled in Home Assistant.
    disabled: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "enable_entity": self.enable_entity,
            "state": self.state,
            "device_id": self.device_id,
            "entities": dict(self.entities),
            "disabled": list(self.disabled),
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
    #: The whole plant's maximum output (kW) where the adapter reads one: the battery charges and discharges
    #: through it (`max_charge_kw`, `max_discharge_kw`).
    plant_max_kw: float | None = None
    #: The battery's own floor (%), the highest of the reserves its adapter can read; `None` when none can be read.
    reserve_soc_pct: float | None = None
    #: `True` on the grid, `False` off it (backup), `None` when the brand does not say.
    on_grid: bool | None = None
    #: The entities of the plant's power on phases A, B and C (`adapters.Adapter.plant_phase_power`), all three found
    #: and enabled, else `None`; and the factor that makes their reading positive for output. Only where they are:
    #: their values are read live, with their age, by the one that uses them (`site/phase_peaks.py`).
    plant_phase_entities: tuple[str, str, str] | None = None
    plant_phase_sign: float = 1.0
    #: The battery's lifetime energy counters now (kWh: into it and out of it, `adapters` says where each brand counts
    #: them), `None` where the brand has none or it reads none. The round trip is learnt from them (`round_trip.py`).
    charged_total_kwh: float | None = None
    discharged_total_kwh: float | None = None
    #: The battery's round trip (0-1) learnt from its own meters (`learned.with_learned`), `None` until it is known:
    #: the plan then assumes `plan_model.DEFAULT_ROUND_TRIP_EFFICIENCY`.
    round_trip_efficiency: float | None = None
    #: The battery's average cell temperature entity (`adapters.Adapter.cell_temperature`), found and enabled, else
    #: `None`. Its day is read from the recorder's statistics by the nightly review, never live.
    cell_temperature_entity: str | None = None
    #: That entity's reading now (°C), `None` when it reads none.
    cell_temperature_c: float | None = None
    #: The battery's own heater (`adapters.Adapter.preheat`), `None` when the adapter has none or its switch is not
    #: found.
    preheat: PreheatFacts | None = None
    management: Management = field(default_factory=lambda: Management("unknown", "no_battery"))
    hold: ControlStatus | None = None
    grid_charge: ControlStatus | None = None
    sources: dict[str, str] = field(default_factory=dict)
    #: What was worked out and not read: `capacity_from_ah` (Ah x the live voltage), `rated_from_current` (the BMS'
    #: present current limits x the live voltage). The plan carries them on as its estimates.
    estimates: tuple[str, ...] = ()

    @property
    def max_charge_kw(self) -> float | None:
        """The most the battery can charge with: its rating, or the plant's maximum when that is lower."""
        return _lower(self.rated_charge_kw, self.plant_max_kw)

    @property
    def max_discharge_kw(self) -> float | None:
        """The most the battery can discharge with: its rating, or the plant's maximum when that is lower."""
        return _lower(self.rated_discharge_kw, self.plant_max_kw)

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
            "plant_max_kw": self.plant_max_kw,
            "max_charge_kw": self.max_charge_kw,
            "max_discharge_kw": self.max_discharge_kw,
            "reserve_soc_pct": self.reserve_soc_pct,
            "firmware": self.firmware,
            "on_grid": self.on_grid,
            "cell_temperature_c": self.cell_temperature_c,
            "charged_total_kwh": self.charged_total_kwh,
            "discharged_total_kwh": self.discharged_total_kwh,
            "round_trip_efficiency": self.round_trip_efficiency,
            "preheat": None if self.preheat is None else self.preheat.as_dict(),
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
            "estimates": list(self.estimates),
        }


def _lower(rating: float | None, plant_max: float | None) -> float | None:
    if rating is None or plant_max is None or plant_max <= 0:
        return rating
    return min(rating, plant_max)


def to_kw(state: str | None, unit: str | None) -> float | None:
    """A power reading in kW; `None` when it is none or its unit is not one of power's."""
    value = number_from_state(state)
    if value is None:
        return None
    factor = {"W": 0.001, "kW": 1.0, "MW": 1000.0}.get(unit or "kW")
    return None if factor is None else value * factor


def to_kwh(state: str | None, unit: str | None) -> float | None:
    """An energy reading in kWh; an amp-hour capacity (Victron, GoodWe) is no energy and reads `None`."""
    value = number_from_state(state)
    if value is None:
        return None
    factor = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}.get(unit or "kWh")
    return None if factor is None else value * factor


#: A battery's voltage that can be a reading of one (V); below or above it the entity is something else.
_VOLTAGE_RANGE: Final = (5.0, 1000.0)
#: Units a current or an amp-hour size is read in. The community Victron Modbus integration puts `A` on its Ah register.
_CURRENT_UNITS: Final = frozenset({"A", "Ah"})
EST_CAPACITY_FROM_AH: Final = "capacity_from_ah"
EST_RATED_FROM_CURRENT: Final = "rated_from_current"


def _percent(state: str | None) -> float | None:
    value = number_from_state(state)
    return value if value is not None and 0.0 <= value <= 100.0 else None


def _volts(entity: EntityFact | None) -> float | None:
    """The battery voltage an entity reads, `None` when it reads none, is not in volts or is not a battery's."""
    if entity is None or (entity.unit or "V") != "V":
        return None
    value = number_from_state(entity.state)
    return value if value is not None and _VOLTAGE_RANGE[0] <= value <= _VOLTAGE_RANGE[1] else None


def _from_amps(entity: EntityFact | None, volts: float | None) -> tuple[float | None, str | None]:
    """An amp reading (Ah or A) times the voltage in thousandths (kWh or kW), with the entity it came from."""
    if entity is None or volts is None or (entity.unit or "A") not in _CURRENT_UNITS:
        return None, None
    amps = number_from_state(entity.state)
    if amps is None or amps <= 0:
        return None, None
    return amps * volts / 1000.0, entity.entity_id


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


#: Below this level (%) a pack's size worked out from its energy left over its level is too coarse to weigh by.
PACK_WEIGHT_MIN_SOC: Final = 10.0


def packs_soc(
    packs: Sequence[SocPack], entities: Sequence[EntityFact], battery: EntityFact
) -> tuple[float | None, list[str]]:
    """The battery's level (%) from its packs' levels, with the pack entities read; `None` when no pack reads one.

    A pack that reads nothing (a site with one pack, a BMS that is offline) is left out. Where every pack read also
    states the energy it holds now and none is nearly empty, each pack's size is that energy over its level, and the
    level is the energy of all packs over their size together; otherwise the levels' plain mean."""
    levels: list[float] = []
    energies: list[float | None] = []
    read: list[str] = []
    for pack in packs:
        entity = find_entity(pack.soc, entities, battery)
        level = None if entity is None or not entity.available else _percent(entity.state)
        if level is None:
            continue
        levels.append(level)
        read.append(entity.entity_id)  # type: ignore[union-attr]
        remaining = None if pack.remaining is None else find_entity(pack.remaining, entities, battery)
        energies.append(
            None if remaining is None or not remaining.available else to_kwh(remaining.state, remaining.unit)
        )
    if not levels:
        return None, read
    if len(levels) == 1:
        return levels[0], read
    if all(energy is not None and energy > 0 for energy in energies) and min(levels) >= PACK_WEIGHT_MIN_SOC:
        stored = sum(energy for energy in energies if energy is not None)
        size = sum(energy * 100.0 / level for energy, level in zip(energies, levels) if energy is not None)
        return round(stored * 100.0 / size, 1), read
    return round(sum(levels) / len(levels), 1), read


def _plant_phase_entities(
    adapter: Adapter, entities: Sequence[EntityFact], battery: EntityFact
) -> tuple[str, str, str] | None:
    """The plant's per-phase power entities, when the adapter knows them and all three are found and enabled."""
    if adapter.plant_phase_power is None:
        return None
    found = [find_entity(match, entities, battery) for match in adapter.plant_phase_power]
    if any(entity is None or entity.disabled or entity.domain != "sensor" for entity in found):
        return None
    return (found[0].entity_id, found[1].entity_id, found[2].entity_id)  # type: ignore[union-attr]


def _cell_temperature_entity(adapter: Adapter, entities: Sequence[EntityFact], battery: EntityFact) -> str | None:
    """The first of the adapter's cell temperature sensors that is found and enabled."""
    for match in adapter.cell_temperature:
        entity = find_entity(match, entities, battery)
        if entity is not None and not entity.disabled and entity.domain == "sensor":
            return entity.entity_id
    return None


def _cell_temperature_c(entities: Sequence[EntityFact], entity_id: str | None) -> float | None:
    """The cells' temperature now (°C) from the entity found, `None` when it reads none or not in °C."""
    entity = next((item for item in entities if item.entity_id == entity_id), None)
    if entity is None or not entity.available or entity.unit not in (None, "°C"):
        return None
    return number_from_state(entity.state)


def _preheat(spec: PreheatSpec | None, entities: Sequence[EntityFact], battery: EntityFact) -> PreheatFacts | None:
    """The heater's switch and schedule entities, `None` without a spec or a switch found."""
    if spec is None:
        return None
    switch = find_entity(spec.enable, entities, battery)
    if switch is None:
        return None
    if switch.disabled:
        state: PreheatState = "disabled"
    elif switch.available and switch.state in ("on", "off"):
        state = "on" if switch.state == "on" else "off"
    else:
        state = "unavailable"
    roles: dict[str, Match] = {}
    for name, match in (("mode", spec.mode), ("advance", spec.advance), ("reserved_soc", spec.reserved_soc)):
        if match is not None:
            roles[name] = match
    for number in range(1, spec.slots + 1):
        for index, name in enumerate(("start", "end", "power")):
            match = spec.slot(number, index)
            if match is not None:
                roles[f"slot_{number}_{name}"] = match
    found: dict[str, str] = {}
    disabled: list[str] = []
    for name, match in roles.items():
        entity = find_entity(match, entities, battery)
        if entity is not None:
            found[name] = entity.entity_id
            if entity.disabled:
                disabled.append(name)
    return PreheatFacts(switch.entity_id, state, switch.device_id, found, tuple(disabled))


def _reserve(
    reserves: Sequence[ReserveSource], entities: Sequence[EntityFact], battery: EntityFact,
    found: Mapping[str, str] | None = None,
) -> tuple[float | None, list[str]]:
    """The highest floor (%) the reserves read, with the entities read; `None` when none can be read. `found`: the
    state found before SpotNav first wrote an entity, by entity id, read in place of what it reads now (the floor
    SpotNav set for a hold is its own, not the battery's)."""
    floors: list[float] = []
    read: list[str] = []
    for source in reserves:
        entity = find_entity(source.match, entities, battery)
        if entity is not None and found and entity.entity_id in found:
            value = _percent(found[entity.entity_id])
        else:
            value = None if entity is None or not entity.available else _percent(entity.state)
        if value is None:
            continue
        floors.append(100.0 - value if source.depth else value)
        read.append(entity.entity_id)  # type: ignore[union-attr]
    return (max(floors) if floors else None), read


def _management(
    adapter: Adapter | None, entities: Sequence[EntityFact], battery: EntityFact, external_planner: str | None
) -> Management:
    if external_planner is not None:
        return Management("externally_controlled", f"planner:{external_planner}")
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
    # Sigenergy 2.0.1 shows a never-set limit as `unknown`: that is a reading (no limit), never a limit of 0.
    unset_unknown = (
        spec.method == "number" and spec.unknown_is_unset and not entity.disabled
        and (entity.state or "").strip().lower() == "unknown"
    )
    if unset_unknown:
        unset, current_kw = True, None
    elif spec.method == "number" and entity.available:
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
        available=entity.available or unset_unknown,
        current=entity.state,
        current_kw=current_kw,
        unset=unset,
        unit=entity.unit,
        step=entity.step,
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


def device_firmware(sw_version: Any) -> str | None:
    """A device registry entry's `sw_version` as the firmware, stripped; `None` when it is blank or no reading, so
    the firmware stays unknown rather than a key made of nothing."""
    if not isinstance(sw_version, str):
        return None
    text = sw_version.strip()
    return None if text.lower() in _NO_READING else text


def _firmware(entity: EntityFact | None, sw_version: str | None, sources: dict[str, str]) -> str | None:
    """The firmware: the adapter's firmware entity first, else the battery device's software version (SolaX Modbus
    keeps its firmware registers internal and puts the version only on its inverter device)."""
    if entity is not None:
        # As read, unstripped: the reports kept per firmware key were stored under this string.
        return entity.state
    firmware = device_firmware(sw_version)
    if firmware is not None:
        sources["firmware"] = "device sw_version"
    return firmware


def with_outside_control(facts: BatteryFacts, active: bool, reason: str | None) -> BatteryFacts:
    """The facts with the outside-control verdict (`outside_control.Verdict`) applied: a battery another system
    drives is `externally_controlled`, whatever its own mode says (its settings are kept for the reader)."""
    if not active or facts.battery_entity_id is None or facts.management.state == "externally_controlled":
        return facts
    return replace(
        facts, management=Management("externally_controlled", reason or "outside", dict(facts.management.settings))
    )


def battery_facts(
    battery: EntityFact | None,
    entities: Sequence[EntityFact],
    *,
    manufacturer: str | None = None,
    power_kw: float | None = None,
    external_planner: str | None = None,
    found_states: Mapping[str, str] | None = None,
    sw_version: str | None = None,
) -> BatteryFacts:
    """What SpotNav knows of the site's battery. `entities` are the battery integration's entities (its config
    entry's); `manufacturer` is the battery device's; `external_planner` names a battery planner found running
    (Predbat, EMHASS), which runs the battery whatever its own mode says. `found_states`: the states SpotNav's control found
    before it first wrote, by entity id (`Executor.found_states`): a floor it set is read as the one found.
    `sw_version`: the battery device's software version in the device registry, the firmware where the adapter
    names no firmware entity or it reads nothing (`device_firmware`)."""
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
    soc_pct = None if soc_entity is None else _percent(soc_entity.state)
    if soc_pct is None and adapter.soc_packs:
        soc_pct, packs_read = packs_soc(adapter.soc_packs, entities, battery)
        if packs_read:
            # The first pack stands for the level's history (the card's recorded level); all are named.
            sources["soc"] = packs_read[0]
            sources["soc_packs"] = ", ".join(packs_read)
    capacity_kwh = None if capacity_entity is None else to_kwh(capacity_entity.state, capacity_entity.unit)
    if capacity_kwh is not None and capacity_kwh <= 0:
        capacity_kwh = None
    rated_charge_kw, rated_discharge_kw = kw("rated_charge"), kw("rated_discharge")
    estimates: list[str] = []
    volts_entity = found.get("voltage")
    volts = _volts(volts_entity)
    if volts is not None and volts_entity is not None:
        if capacity_kwh is None:
            capacity_kwh, source = _from_amps(found.get("capacity_ah"), volts)
            if source is not None:
                sources["capacity"] = f"{source} x {volts_entity.entity_id}"
                estimates.append(EST_CAPACITY_FROM_AH)
        if rated_charge_kw is None:
            rated_charge_kw, source = _from_amps(found.get("charge_current"), volts)
            if source is not None:
                sources["rated_charge"] = f"{source} x {volts_entity.entity_id}"
                estimates.append(EST_RATED_FROM_CURRENT)
        if rated_discharge_kw is None:
            rated_discharge_kw, source = _from_amps(found.get("discharge_current"), volts)
            if source is not None:
                sources["rated_discharge"] = f"{source} x {volts_entity.entity_id}"
                if EST_RATED_FROM_CURRENT not in estimates:
                    estimates.append(EST_RATED_FROM_CURRENT)
    plant_max_kw: float | None = None
    if adapter.plant_max is not None:
        plant = find_entity(adapter.plant_max, entities, battery)
        if plant is not None and plant.available:
            plant_max_kw = to_kw(plant.state, plant.unit)
            if plant_max_kw is not None and plant_max_kw > 0:
                sources["plant_max"] = plant.entity_id
            else:
                plant_max_kw = None
    plant_phases = _plant_phase_entities(adapter, entities, battery)
    if plant_phases is not None:
        sources["plant_phase_power"] = ", ".join(plant_phases)
    cell_temperature = _cell_temperature_entity(adapter, entities, battery)
    if cell_temperature is not None:
        sources["cell_temperature"] = cell_temperature
    reserve_soc_pct, reserve_read = _reserve(adapter.reserves, entities, battery, found_states)
    if reserve_read:
        sources["reserve"] = ", ".join(reserve_read)
    charged = found.get("charged_total")
    discharged = found.get("discharged_total")
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
        soc_pct=soc_pct,
        power_kw=power_kw,
        capacity_kwh=capacity_kwh,
        rated_charge_kw=rated_charge_kw,
        rated_discharge_kw=rated_discharge_kw,
        plant_max_kw=plant_max_kw,
        reserve_soc_pct=reserve_soc_pct,
        firmware=_firmware(firmware_entity, sw_version, sources),
        on_grid=on_grid,
        plant_phase_entities=plant_phases,
        plant_phase_sign=adapter.plant_phase_sign,
        charged_total_kwh=None if charged is None else to_kwh(charged.state, charged.unit),
        discharged_total_kwh=None if discharged is None else to_kwh(discharged.state, discharged.unit),
        cell_temperature_entity=cell_temperature,
        cell_temperature_c=_cell_temperature_c(entities, cell_temperature),
        preheat=_preheat(adapter.preheat, entities, battery),
        management=management,
        hold=_control(adapter.hold, entities, battery, rated_discharge_kw),
        grid_charge=_control(adapter.grid_charge, entities, battery, rated_discharge_kw),
        sources=sources,
        estimates=tuple(estimates),
    )
