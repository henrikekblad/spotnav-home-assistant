"""Automatic, read-only detection of vehicle-like devices.

Readings are computed from live Home Assistant state on every dashboard
capture; an empty list is a normal result. Human decisions (dismissal, a
confirmed state-of-charge or charge-limit entity) live in
`vehicles/discovery_decisions.py`, which this module only reads. Detection uses
entity domain, device class, unit and declared range; between look-alikes it ranks by the
generic words of each entity's key (`entity_keys`: `soh`, `target`, `v2l`, `capacity`, ...), never
by brand. A wrong guess is worse than a miss, so:

* A percent battery sensor alone is not enough; a distance-class range sensor
  must be on the same device.
* An unreadable state of charge leaves the device out.
* Several percent battery sensors are ranked by key: health, target, limit, 12 V, arrival,
  predicted ... keys never are the state of charge, `usable` is only a soft demotion, a
  state-of-charge key is preferred. Only when exactly one survives is it picked; otherwise the
  device is ambiguous (`discover_ambiguous_vehicles`).
* Charge limit: a `number` (or a `select` whose options are percents in 50-100) with a
  limit key; `*_ac` before `*_dc`; v2l/discharge/min/profile/solar/share never; a limit the
  integration enforces itself is not the car's; a read-only target sensor is a ceiling only.
* Charge limit and capacity are optional extras. A capacity must be static (cumulative meters
  are never one) and never a remaining/available/added energy; capacity keys come first.
* A dismissed device is skipped, whatever else was decided.
* A confirmed entity replaces only the "exactly one candidate" rule; range and
  readability checks still apply, with no fallback to the other candidates. A
  confirmation naming a deleted entity is stale and ignored (never deleted).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..runtime import domain_data
from .discovery_decisions import DECISION_DOMAIN_VEHICLE, DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT
from .entity_keys import EntityKey, entity_key


CapacitySource = Literal["detected", "unknown"]

_BATTERY_DEVICE_CLASS = "battery"
_RANGE_DEVICE_CLASS = "distance"
# `energy_storage` is an exact match; `energy` is the weaker fallback.
_ENERGY_STORAGE_DEVICE_CLASS = "energy_storage"
_ENERGY_DEVICE_CLASS = "energy"

_PERCENT_UNITS = ("%",)
# Energy units a capacity may use, with the factor to kWh. `MWh` is absent: that
# size is a stationary battery.
_ENERGY_UNITS: dict[str, float] = {
    "kWh": 1.0,
    "Wh": 0.001,
    "MJ": 1.0 / 3.6,
    "kJ": 1.0 / 3600.0,
}

# Capacity window (kWh): below is too small for a traction battery, above is
# a lifetime meter.
_CAPACITY_KWH_WINDOW = (5.0, 250.0)

# `state_class` values meaning an accumulating meter, not a static property.
_CUMULATIVE_STATE_CLASSES = ("total", "total_increasing")

# A charge limit's declared range must top out at least this high (and lie in
# 0-100); a 0-20% number is some other setting.
_MIN_CHARGE_LIMIT_CEILING = 50.0

# --- Key vocabulary (generic words, never brands) ---------------------------------------------

# A percent battery sensor whose key has one of these is not the traction battery's charge level.
_SOC_NEGATIVE_PHRASES = (
    "soh", "state of health", "health", "target", "limit", "min", "max", "arrival", "departure",
    "predicted", "extrapolated", "precise", "trip", "segment", "route", "threshold", "fuel",
    "12v", "12 v", "aux", "auxiliary", "service battery", "starter", "car battery", "low voltage",
    "phone", "key", "fob",
)
# Only a soft demotion: the sole charge-level sensor of some integrations says `usable`.
_SOC_SOFT_PHRASES = ("usable",)
# Preferred keys, strongest first. Longest phrase of the first tier that matches decides.
_SOC_POSITIVE_TIERS: tuple[tuple[str, ...], ...] = (
    ("state of charge displayed",),
    ("battery management header",),
    (
        "battery level", "ev battery percentage", "ev battery level", "state of charge", "soc",
        "battery percentage", "battery charge level", "elec percent", "charge percent",
        "remaining battery percent", "battery soc",
    ),
)

# Charge limit keys (squashed substrings, so `elVehTargetCharge` matches).
_LIMIT_POSITIVE_FRAGMENTS = (
    "chargelimit", "charginglimit", "targetsoc", "targetstateofcharge", "stateofchargetarget",
    "targetcharge", "chargetarget", "chargingtarget", "targetbatterychargelevel", "maxsoc",
    "maxstateofcharge",
)
_LIMIT_DEMOTED_PHRASES = (
    "v2l", "discharge", "min", "minimum", "profile", "solar", "share", "arrival", "departure",
)
_LIMIT_DEMOTED_FRAGMENTS = ("batterycare",)
_LIMIT_GLOBAL_FRAGMENT = "global"
# An integration-side soft limit (stored locally, empty until set) has this declared range.
_INTEGRATION_LIMIT_RANGE = (15.0, 95.0)
# Options of a percent `select` charge limit.
_SELECT_LIMIT_WINDOW = (50.0, 100.0)

# Capacity keys: never a remaining/added/delta energy; capacity-like keys come first.
_CAPACITY_EXCLUDED_FRAGMENTS = (
    "remain", "available", "residual", "energyleft", "added", "delta", "mileage",
    "consumption", "tofull", "fullycharged",
)
# `kwhr` ends a remaining-energy key (`kwhr`), but also a capacity one (`capacity_kwhr`): excluded unless the
# key says capacity.
_CAPACITY_EXCLUDED_UNLESS_NAMED = ("kwhr",)
_CAPACITY_EXCLUDED_PHRASES = ("soe",)
_CAPACITY_PREFERRED_FRAGMENTS = ("capacity", "size", "maxenergy")


@dataclass(frozen=True, slots=True)
class VehicleCandidate:
    """One detected vehicle-like device.

    `id` is the stable device id (`name` is display only). `entity_ids` is what the
    reading depends on; it is never published and only feeds `refresh_vehicle`.
    """

    id: str
    name: str
    soc_percent: float
    target_soc_percent_max: float | None
    battery_capacity_kwh: float | None
    capacity_source: CapacitySource
    entity_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AmbiguousVehicleCandidate:
    """A vehicle-like device with several percent battery sensors.

    `candidate_entity_ids` (sorted) is what a person chooses between; `id` keys the
    confirmation in `vehicles/discovery_decisions.py`.
    """

    id: str
    name: str
    candidate_entity_ids: list[str]


@dataclass(frozen=True, slots=True)
class AmbiguousChargeLimitCandidate:
    """A vehicle-like device with several charge-limit-shaped `number` entities.

    Independent of `AmbiguousVehicleCandidate`; each is resolved separately.
    """

    id: str
    name: str
    candidate_entity_ids: list[str]


def discover_vehicles(hass: HomeAssistant) -> list[VehicleCandidate]:
    """Every vehicle-like device currently visible, sorted by device id.

    Never raises for an incomplete device. Dismissal is checked before any signal
    is read.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    decisions = domain_data(hass).decision_store
    vehicles: list[VehicleCandidate] = []
    # Sorted by id so the result does not depend on registry insertion order.
    for device in sorted(device_registry.devices, key=lambda entry: entry.id):
        if decisions is not None and decisions.is_dismissed(DECISION_DOMAIN_VEHICLE, device.id):
            continue
        entities = _device_entities(hass, entity_registry, device.id)
        soc_entity_id = _soc_entity_id(
            entities, _confirmed_soc_entity_id(entity_registry, decisions, device.id)
        )
        if soc_entity_id is None:
            continue
        soc_percent = valid_soc_percent(hass.states.get(soc_entity_id))
        range_entity_ids = _range_signal_entity_ids(entities)
        if soc_percent is None or not range_entity_ids:
            continue
        capacity_kwh = _battery_capacity_kwh(entities)
        vehicles.append(
            VehicleCandidate(
                id=device.id,
                name=_device_name(device),
                soc_percent=soc_percent,
                target_soc_percent_max=_target_soc_percent_max(
                    entities,
                    _confirmed_charge_limit_entity_id(
                        entity_registry, decisions, device.id
                    ),
                ),
                battery_capacity_kwh=capacity_kwh,
                capacity_source="detected" if capacity_kwh is not None else "unknown",
                entity_ids=_reading_entity_ids(
                    soc_entity_id,
                    range_entity_ids,
                    _charge_limit_readings(entities),
                    _capacity_readings(entities),
                ),
            )
        )
    return vehicles


