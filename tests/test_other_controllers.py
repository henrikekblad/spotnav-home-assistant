"""Other integrations that switch or limit a charger by themselves (EV Smart Charging, EVSE Load
Balancer, PeaqEV): detected per charger where their setting names it, else per installation, and warned
about in the charger flow and in the entity configuration's `control.conflicts`.

The settings are the ones the integrations' own sources read (`execution/other_controllers.py`).
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api.entity_fields import charger_control_descriptor
from custom_components.spotnav.execution.other_controllers import (
    other_controllers,
    OtherController,
    SCOPE_CHARGER,
    SCOPE_INSTALLATION,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from .charger_shapes import register_shape, SHAPES
from .test_charger_config_flow import _to_detected_entities
from .world import setup_charger

CONTROL = "switch.wallbox_pause_resume"


def _evsc(hass: HomeAssistant, *, data=None, options=None, entry_id="evsc") -> MockConfigEntry:
    entry = MockConfigEntry(domain="ev_smart_charging", entry_id=entry_id, data=data or {}, options=options or {})
    entry.add_to_hass(hass)
    return entry


def _device_of(hass: HomeAssistant, entity_id: str) -> str:
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None and registered.device_id is not None
    return registered.device_id


async def test_ev_smart_charging_is_reported_for_the_charger_its_charger_entity_names(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])
    _evsc(hass, data={"charger_entity": CONTROL})

    found = other_controllers(hass, charge_control=CONTROL, device_id=_device_of(hass, CONTROL))

    assert found == [OtherController("ev_smart_charging", "EV Smart Charging", SCOPE_CHARGER)]


async def test_its_options_win_over_its_data_as_the_integration_reads_them(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])
    _evsc(hass, data={"charger_entity": CONTROL}, options={"charger_entity": "switch.some_other_charger"})

    assert other_controllers(hass, charge_control=CONTROL, device_id=_device_of(hass, CONTROL)) == []


async def test_a_switch_on_the_same_device_names_the_same_charger(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])
    registry = er.async_get(hass)
    device_id = _device_of(hass, CONTROL)
    sibling = registry.async_get_or_create("switch", "wallbox", "sibling", device_id=device_id, suggested_object_id="wallbox_sibling")
    _evsc(hass, data={"charger_entity": sibling.entity_id})

    assert [c.scope for c in other_controllers(hass, charge_control=CONTROL, device_id=device_id)] == [SCOPE_CHARGER]


@pytest.mark.parametrize("named", ["", "  ", "switch.another_charger"])
async def test_a_charger_entity_that_is_blank_or_another_charger_is_not_this_chargers_problem(
    hass: HomeAssistant, named: str
) -> None:
    register_shape(hass, SHAPES["wallbox"])
    _evsc(hass, data={"charger_entity": named})

    assert other_controllers(hass, charge_control=CONTROL, device_id=_device_of(hass, CONTROL)) == []


async def test_a_switched_off_ev_smart_charging_is_left_alone(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])
    entry = _evsc(hass, data={"charger_entity": CONTROL})
    switch = er.async_get(hass).async_get_or_create(
        "switch", "ev_smart_charging", f"{entry.entry_id}.switch.smartchargingactivated", suggested_object_id="smart_charging_activated"
    )
    for state, expected in (("on", 1), ("unavailable", 1), ("off", 0)):
        hass.states.async_set(switch.entity_id, state)
        assert len(other_controllers(hass, charge_control=CONTROL, device_id=_device_of(hass, CONTROL))) == expected, state


async def test_evse_load_balancer_is_reported_for_the_device_it_balances(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])
    device_id = _device_of(hass, CONTROL)
    MockConfigEntry(domain="evse_load_balancer", data={"charger_device": device_id}).add_to_hass(hass)
    MockConfigEntry(domain="evse_load_balancer", entry_id="other", data={"charger_device": "not-this-one"}).add_to_hass(hass)

    found = other_controllers(hass, charge_control=CONTROL, device_id=device_id)

    assert found == [OtherController("evse_load_balancer", "EVSE Load Balancer", SCOPE_CHARGER)]
    assert other_controllers(hass, charge_control="switch.x", device_id="elsewhere") == []


async def test_peaqev_cannot_name_a_charger_so_it_is_reported_for_the_installation(hass: HomeAssistant) -> None:
    MockConfigEntry(domain="peaqev", data={"chargertype": "Easee", "chargerid": "EH123456"}).add_to_hass(hass)

    assert other_controllers(hass, charge_control=None, device_id=None) == [
        OtherController("peaqev", "PeaqEV", SCOPE_INSTALLATION)
    ]


@pytest.mark.parametrize("domain", ["nordpool_planner", "peaqnext"])
async def test_integrations_that_only_publish_sensors_are_not_controllers(hass: HomeAssistant, domain: str) -> None:
    MockConfigEntry(domain=domain).add_to_hass(hass)

    assert other_controllers(hass, charge_control=CONTROL, device_id=None) == []


async def test_a_disabled_controller_entry_is_not_reported(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="peaqev", disabled_by=ConfigEntryDisabler.USER)
    entry.add_to_hass(hass)

    assert other_controllers(hass, charge_control=None, device_id=None) == []


async def test_detection_carries_the_controllers_and_the_flow_words_the_warning(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    _evsc(hass, data={"charger_entity": CONTROL})
    MockConfigEntry(domain="peaqev").add_to_hass(hass)

    found = detect_charger(hass, ids["device_id"])
    assert [(c.name, c.scope) for c in found.controllers] == [
        ("EV Smart Charging", SCOPE_CHARGER),
        ("PeaqEV", SCOPE_INSTALLATION),
    ]

    result = await _to_detected_entities(hass, ids["device_id"])
    warning = result["description_placeholders"]["warning"]
    assert "EV Smart Charging also controls chargers; turn it off for this charger or SpotNav and EV Smart Charging will fight." in warning
    assert "PeaqEV also controls chargers; turn it off for this charger or SpotNav and PeaqEV will fight." in warning
    # Unlike evcc and openWB, it does not take the suggestions away.
    assert (result["data_schema"].schema and any((key.description or {}).get("suggested_value") for key in result["data_schema"].schema))


async def test_without_a_controller_the_flow_has_no_controller_warning(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    _evsc(hass, data={"charger_entity": "switch.another_charger"})

    result = await _to_detected_entities(hass, ids["device_id"])

    assert "also controls chargers" not in result["description_placeholders"]["warning"]


async def test_the_entity_configuration_lists_the_controller_as_a_conflict(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])
    entry = await setup_charger(hass, charge_control=CONTROL)
    assert charger_control_descriptor(hass, entry)["conflicts"] == []
    _evsc(hass, data={"charger_entity": CONTROL})
    MockConfigEntry(domain="peaqev").add_to_hass(hass)

    conflicts = charger_control_descriptor(hass, entry)["conflicts"]

    assert [item for item in conflicts if item["kind"] == "other_controller"] == [
        {"kind": "other_controller", "entity_id": CONTROL, "label": "EV Smart Charging", "state": "charger"},
        {"kind": "other_controller", "entity_id": CONTROL, "label": "PeaqEV", "state": "installation"},
    ]


async def test_an_easee_charger_is_told_about_cloud_smart_charging_and_others_are_not(hass: HomeAssistant) -> None:
    easee = register_shape(hass, SHAPES["easee"])
    result = await _to_detected_entities(hass, easee["device_id"])
    assert "linked to Tibber (or another app) for smart charging, turn that off" in result["description_placeholders"]["warning"]


async def test_a_charger_without_the_easee_cloud_has_no_cloud_note(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    result = await _to_detected_entities(hass, ids["device_id"])
    assert "Tibber" not in result["description_placeholders"]["warning"]
