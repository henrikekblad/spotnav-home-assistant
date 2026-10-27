"""A Mazda MX-30 on Mazda Connected Services ng (`mazda_cs`), registered as that integration registers it.

From the integration's source (fano0001/home-assistant-mazda, manifest 2.2.1): sensors and binary sensors
have unique ids `<VIN>_<key>` and translation keys equal to the key, the device tracker and the charging
switch both use the bare VIN, and entity names hang off the vehicle's device (`has_entity_name`). Entity ids
are the ones a real MX-30 got. The level is read only (no charge-limit entity exists), the plug is a
`plug`-class binary sensor, the location the device tracker; a refresh button exists, but SpotNav has no wake
for the integration.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.vehicles.identification_sources import identification_sources
from custom_components.spotnav.vehicles.vehicle_discovery import (
    discover_ambiguous_vehicles,
    discover_vehicles,
    vehicle_charge_limit_entity_id,
    vehicle_soc_entity_id,
)
from custom_components.spotnav.vehicles.vehicle_wake import WAKE_METHODS, wake_supported

VIN = "JMZDRAAA000000000"

# (domain, unique id, translation key, object id, state, attributes)
MX_30: tuple[tuple[str, str, str | None, str, str, dict], ...] = (
    ("sensor", f"{VIN}_ev_charge_level", "ev_charge_level", "mx_30_charge_level", "64",
     {"device_class": "battery", "unit_of_measurement": "%", "state_class": "measurement"}),
    ("sensor", f"{VIN}_ev_remaining_range", "ev_remaining_range", "mx_30_remaining_range", "142",
     {"device_class": "distance", "unit_of_measurement": "km", "state_class": "measurement"}),
    ("sensor", f"{VIN}_odometer", "odometer", "mx_30_odometer", "12034",
     {"device_class": "distance", "unit_of_measurement": "km", "state_class": "total_increasing"}),
    ("sensor", f"{VIN}_ev_remaining_charging_time", "ev_remaining_charging_time",
     "mx_30_remaining_charging_time_ac", "120", {"device_class": "duration", "unit_of_measurement": "min"}),
    ("sensor", f"{VIN}_soc_ecm_a_est", "soc_ecm_a_est", "mx_30_12v_battery_state_of_charge", "88.2",
     {"unit_of_measurement": "%", "state_class": "measurement"}),
    ("binary_sensor", f"{VIN}_ev_plugged_in", "ev_plugged_in", "mx_30_plugged_in", "on", {"device_class": "plug"}),
    ("binary_sensor", f"{VIN}_ev_battery_heater_on", "ev_battery_heater_on", "mx_30_battery_heater", "off", {}),
    ("binary_sensor", f"{VIN}_driver_door", "driver_door", "mx_30_driver_door", "off", {"device_class": "door"}),
    ("device_tracker", VIN, "device_tracker", "mx_30_device_tracker", "home", {"source_type": "gps"}),
    ("switch", VIN, "charging", "mx_30_charging", "on", {}),
    ("button", f"{VIN}_refresh_vehicle_status", "refresh_vehicle_status", "mx_30_refresh_status", "unknown", {}),
)


def register_mx_30(hass: HomeAssistant) -> str:
    entry = MockConfigEntry(domain="mazda_cs", entry_id="mazda")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("mazda_cs", VIN)}, manufacturer="Mazda", model="MX-30",
        name="MX-30",
    )
    registry = er.async_get(hass)
    for domain, unique_id, translation_key, object_id, state, attributes in MX_30:
        registered = registry.async_get_or_create(
            domain, "mazda_cs", unique_id, config_entry=entry, device_id=device.id,
            suggested_object_id=object_id, translation_key=translation_key, has_entity_name=True,
        )
        assert registered.entity_id == f"{domain}.{object_id}"
        hass.states.async_set(registered.entity_id, state, attributes)
    return device.id


async def test_the_mx_30_is_a_vehicle_by_its_charge_level_with_no_charge_limit(hass: HomeAssistant) -> None:
    vehicle_id = register_mx_30(hass)

    vehicles = {vehicle.id: vehicle for vehicle in discover_vehicles(hass)}
    assert vehicle_id in vehicles and vehicle_id not in {c.id for c in discover_ambiguous_vehicles(hass)}
    assert vehicle_soc_entity_id(hass, vehicle_id) == "sensor.mx_30_charge_level"
    assert vehicle_charge_limit_entity_id(hass, vehicle_id) is None
    assert vehicles[vehicle_id].target_soc_percent_max is None


async def test_the_mx_30_plug_and_tracker_tell_which_car_is_at_the_charger(hass: HomeAssistant) -> None:
    vehicle_id = register_mx_30(hass)

    plug, location = identification_sources(hass, vehicle_id)
    assert plug.entity_id == "binary_sensor.mx_30_plugged_in" and plug.chosen is False
    assert location.entity_id == "device_tracker.mx_30_device_tracker" and location.chosen is False


async def test_the_mx_30_is_never_woken(hass: HomeAssistant) -> None:
    vehicle_id = register_mx_30(hass)

    assert "mazda_cs" not in WAKE_METHODS
    assert wake_supported(hass, vehicle_id) is False
