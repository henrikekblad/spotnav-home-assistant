"""A charger whose SpotNav entry is deleted for good is handed back: allowed to charge, at the top of its
current range (`execution/charger_handback.py`). An unload or a reload hands nothing back, and a write that
fails is logged and does not hold the removal up."""

from __future__ import annotations

import logging

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CURRENT_CONTROL_NUMBER

from tests.helpers import make_entry

pytestmark = pytest.mark.usefixtures("offline_relay")

NUMBER = "number.charger_limit"
SWITCH = "switch.charger_a"


async def _charger(
    hass: HomeAssistant, *, switch: str = "off", amps: str = "10", maximum: float = 16, current: bool = True
):
    """A generic charger with a switch and (opted in) a current number, set up through Home Assistant, and
    the charger integration's services recorded from then on."""
    hass.states.async_set(SWITCH, switch)
    hass.states.async_set(NUMBER, amps, {"unit_of_measurement": "A", "min": 6, "max": maximum, "step": 1})
    entry = make_entry(
        hass,
        entry_id="entry_a",
        charge_control=SWITCH,
        current_limit=NUMBER,
        webhook_id="webhook-a",
        title="Garage",
        current_control=CURRENT_CONTROL_NUMBER if current else "",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # After setup: the core `switch` and `number` components register their own handlers while it runs.
    turn_on = async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "switch", "turn_off")
    set_value = async_mock_service(hass, "number", "set_value")
    return entry, turn_on, set_value


def _values(calls: list[ServiceCall]) -> list[float]:
    return [call.data["value"] for call in calls]


async def test_removing_the_charger_turns_it_back_on_at_its_full_current(hass: HomeAssistant) -> None:
    entry, turn_on, set_value = await _charger(hass)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in turn_on] == [SWITCH]
    assert _values(set_value) == [16]
    assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_a_charger_already_on_at_its_maximum_needs_nothing(hass: HomeAssistant) -> None:
    entry, turn_on, set_value = await _charger(hass, switch="on", amps="16")
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert turn_on == [] and set_value == []


async def test_a_current_spotnav_never_set_is_left_alone(hass: HomeAssistant) -> None:
    """Without the opt-in SpotNav never wrote the current, so there is nothing of ours to undo."""
    entry, turn_on, set_value = await _charger(hass, current=False)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert len(turn_on) == 1 and set_value == []


async def test_a_reload_or_an_unload_hands_nothing_back(hass: HomeAssistant) -> None:
    entry, turn_on, set_value = await _charger(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert turn_on == [] and set_value == []


async def test_a_failed_write_is_logged_and_the_removal_goes_on(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    entry, turn_on, _set_value = await _charger(hass)

    async def refuse(_call: ServiceCall) -> None:
        raise HomeAssistantError("the charger said no")

    hass.services.async_register("number", "set_value", refuse)
    with caplog.at_level(logging.WARNING):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert len(turn_on) == 1, "the start still went out"
    assert "current was not set back to 16 A" in caplog.text


async def test_a_flash_stored_current_is_written_once(hass: HomeAssistant) -> None:
    """A charger that keeps its current in flash (`WritePolicy.flash_stored`) takes the one hand-back write."""
    hass.states.async_set(SWITCH, "on")
    hass.states.async_set(NUMBER, "8", {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})
    entry = make_entry(
        hass,
        entry_id="entry_a",
        charge_control=SWITCH,
        current_limit=NUMBER,
        webhook_id="webhook-a",
        title="Alfen",
        current_control=CURRENT_CONTROL_NUMBER,
        extra={"charger_platform": "alfen_wallbox"},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    set_value = async_mock_service(hass, "number", "set_value")
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _values(set_value) == [32]
