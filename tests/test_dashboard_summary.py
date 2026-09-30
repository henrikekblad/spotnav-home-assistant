"""The dashboard's `summary` block: the setup in words, never in entity ids, over both carriers."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.api.dashboard import serialize_dashboard
from custom_components.spotnav.api.entity_fields import charger_field_descriptors, site_field_descriptors
from custom_components.spotnav.const import CONF_ENERGY_REGISTER_ENTITY

from .helpers import make_entry, make_site_entry
from .test_dashboard_api import walk


def _summary(hass: HomeAssistant, entry: Any) -> dict[str, Any]:
    return serialize_dashboard(dashboard_api.capture_dashboard(hass, entry), can_act=True)["summary"]


def _vehicle(hass: HomeAssistant) -> str:
    owner = MockConfigEntry(domain="car_brand")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("car_brand", "car")}, name="Family car"
    )
    registry = er.async_get(hass)
    level = registry.async_get_or_create(
        "sensor", "car_brand", "car-ev_battery_percentage", device_id=device.id,
        translation_key="ev_battery_percentage", suggested_object_id="car_level",
    )
    reach = registry.async_get_or_create(
        "sensor", "car_brand", "car-range", device_id=device.id, suggested_object_id="car_range"
    )
    hass.states.async_set(
        level.entity_id, "64",
        {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Family car battery level"},
    )
    hass.states.async_set(reach.entity_id, "300", {"device_class": "distance", "unit_of_measurement": "km"})
    return device.id


async def test_the_summary_names_the_setup_and_never_an_entity_id(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.wallbox", "off", {"friendly_name": "Wallbox switch"})
    hass.states.async_set("number.wallbox_limit", "16", {"min": 6, "max": 16, "friendly_name": "Wallbox limit"})
    hass.states.async_set("sensor.wallbox_energy", "5", {"friendly_name": "Wallbox energy"})
    hass.states.async_set("sensor.house_battery", "0", {"friendly_name": "House battery power"})
    vehicle_id = _vehicle(hass)
    charger = make_entry(
        hass, entry_id="charger", charge_control="switch.wallbox", current_limit="number.wallbox_limit",
        webhook_id="webhook-charger", title="Wallbox", current_control="number",
        extra={CONF_ENERGY_REGISTER_ENTITY: "sensor.wallbox_energy"},
    )
    site = make_site_entry(
        hass, entry_id="site", charger_entry_ids=[charger.entry_id], main_fuse_a=20.0,
        battery_aggregate_power_entity="sensor.house_battery",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    summary = _summary(hass, charger)

    assert summary == {
        "charger": {
            "start_stop_name": "Wallbox switch",
            "current_path": "number",
            "current_entity_name": "Wallbox limit",
            "energy_name": "Wallbox energy",
            "energy_automatic": False,
        },
        "site": {"main_fuse_a": 20.0, "measurement_mode": "direct_phase_current", "battery_name": "House battery power"},
        "vehicles": {vehicle_id: {"soc_sensor_name": "Family car battery level"}},
    }
    for path, value in walk(summary):
        if isinstance(value, str):
            assert not any(fragment in value for fragment in ("sensor.", "switch.", "number.")), path

    # The card's own summaries read the same facts from the entity configuration.
    by_name = {field["field"]: field for field in charger_field_descriptors(hass, charger)}
    assert by_name["charge_control"]["current"]["friendly_name"] == summary["charger"]["start_stop_name"]
    assert by_name["current_limit"]["current"]["friendly_name"] == summary["charger"]["current_entity_name"]
    assert by_name["energy_register_entity"]["current"]["friendly_name"] == summary["charger"]["energy_name"]
    site_fields = {field["field"]: field for field in site_field_descriptors(hass, site)}
    assert site_fields["main_fuse_a"]["value"] == summary["site"]["main_fuse_a"]
    assert site_fields["measurement_mode"]["value"] == summary["site"]["measurement_mode"]
    assert (
        site_fields["battery_aggregate_power_entity"]["current"]["friendly_name"]
        == summary["site"]["battery_name"]
    )


async def test_an_unconfigured_charger_has_no_site_and_an_automatic_energy_register(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.solo", "off", {"friendly_name": "Solo switch"})
    charger = make_entry(
        hass, entry_id="solo", charge_control="switch.solo", current_limit=None,
        webhook_id="webhook-solo", title="Solo",
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()

    assert _summary(hass, charger) == {
        "charger": {
            "start_stop_name": "Solo switch",
            "current_path": "none",
            "current_entity_name": None,
            "energy_name": None,
            "energy_automatic": True,
        },
        "site": None,
        "vehicles": {},
    }
