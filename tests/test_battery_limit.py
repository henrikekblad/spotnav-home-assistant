"""A battery integration's own grid import limit set against SpotNav's: "two limits on one fuse".

The Sigenergy integration's `Grid Import Limitation` number (kW, disabled by default; 4294967.295 kW is "no
limit") is read from the device of the site's battery entity and compared, per phase at the site's voltage,
with the main fuse minus the safety margin. The site's warnings say so when they differ by more than 1 A.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api.entity_fields import site_measurement_info
from custom_components.spotnav.site.battery_limit import battery_import_limit

from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

#: 25 A fuse less the 1 A margin of the test site, at the default 400 V between phases.
SPOTNAV_KW = 24 * math.sqrt(3) * 400 / 1000


def _sigenergy(
    hass: HomeAssistant, kilowatts: str, *, platform: str = "sigen", disabled: bool = False, unit: str = "kW"
) -> tuple[str, str]:
    """A Sigenergy plant: its battery power sensor and the grid import limit number on one device."""
    owner = MockConfigEntry(domain=platform, entry_id=f"owner_{platform}")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={(platform, "plant")}, name="Sigen Plant"
    )
    registry = er.async_get(hass)
    battery = registry.async_get_or_create(
        "sensor", platform, f"{owner.entry_id}_sigen_plant_plant_ess_power", device_id=device.id,
        config_entry=owner, suggested_object_id="sigen_plant_ess_power",
    )
    hass.states.async_set(battery.entity_id, "0", {"unit_of_measurement": "kW"})
    limit = registry.async_get_or_create(
        "number", platform, f"{owner.entry_id}_sigen_plant_plant_grid_maximum_import_limitation",
        device_id=device.id, config_entry=owner, suggested_object_id="sigen_plant_grid_import_limitation",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION if disabled else None,
    )
    hass.states.async_set(limit.entity_id, kilowatts, {"unit_of_measurement": unit})
    return battery.entity_id, limit.entity_id


async def _site(hass: HomeAssistant, battery_entity: str):
    _charger, site = await setup_charger_and_site(hass, battery_aggregate_power_entity=battery_entity)
    assert site is not None
    return site


def _codes(hass: HomeAssistant, site: Any) -> list[dict[str, Any]]:
    warnings = site_measurement_info(hass, site)["warnings"]
    return [item for item in warnings if item["code"] == "battery_import_limit_differs"]


async def test_a_limit_that_differs_by_more_than_an_ampere_per_phase_is_a_site_warning(hass: HomeAssistant) -> None:
    battery, limit = _sigenergy(hass, "11.0")
    site = await _site(hass, battery)

    found = battery_import_limit(hass, site)

    assert found is not None and found.entity_id == limit and found.differs
    assert found.battery_a == pytest.approx(11000 / (math.sqrt(3) * 400))
    [warning] = _codes(hass, site)
    assert warning["integration"] == "sigen" and warning["entity_id"] == limit
    assert warning["limits_a"] == {"battery": 15.9, "spotnav": 24.0}


async def test_every_other_warning_states_no_limits(hass: HomeAssistant) -> None:
    battery, _limit = _sigenergy(hass, "11.0")
    site = await _site(hass, battery)
    from custom_components.spotnav.const import CONF_BATTERY_AGGREGATE_POWER_ENTITY

    assert site.data[CONF_BATTERY_AGGREGATE_POWER_ENTITY] == battery
    others = [w for w in site_measurement_info(hass, site)["warnings"] if w["code"] != "battery_import_limit_differs"]
    assert others and all(w["limits_a"] is None for w in others)


async def test_limits_within_an_ampere_agree(hass: HomeAssistant) -> None:
    battery, _limit = _sigenergy(hass, f"{SPOTNAV_KW + 0.5:.3f}")
    site = await _site(hass, battery)

    found = battery_import_limit(hass, site)

    assert found is not None and not found.differs
    assert _codes(hass, site) == []


@pytest.mark.parametrize("value", ["4294967.295", "unavailable", "unknown", "-1"])
async def test_no_limit_or_no_reading_is_no_warning(hass: HomeAssistant, value: str) -> None:
    battery, _limit = _sigenergy(hass, value)
    site = await _site(hass, battery)

    assert battery_import_limit(hass, site) is None
    assert _codes(hass, site) == []


async def test_a_disabled_entity_is_not_read(hass: HomeAssistant) -> None:
    battery, _limit = _sigenergy(hass, "11.0", disabled=True)
    site = await _site(hass, battery)

    assert battery_import_limit(hass, site) is None


async def test_watts_are_converted(hass: HomeAssistant) -> None:
    battery, _limit = _sigenergy(hass, "11000", unit="W")
    site = await _site(hass, battery)

    found = battery_import_limit(hass, site)

    assert found is not None and found.battery_a == pytest.approx(11000 / (math.sqrt(3) * 400))


async def test_another_integrations_battery_is_not_looked_into(hass: HomeAssistant) -> None:
    battery, _limit = _sigenergy(hass, "11.0", platform="other_battery")
    site = await _site(hass, battery)

    assert battery_import_limit(hass, site) is None


async def test_the_site_voltage_is_the_one_the_limit_is_figured_at(hass: HomeAssistant) -> None:
    from custom_components.spotnav.const import CONF_VOLTAGE_BETWEEN_PHASES_V

    battery, _limit = _sigenergy(hass, "11.0")
    site = await _site(hass, battery)
    hass.config_entries.async_update_entry(site, data={**site.data, CONF_VOLTAGE_BETWEEN_PHASES_V: 230})

    found = battery_import_limit(hass, site)

    assert found is not None and found.battery_a == pytest.approx(11000 / (math.sqrt(3) * 230))
