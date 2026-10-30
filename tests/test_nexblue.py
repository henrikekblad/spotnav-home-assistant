"""NexBlue (core `nexblue`): read only, because no version of the integration can set the current. Its phase
currents and lifetime energy are still recognised, from the registry entries the integration creates
(`<serial>_<key>` unique ids; the three phase currents share the translation key `current`)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.execution.charger_profiles import profile_for, ROLE_MEASUREMENT_ONLY
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.discovery import (
    async_discover_charger_current_sources,
    REASON_SEPARATE_ENTITIES_PROFILE_MATCH,
)

SERIAL = "NB2412345678"
PHASES = ("L1", "L2", "L3")


def _nexblue(hass: HomeAssistant, *, with_switch: bool) -> tuple[str, list[str]]:
    """The sensors of Home Assistant 2026.9 and, from 2026.10, the `charging` switch."""
    config = MockConfigEntry(domain="nexblue", entry_id="nexblue")
    config.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config.entry_id, identifiers={("nexblue", SERIAL)}, manufacturer="NexBlue", name=SERIAL
    )
    registry = er.async_get(hass)

    def add(domain: str, key: str, translation_key: str | None, state: str, attrs: dict) -> str:
        entry = registry.async_get_or_create(
            domain, "nexblue", f"{SERIAL}_{key}", config_entry=config, device_id=device.id,
            translation_key=translation_key, suggested_object_id=f"nexblue_{key}",
        )
        hass.states.async_set(entry.entity_id, state, attrs)
        return entry.entity_id

    add("sensor", "charging_state", "charging_state", "charging", {"device_class": "enum"})
    add("sensor", "power", None, "7.2", {"device_class": "power", "unit_of_measurement": "kW"})
    add("sensor", "energy", None, "3.1", {"device_class": "energy", "state_class": "total", "unit_of_measurement": "kWh"})
    add("sensor", "lifetime_energy", "lifetime_energy", "812.4", {"device_class": "energy", "state_class": "total_increasing", "unit_of_measurement": "kWh"})
    add("sensor", "current_limit", "current_limit", "16", {"device_class": "current", "unit_of_measurement": "A"})
    add("sensor", "cable_current_limit", "cable_current_limit", "32", {"device_class": "current", "unit_of_measurement": "A"})
    currents = [
        add("sensor", f"current_{n}", "current", value, {"device_class": "current", "unit_of_measurement": "A"})
        for n, value in zip((3, 1, 2), ("10.4", "10.1", "10.2"))
    ]
    if with_switch:
        add("switch", "charging", "charging", "on", {})
    # Registered L3 first, so the phase order comes from the keys and not from the registry.
    return device.id, [currents[1], currents[2], currents[0]]


async def test_nexblue_is_read_only_with_or_without_its_charging_switch(hass: HomeAssistant) -> None:
    device_id, _ = _nexblue(hass, with_switch=True)

    found = detect_charger(hass, device_id)

    assert found is not None
    assert found.role == ROLE_MEASUREMENT_ONLY
    assert found.charge_control is None and found.control_path is None
    profile = profile_for("nexblue")
    assert profile is not None and not profile.can_set_current


async def test_nexblue_phase_currents_are_offered_in_phase_order(hass: HomeAssistant) -> None:
    device_id, currents = _nexblue(hass, with_switch=False)

    candidates = await async_discover_charger_current_sources(hass, charger_device_id=device_id)

    by_profile = [c for c in candidates if c.reason_code == REASON_SEPARATE_ENTITIES_PROFILE_MATCH]
    assert len(by_profile) == 1
    assert dict(by_profile[0].mapping.entity_ids) == dict(zip(PHASES, currents))
