"""Editing the site's basics does not drag the owner through the measurement and wiring step."""

from __future__ import annotations

import json
from pathlib import Path

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .helpers import create_ocpp_charger_device, make_entry, make_ocpp_config_entry, make_site_entry

WIRING = {"phases": 3, "phase": None}
TRANSLATIONS = Path(__file__).parents[1] / "custom_components" / "spotnav" / "translations"
LANGUAGES = ("en", "sv", "da", "nb", "fi", "de", "nl", "fr", "es")


def _basics(charger_ids: list[str], **extra) -> dict:
    return {
        "main_fuse_a": 25.0,
        "safety_margin_a": 1.0,
        "measurement_mode": "direct_phase_current",
        "charger_entry_ids": charger_ids,
        "max_age_s": 120.0,
        **extra,
    }


def _site(hass: HomeAssistant, charger_ids: list[str]):
    return make_site_entry(
        hass,
        entry_id="site",
        charger_entry_ids=charger_ids,
        phase_wiring={cid: dict(WIRING) for cid in charger_ids},
    )


async def test_unticked_saves_at_once_and_changes_only_the_basics(hass: HomeAssistant) -> None:
    entry = _site(hass, [])
    before = dict(entry.data)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], _basics([]))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    after = dict(hass.config_entries.async_get_entry(entry.entry_id).data)
    assert after["phase_wiring"] == before["phase_wiring"]
    assert after["direct_entities"] == before["direct_entities"]
    changed = {key for key in after if after[key] != before.get(key)}
    assert changed <= {"battery_aggregate_power_entity"}


async def test_ticked_shows_the_measurement_step(hass: HomeAssistant) -> None:
    entry = _site(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _basics([], change_measurement=True)
    )

    assert result["step_id"] == "site_current_suggestions"


async def test_an_added_charger_needs_its_wiring_so_the_step_is_shown(hass: HomeAssistant) -> None:
    owner = make_ocpp_config_entry(hass, entry_id="owner")
    switch = create_ocpp_charger_device(hass, ocpp_entry=owner, device_unique_id="c1", switch_object_id="c1")
    charger = make_entry(
        hass, entry_id="c1", charge_control=switch, current_limit=None, webhook_id="h", title="C1"
    )
    entry = _site(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _basics([charger.entry_id])
    )

    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "site_details"
    assert "chargers changed" in result["description_placeholders"]["reason"]


async def test_a_changed_mode_shows_the_step(hass: HomeAssistant) -> None:
    entry = _site(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _basics([], measurement_mode="derived_phase_current")
    )

    assert result["step_id"] == "site_details"
    assert "mode changed" in result["description_placeholders"]["reason"]


async def test_the_page_ends_with_the_way_on_and_explains_every_field(hass: HomeAssistant) -> None:
    entry = _site(hass, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)

    fields = [str(marker.schema if isinstance(marker, vol.Marker) else marker) for marker in result["data_schema"].schema]
    assert fields[-1] == "change_measurement"
    assert "site_enabled" not in fields
    for language in LANGUAGES:
        step = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))["options"]["step"]["site_init"]
        assert "site_enabled" not in step["data"], language
        assert [name for name in fields if not step["data_description"].get(name)] == [], language
