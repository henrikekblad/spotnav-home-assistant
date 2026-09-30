"""After a charger is confirmed, the installation's one site may be offered it."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.spotnav.const import DOMAIN

from .helpers import create_ocpp_charger_device, make_ocpp_config_entry, make_site_entry


def _switch(hass: HomeAssistant, *, attributes: dict | None, name: str = "halo") -> str:
    owner = make_ocpp_config_entry(hass, entry_id=f"owner_{name}")
    return create_ocpp_charger_device(
        hass, ocpp_entry=owner, device_unique_id=name, switch_object_id=name, current_attributes=attributes
    )


async def _generic(hass: HomeAssistant, switch: str) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "charger"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mode": "generic"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"charge_control": switch})


async def test_the_type_choice_is_automatic_or_generic(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "charger"})
    field = next(key for key in result["data_schema"].schema if str(key) == "mode")

    assert result["data_schema"].schema[field].config["options"] == ["detected", "generic"]
    assert field.default() == "detected"


async def test_without_a_site_the_charger_is_created_at_once(hass: HomeAssistant) -> None:
    result = await _generic(hass, _switch(hass, attributes={"L1": 1, "L2": 1, "L3": 1}))

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_with_several_sites_there_is_no_question(hass: HomeAssistant) -> None:
    make_site_entry(hass, entry_id="site_a")
    make_site_entry(hass, entry_id="site_b")

    result = await _generic(hass, _switch(hass, attributes={"L1": 1, "L2": 1, "L3": 1}))

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_yes_adds_the_charger_and_its_wiring_to_the_site_once_it_exists(
    hass: HomeAssistant,
) -> None:
    site = make_site_entry(hass, entry_id="site", title="Home")
    before = dict(site.data)

    result = await _generic(hass, _switch(hass, attributes={"L1": 1, "L2": 1, "L3": 1}))
    assert result["step_id"] == "join_site"
    assert result["description_placeholders"]["site"] == "Home"
    assert "3 phases" in result["description_placeholders"]["summary"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    charger_id = result["result"].entry_id
    after = hass.config_entries.async_get_entry("site").data
    assert after["charger_entry_ids"] == [charger_id]
    wiring = after["phase_wiring"][charger_id]
    assert wiring["phases"] == 3 and wiring["measured_current_source"]["kind"] == "attributes"
    changed = {key for key in after if after[key] != before.get(key)}
    assert changed == {"charger_entry_ids", "phase_wiring"}


async def test_no_leaves_the_site_alone(hass: HomeAssistant) -> None:
    site = make_site_entry(hass, entry_id="site")
    before = dict(site.data)

    result = await _generic(hass, _switch(hass, attributes={"L1": 1, "L2": 1, "L3": 1}))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"join": False})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(hass.config_entries.async_get_entry("site").data) == before


async def test_ambiguous_wiring_offers_only_no_and_points_to_the_settings(hass: HomeAssistant) -> None:
    site = make_site_entry(hass, entry_id="site")
    before = dict(site.data)

    result = await _generic(hass, _switch(hass, attributes=None))
    assert result["step_id"] == "join_site"
    assert list(result["data_schema"].schema) == []
    assert "site's settings" in result["description_placeholders"]["summary"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(hass.config_entries.async_get_entry("site").data) == before
