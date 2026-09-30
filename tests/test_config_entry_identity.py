"""A charger and a site each have one stable identity, and the flow refuses to add either twice."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.spotnav.const import DOMAIN

from .helpers import make_entry


async def _add_generic_charger(hass: HomeAssistant, switch: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"entry_type": "charger"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"mode": "generic"})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"charge_control": switch}
    )


async def _add_site(hass: HomeAssistant, name: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry_type": "site"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": name,
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
        },
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"choice": "skip"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


async def test_a_charger_is_identified_by_its_charge_control_and_added_once(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.wallbox", "off")

    created = await _add_generic_charger(hass, "switch.wallbox")

    assert created["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(created["result"].entry_id)
    assert entry.unique_id == "charge_control:switch.wallbox"

    # The charge control is already used, which the form itself refuses before an entry is built.
    again = await _add_generic_charger(hass, "switch.wallbox")

    assert again["type"] is FlowResultType.FORM
    assert again["errors"] == {"charge_control": "charge_control_in_use"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_an_entry_already_holding_the_identity_aborts_the_flow(hass: HomeAssistant) -> None:
    """The identity is the second guard: it holds even when the entities differ."""
    hass.states.async_set("switch.wallbox", "off")
    make_entry(
        hass,
        entry_id="existing",
        charge_control="switch.somewhere_else",
        current_limit=None,
        webhook_id="webhook-existing",
        title="Existing",
        unique_id="charge_control:switch.wallbox",
    )

    result = await _add_generic_charger(hass, "switch.wallbox")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_two_different_chargers_are_both_added(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.wallbox_a", "off")
    hass.states.async_set("switch.wallbox_b", "off")

    first = await _add_generic_charger(hass, "switch.wallbox_a")
    second = await _add_generic_charger(hass, "switch.wallbox_b")

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.CREATE_ENTRY


async def test_a_site_keeps_its_identity_and_is_added_once(hass: HomeAssistant) -> None:
    created = await _add_site(hass, "Home")

    assert created["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(created["result"].entry_id)
    assert entry.unique_id == "site:home"

    again = await _add_site(hass, "Home")

    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"
