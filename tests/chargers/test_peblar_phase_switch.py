"""Peblar's phase switch as an optional adapter capability (`PhaseSwitch`), from the core integration's real shape.

Home Assistant's `peblar` integration creates `switch.<name>_force_single_phase` (key and translation key
`force_single_phase`, unique id `<serial>_force_single_phase`, entity category config) only when the charger allows
forcing one phase and has more than one phase connected. On means one phase, off means three. SpotNav finds it on
the charger's own device through the entity registry, never by its entity id; without it there is no capability.
"""

from __future__ import annotations

import json
from dataclasses import replace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service, MockConfigEntry

from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.execution.chargers.base import (
    PHASES_INVALID,
    PHASES_TARGET_UNAVAILABLE,
    PHASES_UNSUPPORTED,
    PhaseSwitchResult,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, Clock
from ..charger_shapes import _e, register_shape, Shape, SHAPES
from ..test_charger_config_flow import _loaded_detected_charger, async_get_entity_config

FORCE = "switch.peblar_force_single_phase"


def _with_force_single_phase(state: str = "off") -> Shape:
    """The recorded Peblar shape on a charger that allows one phase and has three connected."""
    shape = SHAPES["peblar"]
    force = _e("switch", f"{shape.serial}_force_single_phase", "force_single_phase", state, translation_key=True)
    return replace(shape, entities=(*shape.entities, force))


def _adapter(hass: HomeAssistant, shape: Shape):
    ids = register_shape(hass, shape)
    return adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())


async def test_the_switch_is_found_on_the_chargers_device(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _with_force_single_phase())

    switch = adapter.phase_switch

    assert switch is not None and switch.entity_id == FORCE
    entry = er.async_get(hass).async_get(FORCE)
    assert entry.unique_id == "PB777_force_single_phase" and entry.translation_key == "force_single_phase"


async def test_no_switch_no_capability(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, SHAPES["peblar"])

    assert adapter.phase_switch is None
    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_UNSUPPORTED)
    assert adapter.command_log() == []


async def test_it_is_found_by_what_it_is_not_by_its_name(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _with_force_single_phase())
    registry = er.async_get(hass)
    registry.async_update_entity(FORCE, new_entity_id="switch.garage_one_phase")

    assert adapter.phase_switch is not None and adapter.phase_switch.entity_id == "switch.garage_one_phase"


async def test_an_entity_named_like_it_elsewhere_is_not_it(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, SHAPES["peblar"])
    other = MockConfigEntry(domain="template", entry_id="template")
    other.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "switch", "template", "anything", suggested_object_id="peblar_force_single_phase", config_entry=other
    )
    hass.states.async_set(FORCE, "on")

    assert adapter.phase_switch is None


async def test_a_disabled_switch_is_no_capability(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _with_force_single_phase())
    er.async_get(hass).async_update_entity(FORCE, disabled_by=er.RegistryEntryDisabler.USER)

    assert adapter.phase_switch is None


async def test_another_platform_has_no_phase_switch(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, SHAPES["wallbox"])

    assert adapter.phase_switch is None


async def test_the_read_back_follows_the_switch(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _with_force_single_phase())
    switch = adapter.phase_switch
    assert switch is not None

    hass.states.async_set(FORCE, "on")
    assert switch.phases_now() == 1
    hass.states.async_set(FORCE, "off")
    assert switch.phases_now() == 3
    hass.states.async_set(FORCE, "unavailable")
    assert switch.phases_now() is None
    hass.states.async_set(FORCE, "unknown")
    assert switch.phases_now() is None


async def test_a_set_turns_the_switch_on_for_one_phase_and_off_for_three(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _with_force_single_phase())
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(True)
    assert await adapter.async_set_phases(3) == PhaseSwitchResult(True)

    assert [call.data["entity_id"] for call in on] == [FORCE]
    assert [call.data["entity_id"] for call in off] == [FORCE]
    log = adapter.command_log()
    assert [(record["kind"], record["result"]) for record in log] == [("phases", "sent"), ("phases", "sent")]
    assert [record["calls"][0]["service"] for record in log] == ["switch.turn_on", "switch.turn_off"]


async def test_an_unavailable_switch_is_not_executed(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _with_force_single_phase())
    on = async_mock_service(hass, "switch", "turn_on")
    hass.states.async_set(FORCE, "unavailable")

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)

    assert on == []
    record = adapter.command_log()[-1]
    assert (record["kind"], record["result"], record["outcome"]) == ("phases", "not_executed", PHASES_TARGET_UNAVAILABLE)


async def test_neither_one_nor_three_is_refused(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _with_force_single_phase())
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")

    assert await adapter.async_set_phases(2) == PhaseSwitchResult(False, PHASES_INVALID)  # type: ignore[arg-type]

    assert on == [] and off == []


async def test_the_diagnostics_show_the_phase_switch(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "peblar", shape=_with_force_single_phase("on"))

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    block = diagnostics["controller"]["adapter"]
    assert block["phase_switch"] == {"supported": True, "entity": FORCE, "phases_now": 1}
    # The capability stays out of what the card reads (the card rejects unknown keys).
    assert "phase_switch" not in json.dumps(await async_get_entity_config(hass, entry.entry_id))


async def test_the_diagnostics_say_so_without_one(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "peblar")

    block = (await async_get_config_entry_diagnostics(hass, entry))["controller"]["adapter"]

    assert block["phase_switch"] == {"supported": False, "entity": None, "phases_now": None}
