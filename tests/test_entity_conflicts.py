"""Tests for the config-flow/options-flow duplicate-target guard.

Two independent SpotNav config entries controlling the same charge-control
switch (or the same current-limit number) could issue contradictory
start/stop/schedule/current commands to one physical charger, so creating or
editing a config entry must be blocked from introducing that collision. This
guard only runs during config flow / options flow validation — entries saved
before it existed must keep loading normally at Home Assistant startup.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_GENERIC,
)

from pytest_homeassistant_custom_component.common import async_mock_service

from .helpers import create_ocpp_switch_and_number, make_entry, make_ocpp_config_entry
from .world import controller_of


async def _start_generic_flow(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER}
    )
    assert result["step_id"] == "charger_type"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_GENERIC}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "generic"
    return result["flow_id"]


async def _submit_generic(hass: HomeAssistant, flow_id: str, *, charge_control: str, current_limit: str | None = None):
    payload = {CONF_CHARGE_CONTROL: charge_control}
    if current_limit is not None:
        payload[CONF_CURRENT_LIMIT] = current_limit
    return await hass.config_entries.flow.async_configure(flow_id, payload)


async def _start_ocpp_flow(hass: HomeAssistant, device_id: str) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER}
    )
    assert result["step_id"] == "charger_type"
    # The charge point answers, without an AssignedCurrent entry: nothing is guessed, the form shows.
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": None})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODE: "detected"})
    assert result["step_id"] == "detect_device"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"device": device_id})
    assert result["step_id"] == "ocpp_entities"
    return result["flow_id"]


async def _submit_ocpp(hass: HomeAssistant, flow_id: str, *, charge_control: str, current_limit: str | None = None):
    payload = {CONF_CHARGE_CONTROL: charge_control}
    if current_limit is not None:
        payload[CONF_CURRENT_LIMIT] = current_limit
    return await hass.config_entries.flow.async_configure(flow_id, payload)


def _device_id_for(hass: HomeAssistant, entity_id: str) -> str:
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None and entry.device_id is not None
    return entry.device_id


# --- Case 1: two Generic entries, same charge-control --------------------


async def test_two_generic_entries_with_same_charge_control_are_blocked(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.shared", "off")

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(hass, flow_id, charge_control="switch.shared")
    assert result["type"] == FlowResultType.CREATE_ENTRY

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(hass, flow_id, charge_control="switch.shared")

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "generic"  # Case 9: error stays on the right step
    assert result["errors"] == {"charge_control": "charge_control_in_use"}


# --- Case 2: OCPP-guided entry blocked by an existing charge-control ------


async def test_ocpp_entry_blocked_if_charge_control_already_used(
    hass: HomeAssistant,
) -> None:
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_conflict_2")
    switch_id, _ = create_ocpp_switch_and_number(
        hass, ocpp_entry=ocpp_entry, device_unique_id="device_2", switch_object_id="charger_2"
    )
    device_id = _device_id_for(hass, switch_id)

    # First OCPP entry claims the switch.
    flow_id = await _start_ocpp_flow(hass, device_id)
    result = await _submit_ocpp(hass, flow_id, charge_control=switch_id)
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # A second OCPP flow over the very same device/switch must be blocked.
    flow_id = await _start_ocpp_flow(hass, device_id)
    result = await _submit_ocpp(hass, flow_id, charge_control=switch_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "ocpp_entities"
    assert result["errors"] == {"charge_control": "charge_control_in_use"}


# --- Case 3: Generic and OCPP must not bypass each other's check ---------


async def test_generic_entry_blocks_a_later_ocpp_entry_on_the_same_entity(
    hass: HomeAssistant,
) -> None:
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_cross_1")
    switch_id, _ = create_ocpp_switch_and_number(
        hass, ocpp_entry=ocpp_entry, device_unique_id="device_cross_1", switch_object_id="charger_cross_1"
    )
    device_id = _device_id_for(hass, switch_id)

    # A Generic entry claims the same entity_id an OCPP device also exposes.
    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(hass, flow_id, charge_control=switch_id)
    assert result["type"] == FlowResultType.CREATE_ENTRY

    flow_id = await _start_ocpp_flow(hass, device_id)
    result = await _submit_ocpp(hass, flow_id, charge_control=switch_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "ocpp_entities"
    assert result["errors"] == {"charge_control": "charge_control_in_use"}


async def test_ocpp_entry_blocks_a_later_generic_entry_on_the_same_entity(
    hass: HomeAssistant,
) -> None:
    ocpp_entry = make_ocpp_config_entry(hass, entry_id="ocpp_cross_2")
    switch_id, _ = create_ocpp_switch_and_number(
        hass, ocpp_entry=ocpp_entry, device_unique_id="device_cross_2", switch_object_id="charger_cross_2"
    )
    device_id = _device_id_for(hass, switch_id)

    flow_id = await _start_ocpp_flow(hass, device_id)
    result = await _submit_ocpp(hass, flow_id, charge_control=switch_id)
    assert result["type"] == FlowResultType.CREATE_ENTRY

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(hass, flow_id, charge_control=switch_id)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "generic"
    assert result["errors"] == {"charge_control": "charge_control_in_use"}


# --- Case 4: different charge-control and different current-limit --------


async def test_different_charge_control_and_current_limit_are_accepted(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.accept_a", "off")
    hass.states.async_set("switch.accept_b", "off")
    hass.states.async_set("number.accept_limit_a", "16", {"unit_of_measurement": "A"})
    hass.states.async_set("number.accept_limit_b", "16", {"unit_of_measurement": "A"})

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(
        hass, flow_id, charge_control="switch.accept_a", current_limit="number.accept_limit_a"
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(
        hass, flow_id, charge_control="switch.accept_b", current_limit="number.accept_limit_b"
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


# --- Case 5: same current-limit, different charge-control -----------------


async def test_same_current_limit_with_different_charge_control_is_blocked(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.limit_conflict_a", "off")
    hass.states.async_set("switch.limit_conflict_b", "off")
    hass.states.async_set("number.shared_limit", "16", {"unit_of_measurement": "A"})

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(
        hass, flow_id, charge_control="switch.limit_conflict_a", current_limit="number.shared_limit"
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(
        hass, flow_id, charge_control="switch.limit_conflict_b", current_limit="number.shared_limit"
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "generic"
    assert result["errors"] == {"current_limit": "current_limit_in_use"}


# --- Case 6: empty current-limit on multiple entries is accepted ---------


async def test_empty_current_limit_on_multiple_entries_is_accepted(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.no_limit_a", "off")
    hass.states.async_set("switch.no_limit_b", "off")

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(hass, flow_id, charge_control="switch.no_limit_a")
    assert result["type"] == FlowResultType.CREATE_ENTRY

    flow_id = await _start_generic_flow(hass)
    result = await _submit_generic(hass, flow_id, charge_control="switch.no_limit_b")
    assert result["type"] == FlowResultType.CREATE_ENTRY


# --- Case 7: options flow never compares an entry against itself ---------


async def test_options_flow_reconfigure_does_not_conflict_with_itself(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.self_reconfigure", "off")
    hass.states.async_set("number.self_reconfigure_limit", "16", {"unit_of_measurement": "A"})

    flow_id = await _start_generic_flow(hass)
    create_result = await _submit_generic(
        hass, flow_id, charge_control="switch.self_reconfigure", current_limit="number.self_reconfigure_limit"
    )
    assert create_result["type"] == FlowResultType.CREATE_ENTRY
    entry = create_result["result"]
    # A config entry created by completing a UI flow is set up automatically
    # by the config entries manager; unlike MockConfigEntry.add_to_hass(), no
    # separate async_setup() call is needed (or allowed) here.
    await hass.async_block_till_done()

    options_result = await hass.config_entries.options.async_init(entry.entry_id)
    assert options_result["type"] == FlowResultType.FORM
    assert options_result["step_id"] == "init"

    # Re-submitting the exact same, already-owned entities must not conflict
    # with the entry's own prior configuration.
    save_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        {
            CONF_CHARGE_CONTROL: "switch.self_reconfigure",
            CONF_CURRENT_LIMIT: "number.self_reconfigure_limit",
        },
    )
    assert save_result["type"] == FlowResultType.CREATE_ENTRY


async def test_options_flow_still_blocks_a_conflict_with_a_different_entry(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("switch.reconfigure_owned", "off")
    hass.states.async_set("switch.reconfigure_other", "off")

    flow_id = await _start_generic_flow(hass)
    result_owned = await _submit_generic(hass, flow_id, charge_control="switch.reconfigure_owned")
    assert result_owned["type"] == FlowResultType.CREATE_ENTRY
    owned_entry = result_owned["result"]

    flow_id = await _start_generic_flow(hass)
    result_other = await _submit_generic(hass, flow_id, charge_control="switch.reconfigure_other")
    assert result_other["type"] == FlowResultType.CREATE_ENTRY

    # Both entries were already set up automatically when their flows
    # completed (see the note in the previous test).
    await hass.async_block_till_done()

    options_result = await hass.config_entries.options.async_init(owned_entry.entry_id)
    conflict_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"], {CONF_CHARGE_CONTROL: "switch.reconfigure_other"}
    )

    assert conflict_result["type"] == FlowResultType.FORM
    assert conflict_result["step_id"] == "init"
    assert conflict_result["errors"] == {"charge_control": "charge_control_in_use"}


async def test_options_flow_can_clear_current_limit_and_reloads_controller(
    hass: HomeAssistant,
) -> None:
    """Saving Options must immediately apply new entities and allow clearing a limit."""
    hass.states.async_set("switch.options_old", "off")
    hass.states.async_set("switch.options_new", "off")
    hass.states.async_set("number.options_limit", "16", {"unit_of_measurement": "A"})

    flow_id = await _start_generic_flow(hass)
    create_result = await _submit_generic(
        hass,
        flow_id,
        charge_control="switch.options_old",
        current_limit="number.options_limit",
    )
    assert create_result["type"] == FlowResultType.CREATE_ENTRY
    entry = create_result["result"]
    await hass.async_block_till_done()

    options_result = await hass.config_entries.options.async_init(entry.entry_id)
    save_result = await hass.config_entries.options.async_configure(
        options_result["flow_id"],
        {CONF_CHARGE_CONTROL: "switch.options_new"},
    )
    assert save_result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.data[CONF_CHARGE_CONTROL] == "switch.options_new"
    assert entry.data[CONF_CURRENT_LIMIT] == ""
    controller = controller_of(hass, entry.entry_id)
    assert controller.charge_control == "switch.options_new"
    assert controller.current_limit is None


# --- Case 8: entries saved before this guard existed still start up ------


async def test_already_saved_duplicate_entries_still_set_up_at_startup(
    hass: HomeAssistant,
) -> None:
    """The guard only runs in flow steps, never in async_setup_entry, so two saved entries that
    share a charge-control must both keep loading normally.
    """
    hass.states.async_set("switch.legacy_duplicate", "off")

    entry_a = make_entry(
        hass,
        entry_id="legacy_dup_a",
        charge_control="switch.legacy_duplicate",
        current_limit=None,
        webhook_id="webhook-legacy-a",
        title="Legacy A",
    )
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    # entry_b is added only after entry_a finishes setup: the first entry set
    # up for a domain also bootstraps the component, which sets up every
    # entry already registered for that domain in one call (see the same
    # note in tests/helpers.py's setup_two_chargers). Adding entry_b
    # afterwards keeps this test to one config_entries.async_setup() call per
    # entry, matching the other startup-path tests in this suite.
    entry_b = make_entry(
        hass,
        entry_id="legacy_dup_b",
        charge_control="switch.legacy_duplicate",
        current_limit=None,
        webhook_id="webhook-legacy-b",
        title="Legacy B",
    )
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    assert entry_a.state.value == "loaded"
    assert entry_b.state.value == "loaded"
