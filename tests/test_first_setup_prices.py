"""The first setup's step about where prices come from (`flows/price_source_step.py`).

The installation's first charger is added through the real config flow, on the offline relay (whose area list holds
SE4) and with Home Assistant's location in southern Sweden; the Transparency Platform is the fake behind Home
Assistant's session.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CURRENT_LIMIT
from custom_components.spotnav.flows import price_source_step
from custom_components.spotnav.pricing.providers import entsoe as entsoe_module
from custom_components.spotnav.runtime import domain_data

from .charger_shapes import register_shape, SHAPES
from .entsoe_helpers import SECRET
from .test_charger_config_flow import _to_detected_entities
from .test_price_provider_entsoe import FakePlatform
from .world import setup_charger

pytestmark = pytest.mark.usefixtures("offline_relay")

WALLBOX = {
    CONF_CHARGE_CONTROL: "switch.wallbox_pause_resume",
    CONF_CURRENT_LIMIT: "number.wallbox_maximum_charging_current",
}


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    fake = FakePlatform()
    monkeypatch.setattr(entsoe_module, "async_get_clientsession", lambda _hass: fake)
    monkeypatch.setattr(entsoe_module, "MIN_SPACING_S", 0.0)
    return fake


@pytest.fixture(autouse=True)
def in_southern_sweden(hass: HomeAssistant) -> None:
    hass.config.country, hass.config.latitude, hass.config.longitude = "SE", 55.6, 13.0


async def add_wallbox(hass: HomeAssistant) -> dict[str, Any]:
    ids = register_shape(hass, SHAPES["wallbox"])
    result = await _to_detected_entities(hass, ids["device_id"])
    return await hass.config_entries.flow.async_configure(result["flow_id"], WALLBOX)


async def test_the_first_charger_in_a_nord_pool_area_is_told_about_nord_pool(hass: HomeAssistant) -> None:
    result = await add_wallbox(hass)

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "prices_nordpool"
    assert result["description_placeholders"] == {
        "area": "SE4",
        "link": "/config/integrations/dashboard/add?domain=nordpool",
        "entsoe_url": "https://transparency.entsoe.eu/",
    }
    assert [str(key) for key in result["data_schema"].schema] == ["entsoe_key"]

    # Passed without acting: the charger is created, and nothing is stored.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY and result["title"] == "wallbox charger"
    assert not domain_data(hass).price_store.entsoe_key_set


async def test_a_nordpool_entry_that_did_not_start_is_linked_to_its_page(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_ERROR)

    result = await add_wallbox(hass)

    assert result["step_id"] == "prices_nordpool"
    assert result["description_placeholders"]["link"] == "/config/integrations/integration/nordpool"


async def test_with_nord_pool_loaded_there_is_no_step(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="nordpool", title="Nord Pool", data={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    try:
        result = await add_wallbox(hass)
    finally:
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_only_the_first_charger_is_asked(hass: HomeAssistant) -> None:
    await setup_charger(hass, entry_id="entry_first", webhook_id="webhook-first")
    assert (await add_wallbox(hass))["type"] == FlowResultType.CREATE_ENTRY


async def test_without_a_country_there_is_no_step(hass: HomeAssistant) -> None:
    hass.config.country = None
    assert (await add_wallbox(hass))["type"] == FlowResultType.CREATE_ENTRY


async def test_an_entsoe_area_takes_the_key_and_a_rejected_one_keeps_the_step(
    hass: HomeAssistant, platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(price_source_step, "suggest_area", lambda *_args: "ES")
    result = await add_wallbox(hass)
    assert result["step_id"] == "prices_entsoe"
    assert result["description_placeholders"] == {"area": "ES", "link": "", "entsoe_url": "https://transparency.entsoe.eu/"}

    platform.fallback = (401, "Unauthorized", {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entsoe_key": "wrong"})
    assert result["type"] == FlowResultType.FORM and result["errors"] == {"entsoe_key": "entsoe_key_rejected"}
    assert not domain_data(hass).price_store.entsoe_key_set

    platform.fallback = (503, "", {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entsoe_key": SECRET})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # The platform could not be asked: kept, and proved by the first day it is asked for.
    assert domain_data(hass).price_store.entsoe_key_state == "unchecked"


@pytest.mark.parametrize(
    ("area", "step", "link"),
    [
        ("ES-PVPC", "prices_pvpc", "/config/integrations/dashboard/add?domain=pvpc_hourly_pricing"),
        ("GB-C", "prices_octopus", "https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy"),
    ],
)
async def test_pvpc_and_agile_link_their_integrations(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, area: str, step: str, link: str
) -> None:
    monkeypatch.setattr(price_source_step, "suggest_area", lambda *_args: area)
    result = await add_wallbox(hass)
    assert result["step_id"] == step and result["description_placeholders"] == {
        "area": area, "link": link, "entsoe_url": "https://transparency.entsoe.eu/"
    }
    assert [str(key) for key in result["data_schema"].schema] == []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_an_area_nobody_serves_has_no_step(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(price_source_step, "suggest_area", lambda *_args: "XX")
    assert (await add_wallbox(hass))["type"] == FlowResultType.CREATE_ENTRY
