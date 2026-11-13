"""The site options' switched load page: reached from the site's page, saved under the site entry, removed when the
switch is left empty, refused when half filled in, and explained in every language."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.spotnav import texts

from .helpers import make_site_entry

TRANSLATIONS = Path(__file__).parents[1] / "custom_components" / "spotnav" / "translations"
LANGUAGES = ("en", "sv", "da", "nb", "fi", "de", "nl", "fr", "es")
BASICS = {
    "main_fuse_a": 25.0,
    "safety_margin_a": 1.0,
    "measurement_mode": "direct_phase_current",
    "charger_entry_ids": [],
    "max_age_s": 120.0,
}


@pytest.fixture(autouse=True)
async def _texts_loaded(hass: HomeAssistant) -> None:
    """Read SpotNav's texts off the loop first, as setup does (a site reloaded by the flow reads them)."""
    await texts.async_load(hass)


LOAD = {"switch_entity": "switch.water_heater", "rated_kw": 3.0, "runtime_h": 3.0}


async def _to_the_page(hass: HomeAssistant, **extra):
    entry = make_site_entry(hass, entry_id="site", extra_data=extra or None)
    hass.states.async_set("switch.water_heater", "off")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASICS, "change_switch_load": True}
    )
    assert result["step_id"] == "site_switch_load"
    return entry, result


async def test_a_load_is_saved_with_the_basics_and_nothing_else_changes(hass: HomeAssistant) -> None:
    entry, result = await _to_the_page(hass)
    before = dict(entry.data)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**LOAD, "temperature_entity": "sensor.tank", "floor_c": 45.0}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    after = dict(hass.config_entries.async_get_entry(entry.entry_id).data)
    assert after["switch_load"] == {
        "entity_id": "switch.water_heater", "rated_kw": 3.0, "runtime_h": 3.0,
        "temperature_entity": "sensor.tank", "floor_c": 45.0,
    }
    assert {key for key in after if after[key] != before.get(key)} <= {"switch_load", "battery_aggregate_power_entity"}
    assert "change_switch_load" not in after


async def test_the_page_is_filled_in_from_the_stored_load_and_an_empty_switch_removes_it(hass: HomeAssistant) -> None:
    stored = {"entity_id": "switch.water_heater", "rated_kw": 2.0, "runtime_h": 4.0}
    entry, result = await _to_the_page(hass, switch_load=stored)

    suggested = {
        str(marker): marker.description["suggested_value"]
        for marker in result["data_schema"].schema
        if marker.description
    }
    assert suggested == {"switch_entity": "switch.water_heater", "rated_kw": 2.0, "runtime_h": 4.0}

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "switch_load" not in hass.config_entries.async_get_entry(entry.entry_id).data


async def test_a_half_filled_page_is_refused(hass: HomeAssistant) -> None:
    _entry, result = await _to_the_page(hass)

    missing = await hass.config_entries.options.async_configure(result["flow_id"], {"switch_entity": "switch.water_heater"})
    assert missing["errors"] == {"base": "switch_load_incomplete"}
    floor = await hass.config_entries.options.async_configure(missing["flow_id"], {**LOAD, "floor_c": 45.0})
    assert floor["errors"] == {"base": "switch_load_floor_pair"}


async def test_unticked_the_stored_load_is_kept(hass: HomeAssistant) -> None:
    stored = {"entity_id": "switch.water_heater", "rated_kw": 2.0, "runtime_h": 4.0}
    entry = make_site_entry(hass, entry_id="site", extra_data={"switch_load": stored})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], BASICS)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config_entries.async_get_entry(entry.entry_id).data["switch_load"] == stored


async def test_the_measurement_steps_still_follow_when_asked(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="site")
    hass.states.async_set("switch.water_heater", "off")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**BASICS, "change_switch_load": True, "change_measurement": True}
    )
    assert result["step_id"] == "site_switch_load"

    result = await hass.config_entries.options.async_configure(result["flow_id"], LOAD)

    assert result["step_id"] == "site_current_suggestions"


async def test_every_field_is_explained_in_every_language(hass: HomeAssistant) -> None:
    _entry, result = await _to_the_page(hass)
    fields = [str(marker) for marker in result["data_schema"].schema]

    for language in LANGUAGES:
        tree = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))["options"]
        step = tree["step"]["site_switch_load"]
        assert step["title"] and step["description"], language
        assert [name for name in fields if not step["data"].get(name)] == [], language
        assert [name for name in fields if not step["data_description"].get(name)] == [], language
        assert tree["error"]["switch_load_incomplete"] and tree["error"]["switch_load_floor_pair"], language
        assert tree["step"]["site_init"]["data_description"]["change_switch_load"], language
