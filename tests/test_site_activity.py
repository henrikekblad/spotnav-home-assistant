"""The healthy site state reads as what it is: measuring, with or without active load balancing.

`observing` stays the state's value (it is read by automations and diagnostics); what Home Assistant shows
for it is "Measuring", and an additive `site_activity` attribute says whether active load balancing is on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import DOMAIN
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.site.site_capacity import SITE_ACTIVITIES, site_activity

from .helpers import make_site_entry, set_current_sensor

TRANSLATIONS = Path(__file__).parent.parent / "custom_components" / DOMAIN / "translations"
SITE_STATES = (
    "disabled",
    "not_configured",
    "missing_measurements",
    "stale_measurements",
    "invalid_measurements",
    "observing",
)


@pytest.mark.parametrize(
    ("state", "active", "activity"),
    [
        ("observing", False, "measuring"),
        ("observing", True, "balancing"),
        ("stale_measurements", True, "not_measuring"),
        ("missing_measurements", False, "not_measuring"),
        ("disabled", False, "not_measuring"),
    ],
)
def test_the_activity_says_measuring_and_whether_load_balancing_is_on(state: str, active: bool, activity: str) -> None:
    assert site_activity(state, active) == activity
    assert activity in SITE_ACTIVITIES


async def _site(hass: HomeAssistant, entry_id: str, *, active: bool):
    entry = make_site_entry(hass, entry_id=entry_id, charger_entry_ids=[], active_control_enabled=active)
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.{entry_id}_{phase}", 5)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry_id}_site_state")
    return entry, hass.states.get(entity_id)


async def test_the_site_sensor_and_diagnostics_carry_the_activity(hass: HomeAssistant) -> None:
    entry, state = await _site(hass, "site_off", active=False)
    assert state.state == "observing"
    assert state.attributes["site_activity"] == "measuring"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["result"]["state"] == "observing"
    assert diagnostics["site_activity"] == "measuring"


async def test_with_active_load_balancing_on_it_says_so(hass: HomeAssistant) -> None:
    _entry, state = await _site(hass, "site_on", active=True)
    assert state.state == "observing"
    assert state.attributes["site_activity"] == "balancing"


def test_every_language_names_the_states_and_the_activity() -> None:
    names = {}
    for path in sorted(TRANSLATIONS.glob("*.json")):
        sensor = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
        site_state = sensor["site_state"]
        assert set(site_state["state"]) == set(SITE_STATES), path.name
        activity = site_state["state_attributes"]["site_activity"]
        assert activity["name"], path.name
        assert set(activity["state"]) == set(SITE_ACTIVITIES), path.name
        names[path.stem] = site_state["state"]["observing"]
    assert names["en"] == "Measuring"
    assert names["sv"] == "Mäter"


def test_every_language_names_a_chargers_allocation_including_not_requesting() -> None:
    from typing import get_args

    from custom_components.spotnav.site.site_capacity import ChargerState

    names = {}
    for path in sorted(TRANSLATIONS.glob("*.json")):
        proposed = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]["charger_proposed_current"]
        allocation = proposed["state_attributes"]["state"]
        assert allocation["name"], path.name
        assert set(allocation["state"]) == set(get_args(ChargerState)), path.name
        names[path.stem] = allocation["state"]["not_requesting"]
    assert names["en"] == "Not charging"
    assert names["sv"] == "Laddar inte"