def vehicle_reading_entity_ids(hass: HomeAssistant, vehicle_id: object) -> list[str] | None:
    """The entities `vehicle_id`'s current reading depends on, or `None`.

    `None` is one answer for an unknown id, a non-vehicle and a dismissed device,
    so the `refresh_vehicle` action cannot probe which devices exist.
    """
    if not isinstance(vehicle_id, str) or not vehicle_id:
        return None
    for vehicle in discover_vehicles(hass):
        if vehicle.id == vehicle_id:
            return list(vehicle.entity_ids)
    return None


def vehicle_soc_entity_id(hass: HomeAssistant, vehicle_id: object) -> str | None:
    """The entity `vehicle_id`'s state of charge is read from, or `None`.

    Unlike `discover_vehicles` this needs no readable value (shape only), so a
    sleeping car still resolves to the entity target enforcement should watch. The
    same one-answer `None` rule as `vehicle_reading_entity_ids`.
    """
    if not isinstance(vehicle_id, str) or not vehicle_id:
        return None
    device = dr.async_get(hass).async_get(vehicle_id)
    if device is None:
        return None
    decisions = domain_data(hass).decision_store
    if decisions is not None and decisions.is_dismissed(DECISION_DOMAIN_VEHICLE, device.id):
        return None
    entity_registry = er.async_get(hass)
    entities = _device_entities(hass, entity_registry, device.id)
    if not _range_signal_entity_ids(entities):
        return None
    return _soc_entity_id(
        entities, _confirmed_soc_entity_id(entity_registry, decisions, device.id)
    )


