"""What a loaded entry holds on `runtime_data`, and that unloading or failing to set up releases it."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.spotnav.runtime import (
    ChargerData,
    SiteData,
    SpotNavData,
    domain_data,
)

from .world import setup_charger, setup_site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_a_charger_entry_holds_its_live_objects(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)

    data = entry.runtime_data
    assert isinstance(data, ChargerData)
    assert data.controller.entry_id == entry.entry_id
    assert data.executor is not None and data.preview is not None
    assert data.observation is not None and data.solar is not None
    assert data.soc_reader is not None


async def test_a_site_entry_holds_its_controller(hass: HomeAssistant) -> None:
    entry = await setup_site(hass)

    assert isinstance(entry.runtime_data, SiteData)
    assert entry.runtime_data.controller.entry_id == entry.entry_id


async def test_the_installation_singletons_belong_to_no_entry(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    data = domain_data(hass)

    assert isinstance(data, SpotNavData)
    assert data.auto_store is not None and data.price_repository is not None
    assert data.price_refresh is not None and data.decision_store is not None
    assert data.pairing is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert domain_data(hass) is data
    assert data.auto_store is not None and data.price_refresh is not None


async def test_unloading_stops_everything_the_entry_started(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    data = entry.runtime_data
    manager = domain_data(hass).price_refresh
    assert data.executor.shutdown is False

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert data.executor.shutdown is True
    assert data.preview.snapshot().reason == "shutdown"
    assert "webhook-a" not in hass.data["webhook"]
    assert manager.manager_snapshot().areas == (), "the entry released its price areas"


async def test_a_setup_that_fails_after_the_objects_exist_stops_them(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(self, *args, **kwargs):
        raise RuntimeError("platforms refused")

    monkeypatch.setattr(type(hass.config_entries), "async_forward_entry_setups", refuse)
    with pytest.raises(Exception):
        await setup_charger(hass)

    entry = hass.config_entries.async_entries("spotnav")[0]
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.runtime_data.executor.shutdown is True
    assert entry.runtime_data.preview.snapshot().reason == "shutdown"
    assert "webhook-a" not in hass.data["webhook"]


async def test_a_reload_builds_a_fresh_set_of_objects(hass: HomeAssistant) -> None:
    entry = await setup_charger(hass)
    before = entry.runtime_data

    assert await hass.config_entries.async_reload(entry.entry_id)

    assert entry.runtime_data is not before
    assert entry.runtime_data.controller is not before.controller
    assert entry.state is ConfigEntryState.LOADED
