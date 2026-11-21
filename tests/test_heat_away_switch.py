"""The site's heat Away as a switch (`switch.py` `SiteHeatAwaySwitch`, decision D5 of 2026-11-20): on is away until it
is turned off, off is home; Away set by date in the card reads as on; the heat card's own settings write."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import CONF_HEAT, DOMAIN

from .test_demand_site import pilot_site
from .test_heat_api import update


#: A site with one floor-heating zone: heat is set up, so the site has its Away switch.
WITH_ZONE = {CONF_HEAT: {"zones": [{"entity_id": "climate.golvvarme", "offset_c": 0.0}]}}


def _entity_id(hass: HomeAssistant, site_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{site_id}_heat_away")
    assert entity_id is not None
    return entity_id


async def test_a_site_without_heat_has_no_away_switch_until_heat_is_set_up(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, f"{site.entry_id}_heat_away") is None
    # A heat write that sets nothing to steer adds nothing.
    result = await update(hass, api_version=1, site_id=site.entry_id, changes={"target_c": 20.5})
    assert result["result"]["ok"], result
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("switch", DOMAIN, f"{site.entry_id}_heat_away") is None

    hass.states.async_set("climate.golvvarme", "heat", {"current_temperature": 20.4, "temperature": 17})
    result = await update(
        hass, api_version=1, site_id=site.entry_id, changes={"zones": [{"entity_id": "climate.golvvarme", "offset_c": 0.0}]}
    )
    assert result["result"]["ok"], result
    await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, site.entry_id)).state == "off"  # type: ignore[union-attr]


async def test_the_away_switch_sets_and_ends_the_house_s_away(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, **WITH_ZONE)
    entity_id = _entity_id(hass, site.entry_id)
    assert entity_id.endswith("_heat_away")
    assert hass.states.get(entity_id).state == "off"  # type: ignore[union-attr]

    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    heat = site.data[CONF_HEAT]
    assert heat["mode"] == "away"
    until = datetime.fromisoformat(heat["away_until"])
    assert timedelta(days=89) < until - dt_util.utcnow() <= timedelta(days=90)
    assert hass.states.get(entity_id).state == "on"  # type: ignore[union-attr]

    await hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert site.data[CONF_HEAT]["mode"] == "home" and site.data[CONF_HEAT]["away_until"] is None
    assert hass.states.get(entity_id).state == "off"  # type: ignore[union-attr]


async def test_away_set_by_date_in_the_card_reads_as_on(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, **WITH_ZONE)
    entity_id = _entity_id(hass, site.entry_id)
    until = (dt_util.utcnow() + timedelta(days=2)).isoformat()
    result = await update(hass, api_version=1, site_id=site.entry_id, changes={"mode": "away", "away_until": until})
    assert result["result"]["ok"], result
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "on"  # type: ignore[union-attr]


async def test_turning_the_switch_on_over_away_by_date_makes_it_open_ended(hass: HomeAssistant, freezer) -> None:
    site = await pilot_site(hass, freezer, **WITH_ZONE)
    entity_id = _entity_id(hass, site.entry_id)
    until = (dt_util.utcnow() + timedelta(days=2)).isoformat()
    result = await update(hass, api_version=1, site_id=site.entry_id, changes={"mode": "away", "away_until": until})
    assert result["result"]["ok"], result
    await hass.async_block_till_done()

    await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    heat = site.data[CONF_HEAT]
    assert heat["mode"] == "away"
    assert timedelta(days=89) < datetime.fromisoformat(heat["away_until"]) - dt_util.utcnow() <= timedelta(days=90)