def vehicle_charge_limit_entity_id(hass: HomeAssistant, vehicle_id: object) -> str | None:
    """The `number` entity `vehicle_id`'s charge limit is written to, or `None`.

    Uses the same `_resolve_charge_limit` rule as the reported ceiling, so the app
    is never offered a limit a write cannot reach. "Highest value wins" is not used:
    which of several limits is meant is the person's knowledge. `None` is one
    answer for every refusal, as in `vehicle_reading_entity_ids`.
    """
    entities = _writable_vehicle_entities(hass, vehicle_id)
    if entities is None:
        return None
    resolution = _resolve_charge_limit(
        entities,
        _confirmed_charge_limit_entity_id(
            er.async_get(hass), domain_data(hass).decision_store, str(vehicle_id)
        ),
    )
    # A read-only target sensor is a ceiling only: there is nothing to write to.
    return resolution.entity_id if resolution.writable else None


def valid_charge_limit_entity_id(
    hass: HomeAssistant, vehicle_id: object, entity_id: object
) -> bool:
    """Whether `entity_id` is one of `vehicle_id`'s live charge-limit candidates.

    The gate a confirmation passes before being recorded, so it can only name
    something the resolver would accept.
    """
    if not isinstance(entity_id, str) or not entity_id:
        return False
    entities = _writable_vehicle_entities(hass, vehicle_id)
    if entities is None:
        return False
    return any(candidate == entity_id for candidate, _ in _charge_limit_readings(entities))


@dataclass(frozen=True, slots=True)
class VehicleSocChoice:
    """A vehicle whose state-of-charge sensor a person may choose or change.

    Also lists devices that read fine (`automatic`) or are asleep. `source` is
    `confirmed`, `automatic`, or `None` when ambiguous.
    """

    id: str
    name: str
    candidate_entity_ids: list[str]
    selected_entity_id: str | None
    source: Literal["confirmed", "automatic"] | None


