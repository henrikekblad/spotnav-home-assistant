"""A charger's way back from the charge-ownership core: Configure offers "Charging decisions: Current (default) /
Previous logic", which writes `CONF_CORE_OWNERSHIP` false for the previous logic, removes it for the default, and
reloads the charger so the choice holds at once."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CORE_OWNERSHIP
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.flows.options import (
    apply_charging_decisions,
    CHARGING_DECISIONS,
    DECISIONS_CURRENT,
    DECISIONS_PREVIOUS,
)

from .test_entity_conflicts import _start_generic_flow, _submit_generic
from .world import controller_of

SWITCH = "switch.decisions"


def _shown(result: dict[str, Any]) -> str:
    """The choice the form shows."""
    for key in result["data_schema"].schema:
        if str(key) == CHARGING_DECISIONS:
            return key.default()
    raise AssertionError("the form has no charging decisions field")


async def _configure(hass: HomeAssistant, entry_id: str, choice: str | None) -> str:
    """Open Configure, note what it shows, and save it with `choice` (`None`: the field left as shown)."""
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "init"
    shown = _shown(result)
    user_input: dict[str, Any] = {CONF_CHARGE_CONTROL: SWITCH}
    if choice is not None:
        user_input[CHARGING_DECISIONS] = choice
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return shown


async def test_configure_switches_a_charger_to_the_previous_logic_and_back(hass: HomeAssistant) -> None:
    hass.states.async_set(SWITCH, "off")
    created = await _submit_generic(hass, await _start_generic_flow(hass), charge_control=SWITCH)
    entry = created["result"]
    await hass.async_block_till_done()
    assert CONF_CORE_OWNERSHIP not in entry.data, "a new charger says nothing: the default decides"
    default = ownership_shadow.CORE_OWNERSHIP_DEFAULT
    assert controller_of(hass, entry.entry_id).ownership_shadow.drives is default

    # Left as shown, nothing is written.
    assert await _configure(hass, entry.entry_id, None) == DECISIONS_CURRENT
    assert CONF_CORE_OWNERSHIP not in entry.data

    # The previous logic: stored false, the reloaded charger decides with it.
    assert await _configure(hass, entry.entry_id, DECISIONS_PREVIOUS) == DECISIONS_CURRENT
    assert entry.data[CONF_CORE_OWNERSHIP] is False
    controller = controller_of(hass, entry.entry_id)
    assert controller.ownership_shadow.drives is False
    assert controller.ownership_shadow.diagnostics()["drives"] is False

    # Shown as chosen, and kept by a save that leaves it as shown.
    assert await _configure(hass, entry.entry_id, None) == DECISIONS_PREVIOUS
    assert entry.data[CONF_CORE_OWNERSHIP] is False

    # Back to the default: the key is removed, the reloaded charger follows the default again.
    assert await _configure(hass, entry.entry_id, DECISIONS_CURRENT) == DECISIONS_PREVIOUS
    assert CONF_CORE_OWNERSHIP not in entry.data
    assert controller_of(hass, entry.entry_id).ownership_shadow.drives is default


def test_the_choice_writes_false_or_removes_the_key() -> None:
    data: dict[str, Any] = {}
    apply_charging_decisions(data, DECISIONS_CURRENT)
    assert data == {}
    apply_charging_decisions(data, DECISIONS_PREVIOUS)
    assert data == {CONF_CORE_OWNERSHIP: False}
    apply_charging_decisions(data, DECISIONS_CURRENT)
    assert data == {}
    data = {CONF_CORE_OWNERSHIP: True}
    apply_charging_decisions(data, DECISIONS_CURRENT)
    assert data == {CONF_CORE_OWNERSHIP: True}, "an entry set to drive keeps saying so"
    apply_charging_decisions(data, DECISIONS_PREVIOUS)
    assert data == {CONF_CORE_OWNERSHIP: False}
