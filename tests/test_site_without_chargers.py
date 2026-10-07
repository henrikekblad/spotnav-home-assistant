"""A site with no charger says how to add one: in its setup step, in Repairs and on its own device page."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.spotnav.const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from custom_components.spotnav.repairs import async_sync_resolution_repairs
from custom_components.spotnav.setup_hints import add_charger_hint

from .helpers import make_site_entry
from .world import setup_charger, setup_site

HINT = "Add a charger: Settings → Devices & services → SpotNav → Add entry → Charger; it will offer to join this site."


def _issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {key[1]: issue for key, issue in ir.async_get(hass).issues.items() if key[0] == DOMAIN}


async def _site_step(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_SITE})


def test_the_hint_is_the_sentence_the_pages_say() -> None:
    assert add_charger_hint("en") == HINT
    assert add_charger_hint("sv").startswith("Lägg till en laddare: Inställningar → Enheter och tjänster → SpotNav")


async def test_the_site_step_says_how_to_add_a_charger_only_while_there_is_none(hass: HomeAssistant) -> None:
    result = await _site_step(hass)
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "site"
    assert HINT in result["description_placeholders"]["charger_hint"]
    hass.config_entries.flow.async_abort(result["flow_id"])

    await setup_charger(hass)
    again = await _site_step(hass)
    assert again["description_placeholders"]["charger_hint"] == ""


async def test_a_site_without_a_charger_has_a_repair_and_a_hint_on_its_device(hass: HomeAssistant) -> None:
    site = await setup_site(hass, entry_id="site_empty", title="Home")
    await async_sync_resolution_repairs(hass)
    issue = _issues(hass)[f"site_without_chargers_{site.entry_id}"]
    assert (issue.translation_key, issue.is_fixable) == ("site_without_chargers", False)
    assert issue.translation_placeholders == {"site": "Home"}

    state_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{site.entry_id}_site_state")
    assert hass.states.get(state_id).attributes["next_step"] == HINT


async def test_the_repair_and_the_hint_go_when_the_site_has_a_charger(hass: HomeAssistant) -> None:
    charger = await setup_charger(hass, entry_id="entry_a")
    site = make_site_entry(hass, entry_id="site_full", charger_entry_ids=[charger.entry_id])
    assert await hass.config_entries.async_setup(site.entry_id)
    await hass.async_block_till_done()
    assert [key for key in _issues(hass) if key.startswith("site_without_chargers")] == []
    state_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{site.entry_id}_site_state")
    assert "next_step" not in hass.states.get(state_id).attributes


async def test_the_confirm_step_says_how_to_add_a_charger_only_while_there_is_none(hass: HomeAssistant) -> None:
    from .test_config_flow_site_confirm import _choice, _owner_world, _to_detected

    _owner_world(hass, chargers=0)
    detected = await _to_detected(hass, [])
    confirm = await hass.config_entries.flow.async_configure(
        detected["flow_id"], {"choice": _choice(detected), "enable_disabled": True}
    )
    assert confirm["step_id"] == "site_confirm"
    assert HINT in confirm["description_placeholders"]["charger_hint"]
    hass.config_entries.flow.async_abort(confirm["flow_id"])
