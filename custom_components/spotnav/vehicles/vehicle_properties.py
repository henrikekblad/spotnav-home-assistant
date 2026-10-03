"""A vehicle's own properties: battery size and consumption, kept per vehicle, not per charger.

Stored beside the vehicle's other decisions (`discovery_decisions.DECISION_DOMAIN_VEHICLE_PROPERTIES`,
keyed by device id). This module is the one writer of that domain and holds the resolution rules:

* Capacity: the vehicle-reported figure, else the stored one, else missing.
* Consumption: the vehicle's stored figure, else the planner's default
  (`auto_settings.DEFAULT_CONSUMPTION_KWH_PER_10KM`).
* Onboard charger: 1 or 3 phases, else three (`DEFAULT_ONBOARD_PHASES`). A charge uses the smaller
  of this and the charger's wiring (`planning/phases.py`).

Payload: `{"capacity_kwh": 77.4, "consumption_kwh_per_10km": 1.9, "onboard_phases": 1}`, every key
optional; an empty payload is no record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping

from homeassistant.core import HomeAssistant

from ..runtime import domain_data
from ..util import finite_number
from .discovery_decisions import DECISION_DOMAIN_VEHICLE_PROPERTIES, DiscoveryDecisionStore
from .vehicle_discovery import resolve_target_vehicle


CAPACITY_MIN_KWH: Final = 1.0
CAPACITY_MAX_KWH: Final = 500.0

KEY_CAPACITY: Final = "capacity_kwh"
KEY_CONSUMPTION: Final = "consumption_kwh_per_10km"
KEY_ONBOARD_PHASES: Final = "onboard_phases"
PROPERTY_KEYS: Final = (KEY_CAPACITY, KEY_CONSUMPTION, KEY_ONBOARD_PHASES)

#: What a vehicle with no stored onboard charger is taken to have.
DEFAULT_ONBOARD_PHASES: Final = 3

ERR_INVALID_CAPACITY: Final = "invalid_capacity"
ERR_INVALID_CONSUMPTION: Final = "invalid_consumption"
ERR_INVALID_ONBOARD_PHASES: Final = "invalid_onboard_phases"
ERR_UNKNOWN_FIELD: Final = "unknown_field"

CapacitySource = Literal["reported", "stored"]


@dataclass(frozen=True, slots=True)
class VehicleProperties:
    """What is stored for one vehicle; `None` means "never told"."""

    capacity_kwh: float | None = None
    consumption_kwh_per_10km: float | None = None
    onboard_phases: int | None = None

    @property
    def phases(self) -> int:
        """The onboard charger's phases as planning takes them: stored, else three."""
        return self.onboard_phases if self.onboard_phases is not None else DEFAULT_ONBOARD_PHASES


def valid_capacity(value: Any) -> bool:
    number = finite_number(value)
    return number is not None and CAPACITY_MIN_KWH <= number <= CAPACITY_MAX_KWH


def valid_consumption(value: Any) -> bool:
    """Positive and finite: the settings model's own range (the entity's 50 cap is only its UI)."""
    number = finite_number(value)
    return number is not None and number > 0


def valid_onboard_phases(value: Any) -> bool:
    """Exactly 1 or 3, as a whole number (a boolean is not one)."""
    return not isinstance(value, bool) and isinstance(value, int) and value in (1, 3)


def validate_changes(changes: Any) -> dict[str, str]:
    """`{field: code}` for everything wrong with a `changes` object; empty when it can be written.

    A value of `None` clears that property. Unknown keys, and an object with nothing in it, are refused.
    """
    if not isinstance(changes, Mapping) or not changes:
        return {"changes": "invalid_changes"}
    errors: dict[str, str] = {}
    for key, value in changes.items():
        if key == KEY_CAPACITY:
            if value is not None and not valid_capacity(value):
                errors[key] = ERR_INVALID_CAPACITY
        elif key == KEY_CONSUMPTION:
            if value is not None and not valid_consumption(value):
                errors[key] = ERR_INVALID_CONSUMPTION
        elif key == KEY_ONBOARD_PHASES:
            if value is not None and not valid_onboard_phases(value):
                errors[key] = ERR_INVALID_ONBOARD_PHASES
        else:
            errors[str(key)] = ERR_UNKNOWN_FIELD
    return errors


