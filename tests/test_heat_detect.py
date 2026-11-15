"""The heat pumps found on the site by their integration's presence, through the registry, into the diagnostics."""

from __future__ import annotations

import json
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.heat.detect import heat_pump_diagnostics, read_heat_pumps

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _nibe(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="nibe_heatpump", title="F750")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("nibe_heatpump", "f750")})
    registry = er.async_get(hass)
    rows: list[tuple[str, str, str, dict[str, Any]]] = [
        ("sensor", "40004", "-3.2", {"unit_of_measurement": "°C"}),
        ("sensor", "40008", "38.4", {"unit_of_measurement": "°C"}),
        ("number", "43005", "-312.0", {"unit_of_measurement": "DM"}),
        ("sensor", "43086", "HOT WATER", {}),
        ("number", "47011", "-1.0", {"min": -10, "max": 10, "step": 1}),
    ]
    for domain, key, state, attributes in rows:
        item = registry.async_get_or_create(
            domain,
            "nibe_heatpump",
            f"{entry.entry_id}-{key}",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"nibe_{key}",
        )
        hass.states.async_set(item.entity_id, state, attributes)
    # A coil the person has not enabled (all are disabled by default).
    registry.async_get_or_create(
        "sensor",
        "nibe_heatpump",
        f"{entry.entry_id}-40033",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="nibe_40033",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    return entry


async def test_a_nibe_pump_is_found_by_its_integration_and_read(hass: HomeAssistant) -> None:
    _nibe(hass)

    pumps = read_heat_pumps(hass)

    assert len(pumps) == 1
    pump = pumps[0]
    assert pump.brand == "nibe_f"
    assert (pump.outdoor_temp_c, pump.supply_temp_c, pump.degree_minutes) == (-3.2, 38.4, -312.0)
    assert pump.priority == "hot_water"
    assert pump.missing["room_temp"] == "disabled"
    offset = next(capability for capability in pump.capabilities if capability.kind == "offset")
    assert (offset.entity_id, offset.minimum, offset.maximum, offset.step) == ("number.nibe_47011", -10.0, 10.0, 1.0)


async def test_effektguard_set_up_makes_the_pump_watch_only(hass: HomeAssistant) -> None:
    _nibe(hass)
    MockConfigEntry(domain="effektguard").add_to_hass(hass)

    diagnostics = heat_pump_diagnostics(hass)

    assert diagnostics["other_controllers"] == ["effektguard"]
    assert diagnostics["pumps"][0]["control"] == {
        "state": "watch_only",
        "reason": "controller:effektguard",
        "watch_only": True,
    }


async def test_no_heat_pump_integration_means_no_pump(hass: HomeAssistant) -> None:
    assert heat_pump_diagnostics(hass)["pumps"] == []


async def test_the_site_diagnostics_carry_the_heat_pumps(hass: HomeAssistant) -> None:
    _charger, site = await setup_charger_and_site(hass)
    assert site is not None
    _nibe(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, site)

    pumps = diagnostics["heat_pump"]["pumps"]
    assert [pump["brand"] for pump in pumps] == ["nibe_f"]
    assert pumps[0]["readings"]["outdoor_temp_c"] == -3.2
    json.dumps(diagnostics["heat_pump"])