def soc_choices(hass: HomeAssistant) -> list[VehicleSocChoice]:
    """Vehicle-shaped, not dismissed devices with a state-of-charge candidate, by name then id."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    decisions = domain_data(hass).decision_store
    choices: list[VehicleSocChoice] = []
    for device in sorted(device_registry.devices, key=lambda entry: entry.id):
        if decisions is not None and decisions.is_dismissed(DECISION_DOMAIN_VEHICLE, device.id):
            continue
        entities = _device_entities(hass, entity_registry, device.id)
        candidates = _percent_battery_sensor_entity_ids(entities)
        if not candidates or not _has_range_signal(entities):
            continue
        confirmed = _confirmed_soc_entity_id(entity_registry, decisions, device.id)
        selected = _soc_entity_id(entities, confirmed)
        if selected is None and not _rank_soc_candidates(entities)[1]:
            # Every sensor is a health, target, 12 V ... reading: nothing to choose between.
            continue
        choices.append(
            VehicleSocChoice(
                id=device.id,
                name=_device_name(device),
                candidate_entity_ids=candidates,
                selected_entity_id=selected,
                source=None if selected is None else ("confirmed" if confirmed else "automatic"),
            )
        )
    # By name, then id: device ids are random, so id order alone would shuffle the list.
    return sorted(choices, key=lambda choice: (choice.name.casefold(), choice.id))


def resolve_target_vehicle(
    hass: HomeAssistant, stored_vehicle_id: object
) -> tuple[str | None, list[VehicleSocChoice]]:
    """The vehicle a charger's target is for, and the candidates it was chosen from.

    The stored id if still listed with a resolved sensor, else the only candidate,
    else `None`.
    """
    candidates = [c for c in soc_choices(hass) if c.selected_entity_id is not None]
    if isinstance(stored_vehicle_id, str) and any(c.id == stored_vehicle_id for c in candidates):
        return stored_vehicle_id, candidates
    if len(candidates) == 1:
        return candidates[0].id, candidates
    return None, candidates


def valid_soc_choice(hass: HomeAssistant, device_id: object, entity_id: object) -> bool:
    """Whether `entity_id` may be recorded as the state of charge of `device_id`.

    Wider than `valid_soc_entity_id`: it also accepts devices that already read.
    """
    if not isinstance(device_id, str) or not isinstance(entity_id, str) or not entity_id:
        return False
    return any(
        choice.id == device_id and entity_id in choice.candidate_entity_ids
        for choice in soc_choices(hass)
    )


def valid_soc_entity_id(hass: HomeAssistant, device_id: object, entity_id: object) -> bool:
    """Whether `entity_id` is one of this device's ambiguous state-of-charge candidates.

    Mirror of `valid_charge_limit_entity_id`; an unambiguous device has no candidates.
    """
    if not isinstance(entity_id, str) or not entity_id:
        return False
    return entity_id in _ambiguous_soc_candidate_entity_ids(hass, device_id)


def _writable_vehicle_entities(
    hass: HomeAssistant, vehicle_id: object
) -> list[tuple[er.RegistryEntry, State]] | None:
    """The entities of a non-dismissed, vehicle-shaped device, or `None`.

    The gate shared by charge-limit resolution, confirmation checks and the ceiling.
    """
    if not isinstance(vehicle_id, str) or not vehicle_id:
        return None
    device = dr.async_get(hass).async_get(vehicle_id)
    if device is None:
        return None
    decisions = domain_data(hass).decision_store
    if decisions is not None and decisions.is_dismissed(DECISION_DOMAIN_VEHICLE, device.id):
        return None
    entities = _device_entities(hass, er.async_get(hass), device.id)
    if not _range_signal_entity_ids(entities):
        return None
    return entities


def discover_ambiguous_vehicles(hass: HomeAssistant) -> list[AmbiguousVehicleCandidate]:
    """Vehicle-like devices needing a human to pick their state of charge.

    Range signal plus several percent battery sensors, minus confirmed (non-stale)
    and dismissed devices. Sorted by device id.
    """
    ambiguous: list[AmbiguousVehicleCandidate] = []
    for device in sorted(dr.async_get(hass).devices, key=lambda entry: entry.id):
        candidate_entity_ids = _ambiguous_soc_candidate_entity_ids(hass, device.id)
        if not candidate_entity_ids:
            continue
        ambiguous.append(
            AmbiguousVehicleCandidate(
                id=device.id,
                name=_device_name(device),
                candidate_entity_ids=candidate_entity_ids,
            )
        )
    return ambiguous


def discover_ambiguous_charge_limits(
    hass: HomeAssistant,
) -> list[AmbiguousChargeLimitCandidate]:
    """Vehicle-like devices needing a human to pick their charge limit.

    Range signal plus several *live* charge-limit readings, minus confirmed
    (non-stale) and dismissed devices, so an asleep second entity is not a choice.
    """
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    decisions = domain_data(hass).decision_store
    ambiguous: list[AmbiguousChargeLimitCandidate] = []
    for device in sorted(device_registry.devices, key=lambda entry: entry.id):
        if decisions is not None and decisions.is_dismissed(DECISION_DOMAIN_VEHICLE, device.id):
            continue
        if _confirmed_charge_limit_entity_id(entity_registry, decisions, device.id) is not None:
            continue
        entities = _device_entities(hass, entity_registry, device.id)
        if not _has_range_signal(entities):
            continue
        candidate_entity_ids = _resolve_charge_limit(entities, None).ambiguous
        if len(candidate_entity_ids) < 2:
            continue
        ambiguous.append(
            AmbiguousChargeLimitCandidate(
                id=device.id,
                name=_device_name(device),
                candidate_entity_ids=candidate_entity_ids,
            )
        )
    return ambiguous


def _device_name(device: Any) -> str:
    """The user's name for a device, else the integration's, else the id."""
    return device.name_by_user or device.name or device.id


def _confirmed_soc_entity_id(
    entity_registry: er.EntityRegistry, decisions: Any, device_id: str
) -> str | None:
    """The usable, human-confirmed state-of-charge entity, or `None`.

    Usable while the entity is registered; a stale one counts as none but the stored
    decision is untouched. State is not checked, so a sleeping car is not re-asked.
    """
    if decisions is None:
        return None
    payload = decisions.confirmed_payload(DECISION_DOMAIN_VEHICLE, device_id)
    if not payload:
        return None
    entity_id = payload.get("soc_entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        return None
    if entity_registry.async_get(entity_id) is None:
        return None
    return entity_id


def _confirmed_charge_limit_entity_id(
    entity_registry: er.EntityRegistry, decisions: Any, device_id: str
) -> str | None:
    """The usable, human-confirmed charge-limit entity, or `None`.

    Mirror of `_confirmed_soc_entity_id` with its own decision domain.
    """
    if decisions is None:
        return None
    payload = decisions.confirmed_payload(DECISION_DOMAIN_VEHICLE_CHARGE_LIMIT, device_id)
    if not payload:
        return None
    entity_id = payload.get("charge_limit_entity_id")
    if not isinstance(entity_id, str) or not entity_id:
        return None
    if entity_registry.async_get(entity_id) is None:
        return None
    return entity_id


def _device_entities(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, device_id: str
) -> list[tuple[er.RegistryEntry, State]]:
    """Entities on this device that have a live state, paired with it, by entity id.

    An `unavailable`/`unknown` value is kept: its attributes still describe the entity.
    """
    return [
        (entry, state)
        for entry in sorted(
            er.async_entries_for_device(entity_registry, device_id),
            key=lambda entry: entry.entity_id,
        )
        if (state := hass.states.get(entry.entity_id)) is not None
    ]


def _percent_battery_sensor_entity_ids(
    entities: list[tuple[er.RegistryEntry, State]],
) -> list[str]:
    """Percent-shaped `device_class: battery` sensors on the device, sorted: the state-of-charge candidates."""
    return sorted(
        entry.entity_id
        for entry, state in entities
        if entry.domain == "sensor"
        and state.attributes.get("device_class") == _BATTERY_DEVICE_CLASS
        and _is_percent_unit(state.attributes.get("unit_of_measurement"))
    )


def _ambiguous_soc_candidate_entity_ids(hass: HomeAssistant, device_id: object) -> list[str]:
    """The state-of-charge entities a human still has to choose between, or `[]`.

    Shared by `discover_ambiguous_vehicles` and the confirmation check.
    """
    if not isinstance(device_id, str) or not device_id:
        return []
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return []
    decisions = domain_data(hass).decision_store
    if decisions is not None and decisions.is_dismissed(DECISION_DOMAIN_VEHICLE, device.id):
        return []
    entity_registry = er.async_get(hass)
    if _confirmed_soc_entity_id(entity_registry, decisions, device.id) is not None:
        return []
    entities = _device_entities(hass, entity_registry, device.id)
    if not _has_range_signal(entities):
        return []
    return _rank_soc_candidates(entities)[1]


def _soc_entity_id(
    entities: list[tuple[er.RegistryEntry, State]],
    confirmed_entity_id: str | None,
) -> str | None:
    """Which entity the state of charge is read from, or `None`.

    A confirmed entity is used directly; if it is unreadable there is no fallback.
    Otherwise `_rank_soc_candidates` must leave exactly one.
    """
    if confirmed_entity_id:
        return confirmed_entity_id
    return _rank_soc_candidates(entities)[0]


def _rank_soc_candidates(
    entities: list[tuple[er.RegistryEntry, State]],
) -> tuple[str | None, list[str]]:
    """(the chosen state-of-charge entity or `None`, the survivors a person would choose between).

    1. Keys that are not the charge level (health, target, limit, 12 V, arrival, predicted ...)
       are dropped, even when alone: a wrong guess is worse than a miss.
    2. Several left: drop the softly demoted (`usable`) when others remain.
    3. Several left: the strongest state-of-charge key wins if it is unique.
    The survivors list is empty unless more than one remain undecided.
    """
    keyed = [
        (entry.entity_id, entity_key(entry))
        for entry, state in entities
        if _is_percent_battery_sensor(entry, state)
    ]
    survivors = [item for item in keyed if not item[1].has_any(_SOC_NEGATIVE_PHRASES)]
    if len(survivors) > 1:
        firm = [item for item in survivors if not item[1].has_any(_SOC_SOFT_PHRASES)]
        survivors = firm or survivors
    if len(survivors) > 1:
        ranked = [(_soc_key_tier(key), entity_id) for entity_id, key in survivors]
        best = max(tier for tier, _ in ranked)
        top = [entity_id for tier, entity_id in ranked if tier == best]
        if best > 0 and len(top) == 1:
            return top[0], []
        return None, sorted(entity_id for entity_id, _ in survivors)
    if survivors:
        return survivors[0][0], []
    return None, []


def _soc_key_tier(key: EntityKey) -> int:
    """How strongly a key reads as the state of charge: 0 none, higher is stronger."""
    tiers = len(_SOC_POSITIVE_TIERS)
    for index, phrases in enumerate(_SOC_POSITIVE_TIERS):
        if key.has_any(phrases):
            return tiers - index
    return 0


def _is_percent_battery_sensor(entry: er.RegistryEntry, state: State) -> bool:
    return (
        entry.domain == "sensor"
        and state.attributes.get("device_class") == _BATTERY_DEVICE_CLASS
        and _is_percent_unit(state.attributes.get("unit_of_measurement"))
    )


def valid_soc_percent(state: State | None) -> float | None:
    """`state` as a state of charge, or `None` for anything unusable.

    Public so `execution/target_stop.py` accepts exactly what is reported.
    """
    if state is None:
        return None
    value = _as_float(state.state)
    if value is None or not 0.0 <= value <= 100.0:
        return None
    return value


def _range_signal_entity_ids(
    entities: list[tuple[er.RegistryEntry, State]],
) -> list[str]:
    """Entities that count as the range signal: a `sensor` with `device_class: distance`.

    Rejected on purpose: charging or plug binary sensors (home batteries, laptops),
    percent numbers, device trackers and name patterns. The cost is a miss, never a
    wrong report.
    """
    return [
        entry.entity_id
        for entry, state in entities
        if entry.domain == "sensor"
        and state.attributes.get("device_class") == _RANGE_DEVICE_CLASS
    ]


def _has_range_signal(entities: list[tuple[er.RegistryEntry, State]]) -> bool:
    """Whether this device reports a remaining range: the vehicle signal."""
    return bool(_range_signal_entity_ids(entities))


@dataclass(frozen=True, slots=True)
class _LimitCandidate:
    """One charge-limit-shaped entity: a `number`, a percent `select`, or a read-only `sensor`."""

    entity_id: str
    value: float
    domain: str
    key: EntityKey

    @property
    def writable(self) -> bool:
        return self.domain != "sensor"


@dataclass(frozen=True, slots=True)
class _LimitResolution:
    """Which limit a write or a ceiling uses. `ambiguous` lists the tied candidates (2+) or `[]`."""

    entity_id: str | None = None
    value: float | None = None
    writable: bool = False
    ambiguous: list[str] = field(default_factory=list)


def _charge_limit_candidates(
    entities: list[tuple[er.RegistryEntry, State]],
) -> list[_LimitCandidate]:
    """Every live charge-limit-shaped entity on the device that is the car's, by entity id.

    A limit the integration enforces itself (a soft limit it stores locally, with a companion
    `switch` of the same key, or its declared 15-95 range) is not the car's and is left out.
    """
    switch_keys = {
        entry.translation_key
        for entry, _ in entities
        if entry.domain == "switch" and entry.translation_key
    }
    found: list[_LimitCandidate] = []
    for entry, state in entities:
        if entry.domain == "number":
            value = _charge_limit_percent(state)
            if value is None or _integration_enforced(entry, state, switch_keys):
                continue
            found.append(_LimitCandidate(entry.entity_id, value, "number", entity_key(entry)))
        elif entry.domain == "select":
            value = _select_limit_percent(state)
            key = entity_key(entry)
            if value is not None and key.contains_any(_LIMIT_POSITIVE_FRAGMENTS):
                found.append(_LimitCandidate(entry.entity_id, value, "select", key))
        elif entry.domain == "sensor":
            value = _target_sensor_percent(state)
            key = entity_key(entry)
            if value is not None and key.contains_any(_LIMIT_POSITIVE_FRAGMENTS):
                found.append(_LimitCandidate(entry.entity_id, value, "sensor", key))
    return found


def _integration_enforced(
    entry: er.RegistryEntry, state: State, switch_keys: set[str]
) -> bool:
    if entry.translation_key and entry.translation_key in switch_keys:
        return True
    minimum = _as_float(state.attributes.get("min"))
    maximum = _as_float(state.attributes.get("max"))
    return (minimum, maximum) == _INTEGRATION_LIMIT_RANGE


def _charge_limit_readings(
    entities: list[tuple[er.RegistryEntry, State]],
) -> list[tuple[str, float]]:
    """Every writable charge-limit candidate on this device, as (entity id, percent), by entity id."""
    return [
        (candidate.entity_id, candidate.value)
        for candidate in _charge_limit_candidates(entities)
        if candidate.writable
    ]


def _limit_demoted(key: EntityKey) -> bool:
    return key.has_any(_LIMIT_DEMOTED_PHRASES) or key.contains_any(_LIMIT_DEMOTED_FRAGMENTS)


def _is_dc(key: EntityKey) -> bool:
    return key.has("dc")


def _pick_limit(candidates: list[_LimitCandidate]) -> list[_LimitCandidate]:
    """Narrow same-kind candidates by key: the survivors (one means a decision)."""
    left = [c for c in candidates if not _limit_demoted(c.key)]
    if len(left) > 1:
        keyed = [c for c in left if c.key.contains_any(_LIMIT_POSITIVE_FRAGMENTS)]
        left = keyed or left
    if len(left) > 1:
        left = [c for c in left if not _is_dc(c.key)] or left
    if len(left) > 1:
        left = [c for c in left if c.key.contains(_LIMIT_GLOBAL_FRAGMENT)] or left
    return left


def _resolve_charge_limit(
    entities: list[tuple[er.RegistryEntry, State]], confirmed_entity_id: str | None
) -> _LimitResolution:
    """Which charge limit a write goes to, and which one the ceiling is read from.

    A confirmed entity wins (only the "exactly one" count is bypassed). Otherwise the writable
    candidates are narrowed by key (`_pick_limit`); several left are ambiguous, never resolved by
    comparing values. With no writable one, a single read-only target sensor is the ceiling.
    """
    candidates = _charge_limit_candidates(entities)
    if confirmed_entity_id:
        # A confirmed entity that is no longer limit-shaped yields no value; the ceiling is
        # unknown rather than borrowed from another entity.
        value = next((c.value for c in candidates if c.entity_id == confirmed_entity_id), None)
        return _LimitResolution(confirmed_entity_id, value, True)
    writable = _pick_limit([c for c in candidates if c.writable])
    if len(writable) == 1:
        return _LimitResolution(writable[0].entity_id, writable[0].value, True)
    if len(writable) > 1:
        return _LimitResolution(ambiguous=sorted(c.entity_id for c in writable))
    sensors = [
        c for c in candidates if not c.writable and not _limit_demoted(c.key)
    ]
    if len(sensors) == 1:
        return _LimitResolution(sensors[0].entity_id, sensors[0].value, False)
    return _LimitResolution()


def _target_soc_percent_max(
    entities: list[tuple[er.RegistryEntry, State]], confirmed_entity_id: str | None
) -> float | None:
    """The live value of the one charge limit a write would reach (or the one target sensor), or `None`.

    `None` also covers an asleep car and no limit-shaped entity.
    """
    return _resolve_charge_limit(entities, confirmed_entity_id).value


def _charge_limit_percent(state: State) -> float | None:
    """One `number` entity's value, if shaped like a charge limit.

    Needs a percent unit, `min`/`max` inside 0-100 with max at least
    `_MIN_CHARGE_LIMIT_CEILING`, and a live value in range.
    """
    if not _is_percent_unit(state.attributes.get("unit_of_measurement")):
        return None
    minimum = _as_float(state.attributes.get("min"))
    maximum = _as_float(state.attributes.get("max"))
    if minimum is None or maximum is None or minimum < 0.0 or maximum > 100.0:
        return None
    if maximum <= minimum or maximum < _MIN_CHARGE_LIMIT_CEILING:
        return None
    value = _as_float(state.state)
    if value is None or not minimum <= value <= maximum:
        return None
    # Zero means "unknown" (asleep car, not yet polled), not a limit; reporting
    # a 0 ceiling would leave a planner no target at all.
    if value <= 0.0:
        return None
    return value


def select_option_percent(option: Any) -> float | None:
    """A `select` option such as "80" or "80 %" as a percent, or `None`."""
    if not isinstance(option, str):
        return None
    return _as_float(option.strip().removesuffix("%").strip())


def _select_limit_percent(state: State) -> float | None:
    """A `select`'s current option, if every option is a percent in 50-100 (a limit picker)."""
    options = state.attributes.get("options")
    if not isinstance(options, (list, tuple)) or len(options) < 2:
        return None
    percents = [select_option_percent(option) for option in options]
    low, high = _SELECT_LIMIT_WINDOW
    if any(p is None or not low <= p <= high for p in percents):
        return None
    return select_option_percent(state.state)