def stored_properties(hass: HomeAssistant, vehicle_id: object) -> VehicleProperties:
    """The properties stored for `vehicle_id` (all `None` for an unknown or blank id)."""
    store = domain_data(hass).decision_store
    if store is None or not isinstance(vehicle_id, str) or not vehicle_id:
        return VehicleProperties()
    payload = store.confirmed_payload(DECISION_DOMAIN_VEHICLE_PROPERTIES, vehicle_id) or {}
    capacity = payload.get(KEY_CAPACITY)
    consumption = payload.get(KEY_CONSUMPTION)
    onboard = payload.get(KEY_ONBOARD_PHASES)
    return VehicleProperties(
        capacity_kwh=float(capacity) if valid_capacity(capacity) else None,
        consumption_kwh_per_10km=float(consumption) if valid_consumption(consumption) else None,
        onboard_phases=onboard if valid_onboard_phases(onboard) else None,
    )


def stored_capacity_kwh(hass: HomeAssistant, vehicle_id: str | None) -> float | None:
    """The capacity stored for this vehicle, or `None`."""
    return stored_properties(hass, vehicle_id).capacity_kwh


def consumption_kwh_per_10km(hass: HomeAssistant, vehicle_id: str | None) -> float | None:
    """The vehicle's stored consumption, or `None` (the caller falls back to the default)."""
    return stored_properties(hass, vehicle_id).consumption_kwh_per_10km


def onboard_phases(hass: HomeAssistant, vehicle_id: str | None) -> int:
    """The vehicle's onboard charger phases (1 or 3): stored, else three."""
    return stored_properties(hass, vehicle_id).phases


def resolved_vehicle_id(hass: HomeAssistant, entry_id: str) -> str | None:
    """The vehicle this charger plans for (stored, else the only one), or `None`."""
    auto = domain_data(hass).auto_store
    stored = None if auto is None else auto.settings(entry_id).target.vehicle_id
    return resolve_target_vehicle(hass, stored)[0]


async def async_update_vehicle_properties(
    hass: HomeAssistant,
    store: DiscoveryDecisionStore,
    vehicle_id: str,
    changes: Mapping[str, Any],
) -> VehicleProperties:
    """The one writer: merge `changes` into `vehicle_id`'s stored properties (`None` clears one).

    The caller has validated the vehicle and `validate_changes(changes)`; a failing value raises
    `ValueError`. An emptied record is removed. Returns what is stored afterwards.
    """
    if validate_changes(changes):
        raise ValueError("invalid vehicle property changes")
    payload: dict[str, Any] = {}
    current = stored_properties(hass, vehicle_id)
    if current.capacity_kwh is not None:
        payload[KEY_CAPACITY] = current.capacity_kwh
    if current.consumption_kwh_per_10km is not None:
        payload[KEY_CONSUMPTION] = current.consumption_kwh_per_10km
    if current.onboard_phases is not None:
        payload[KEY_ONBOARD_PHASES] = current.onboard_phases
    for key, value in changes.items():
        if value is None:
            payload.pop(key, None)
        elif key == KEY_ONBOARD_PHASES:
            payload[key] = int(value)
        else:
            payload[key] = float(value)
    if payload:
        await store.async_confirm(DECISION_DOMAIN_VEHICLE_PROPERTIES, vehicle_id, payload)
    else:
        await store.async_unconfirm(DECISION_DOMAIN_VEHICLE_PROPERTIES, vehicle_id)
    return stored_properties(hass, vehicle_id)
