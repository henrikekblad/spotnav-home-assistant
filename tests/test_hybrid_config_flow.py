"""The site options flow's `CONF_SOLAR_FORECAST_ENTRIES` picker.

`config_flow._async_ensure_forecast_entry_options` offers config entries whose integration
implements the Energy dashboard's `async_get_solar_forecast` platform.
`hybrid_forecast.async_forecast_capable_domains` (the loader call) is monkeypatched to a fixed
domain set: these tests cover offering the right entries and persisting the choice.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.config_flow import options as config_flow_module
from custom_components.spotnav.const import CONF_SOLAR_FORECAST_ENTRIES

from .helpers import make_site_entry


@pytest.fixture(autouse=True)
def _forecast_capable_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(_hass: HomeAssistant) -> frozenset[str]:
        return frozenset({"forecast_solar"})

    monkeypatch.setattr(config_flow_module, "async_forecast_capable_domains", _fake)


def _forecast_entry(hass: HomeAssistant, *, entry_id: str, title: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain="forecast_solar", entry_id=entry_id, title=title)
    entry.add_to_hass(hass)
    return entry


async def test_the_options_flow_offers_and_persists_a_forecast_capable_entry(
    hass: HomeAssistant,
) -> None:
    forecast_entry = _forecast_entry(hass, entry_id="roof_a", title="Solcast roof A")
    site = make_site_entry(hass, entry_id="hybrid_opts", charger_entry_ids=[])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(site.entry_id)
    assert result["step_id"] == "site_init"
    options = result["data_schema"].schema[CONF_SOLAR_FORECAST_ENTRIES].config["options"]
    assert {"value": forecast_entry.entry_id, "label": "Solcast roof A"} in options

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Hybrid Opts",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": False,
            "change_measurement": True,
            "max_age_s": 120,
            CONF_SOLAR_FORECAST_ENTRIES: [forecast_entry.entry_id],
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.hybrid_opts_l1",
            "direct_L2": "sensor.hybrid_opts_l2",
            "direct_L3": "sensor.hybrid_opts_l3",
        },
    )
    assert result["type"].value == "create_entry"

    updated = hass.config_entries.async_get_entry(site.entry_id)
    assert updated.data[CONF_SOLAR_FORECAST_ENTRIES] == [forecast_entry.entry_id]


async def test_an_entry_no_longer_forecast_capable_stays_selectable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chosen entry whose integration no longer implements the platform is still offered: the
    stored value must stay selectable, as in the battery-power entity picker."""
    stale_entry = _forecast_entry(hass, entry_id="roof_b", title="No longer solar")
    site = make_site_entry(
        hass,
        entry_id="hybrid_opts_stale",
        charger_entry_ids=[],
    )
    hass.config_entries.async_update_entry(
        site, data={**site.data, CONF_SOLAR_FORECAST_ENTRIES: [stale_entry.entry_id]}
    )

    async def _fake_none(_hass: HomeAssistant) -> frozenset[str]:
        return frozenset()  # nothing implements the platform any more

    monkeypatch.setattr(config_flow_module, "async_forecast_capable_domains", _fake_none)

    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(site.entry_id)
    options = result["data_schema"].schema[CONF_SOLAR_FORECAST_ENTRIES].config["options"]
    assert {"value": stale_entry.entry_id, "label": "No longer solar"} in options