def _target_sensor_percent(state: State) -> float | None:
    """A read-only target sensor's percent (1-100), or `None`."""
    if not _is_percent_unit(state.attributes.get("unit_of_measurement")):
        return None
    value = _as_float(state.state)
    if value is None or not 0.0 < value <= 100.0:
        return None
    return value


def _capacity_readings(
    entities: list[tuple[er.RegistryEntry, State]],
) -> list[tuple[str, float]]:
    """Capacity-shaped readings as (entity id, kWh).

    Remaining/available/added/delta energies are never capacities (they track the state of
    charge and would be picked as the largest). Entities whose key says capacity/size/max energy
    come first, from any tier; otherwise the first tier with any candidate.
    """
    keyed: list[tuple[int, str, float]] = []
    for tier, predicate in enumerate(
        (_energy_storage_capacity, _number_capacity, _energy_sensor_capacity)
    ):
        for entry, state in entities:
            key = entity_key(entry)
            if key.contains_any(_CAPACITY_EXCLUDED_FRAGMENTS) or key.has_any(
                _CAPACITY_EXCLUDED_PHRASES
            ):
                continue
            named = key.contains_any(_CAPACITY_PREFERRED_FRAGMENTS)
            if not named and key.contains_any(_CAPACITY_EXCLUDED_UNLESS_NAMED):
                continue
            kwh = predicate(entry, state, named)
            if kwh is not None:
                keyed.append((tier, entry.entity_id, kwh))
    named_ids = {
        entry.entity_id
        for entry, _ in entities
        if entity_key(entry).contains_any(_CAPACITY_PREFERRED_FRAGMENTS)
    }
    for pool in (
        [item for item in keyed if item[1] in named_ids],
        keyed,
    ):
        if pool:
            best_tier = min(tier for tier, _, _ in pool)
            return [(entity_id, kwh) for tier, entity_id, kwh in pool if tier == best_tier]
    return []


def _battery_capacity_kwh(entities: list[tuple[er.RegistryEntry, State]]) -> float | None:
    """This device's battery capacity in kWh, or `None`. The largest reading wins.

    Tiers: `energy_storage` from a `number` or `sensor`; then an energy-unit `number`;
    then an `energy` sensor, the weakest. Capacity-named entities beat unnamed ones. kJ and MJ
    are converted. Out-of-window values are ignored, not clamped.
    """
    readings = _capacity_readings(entities)
    return max(value for _, value in readings) if readings else None


def _reading_entity_ids(
    soc_entity_id: str,
    range_entity_ids: list[str],
    charge_limit_readings: list[tuple[str, float]],
    capacity_readings: list[tuple[str, float]],
) -> tuple[str, ...]:
    """The sorted, deduplicated entities one vehicle's reading was taken from.

    Includes all charge-limit and capacity candidates, not only the winners.
    """
    return tuple(
        sorted(
            {
                soc_entity_id,
                *range_entity_ids,
                *(entity_id for entity_id, _ in charge_limit_readings),
                *(entity_id for entity_id, _ in capacity_readings),
            }
        )
    )


def _capacity_kwh(state: State, named: bool) -> float | None:
    """`state`'s value as kWh if it is a static energy reading inside the window.

    Cumulative `state_class` values are rejected: a lifetime counter passes any
    window at some point and then grows past it. The one exception is `total` (never
    `total_increasing`) in kWh on an entity whose key says capacity: a static pack size some
    integrations mis-declare as a meter.
    """
    state_class = str(state.attributes.get("state_class"))
    unit = str(state.attributes.get("unit_of_measurement"))
    mis_declared_size = named and state_class == "total" and unit == "kWh"
    if state_class in _CUMULATIVE_STATE_CLASSES and not mis_declared_size:
        return None
    factor = _ENERGY_UNITS.get(unit)
    if factor is None:
        return None
    value = _as_float(state.state)
    if value is None:
        return None
    kwh = value * factor
    low, high = _CAPACITY_KWH_WINDOW
    if not low <= kwh <= high:
        return None
    return kwh


def _energy_storage_capacity(entry: er.RegistryEntry, state: State, named: bool) -> float | None:
    if entry.domain not in ("number", "sensor"):
        return None
    if state.attributes.get("device_class") != _ENERGY_STORAGE_DEVICE_CLASS:
        return None
    return _capacity_kwh(state, named)


def _number_capacity(entry: er.RegistryEntry, state: State, named: bool) -> float | None:
    if entry.domain != "number":
        return None
    return _capacity_kwh(state, named)


def _energy_sensor_capacity(entry: er.RegistryEntry, state: State, named: bool) -> float | None:
    if entry.domain != "sensor":
        return None
    if state.attributes.get("device_class") != _ENERGY_DEVICE_CLASS:
        return None
    return _capacity_kwh(state, named)


def _is_percent_unit(unit: Any) -> bool:
    return isinstance(unit, str) and unit.strip() in _PERCENT_UNITS


def _as_float(value: Any) -> float | None:
    """`value` as a finite float, or `None`; never treats "not readable" as zero."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
