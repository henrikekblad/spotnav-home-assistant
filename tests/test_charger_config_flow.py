"""Adding a charger from its device: detection in the config flow, the options flow and the entity
configuration surface that shows the control path and its write policy.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.api.dashboard import capabilities_for
from custom_components.spotnav.api.entity_config import (
    async_get_entity_config,
    async_update_entity_config,
    EntityConfigRefusal,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CHARGER_CURRENT_ENTITIES,
    CONF_CHARGER_PLATFORM,
    CONF_CHARGING_STATE,
    CONF_CONTROL_PATH,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_ENERGY_REGISTER_ENTITY,
    CONF_ENERGY_REGISTER_IS_SESSION,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    DOMAIN,
    ENTRY_TYPE_CHARGER,
    MODE_DETECTED,
)
from custom_components.spotnav.diagnostics import async_get_config_entry_diagnostics
from custom_components.spotnav.runtime import controller_for

from .charger_helpers import detected_config
from .charger_shapes import E, ENERGY_KWH, NUMBER_A, register_shape, Shape, SHAPES
from .messages import register


async def _to_detected_entities(
    hass: HomeAssistant, device_id: str, *, unlisted: bool = False
) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER})
    assert result["step_id"] == "charger_type"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MODE: MODE_DETECTED})
    assert result["step_id"] == "detect_device"
    if unlisted:
        # A device the dropdown never offers: the step's own handling of it is what is under test.
        flow = hass.config_entries.flow._progress[result["flow_id"]]
        return await flow.async_step_detect_device({"device": device_id})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"device": device_id})


def _suggested(result: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }


async def test_a_charger_is_found_from_its_device_and_confirmed(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["peblar"])

    result = await _to_detected_entities(hass, ids["device_id"])

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "detected_entities"
    suggested = _suggested(result)
    assert suggested[CONF_CHARGE_CONTROL] == "switch.peblar_charge"
    assert suggested[CONF_CURRENT_LIMIT] == "number.peblar_charge_current_limit"
    assert suggested[CONF_CURRENT_CONTROL] == "number"
    assert suggested[CONF_ENERGY_REGISTER_ENTITY] == "sensor.peblar_energy_total"
    assert suggested["charging_state_entity"] == "sensor.peblar_cp_state"
    assert "enable_disabled_entities" in suggested

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CHARGE_CONTROL: "switch.peblar_charge",
            CONF_CURRENT_LIMIT: "number.peblar_charge_current_limit",
            CONF_CURRENT_CONTROL: "number",
            CONF_ENERGY_REGISTER_ENTITY: "sensor.peblar_energy_total",
            "charging_state_entity": "sensor.peblar_cp_state",
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    data = result["data"]
    expected = detected_config(detect_charger(hass, ids["device_id"]))
    for key, value in expected.items():
        assert data[key] == value, key
    assert data[CONF_ENTRY_TYPE] == ENTRY_TYPE_CHARGER and data[CONF_MODE] == MODE_DETECTED
    assert data[CONF_CHARGER_PLATFORM] == "peblar"
    assert data[CONF_CONTROL_PATH] == {
        "kind": "switch_budget",
        "entity_id": "switch.peblar_charge",
        "inverted": False,
    }
    assert data[CONF_CHARGING_STATE] == {"entity_id": "sensor.peblar_cp_state", "charging_values": ["charging"]}
    assert len(data[CONF_CHARGER_CURRENT_ENTITIES]) == 3
    assert data[CONF_ENERGY_REGISTER_IS_SESSION] is False
    # The useful entities that were disabled by default were enabled (the box was left ticked).
    registry = er.async_get(hass)
    assert registry.async_get("sensor.peblar_current_phase_1").disabled_by is None
    assert result["title"] == "peblar charger"


async def test_nothing_is_applied_until_the_person_opts_in_to_the_current(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    result = await _to_detected_entities(hass, ids["device_id"])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHARGE_CONTROL: "switch.wallbox_pause_resume", CONF_CURRENT_LIMIT: "number.wallbox_maximum_charging_current"},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CURRENT_CONTROL] == ""


async def test_a_per_session_energy_register_is_used_only_when_flagged(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    result = await _to_detected_entities(hass, ids["device_id"])
    schema_keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_ENERGY_REGISTER_IS_SESSION in schema_keys
    assert _suggested(result)[CONF_ENERGY_REGISTER_ENTITY] is None

    flagged = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHARGE_CONTROL: "switch.wallbox_pause_resume", CONF_ENERGY_REGISTER_IS_SESSION: True},
    )

    assert flagged["data"][CONF_ENERGY_REGISTER_ENTITY] == "sensor.wallbox_added_energy"
    assert flagged["data"][CONF_ENERGY_REGISTER_IS_SESSION] is True


async def test_the_chargers_own_mode_is_asked_to_be_turned_off_and_rechecked(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["goecharger_api2"])
    hass.states.async_set("select.goecharger_api2_lmo", "4", {"options": ["3", "4", "5"]})

    result = await _to_detected_entities(hass, ids["device_id"])
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "own_mode"
    assert "charging mode" in result["description_placeholders"]["modes"]

    # Still on: refused.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "own_mode" and result["errors"] == {"base": "own_mode_active"}

    # Turned off in the meantime: the step passes on its own.
    hass.states.async_set("select.goecharger_api2_lmo", "3", {"options": ["3", "4", "5"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "detected_entities"


async def test_one_can_continue_with_the_own_mode_on_knowingly(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["openevse"])
    result = await _to_detected_entities(hass, ids["device_id"])
    assert result["step_id"] == "own_mode"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"continue_anyway": True})

    assert result["step_id"] == "detected_entities"


async def test_a_heater_outlet_is_not_a_charger(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])  # the selector only exists when a supported device does
    ids = register_shape(hass, Shape("webel_gctrl", "W1", (E("switch", "W1_outlet", "outlet", "off"),), {}))

    result = await _to_detected_entities(hass, ids["device_id"], unlisted=True)

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "not_a_charger"


async def test_a_read_only_charger_cannot_be_added_as_a_controllable_one(hass: HomeAssistant) -> None:
    ids = register_shape(
        hass, Shape("tesla_wall_connector", "T1", (E("sensor", "T1_energy_kwh", "energy_kwh", "1", ENERGY_KWH),), {})
    )

    result = await _to_detected_entities(hass, ids["device_id"])

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "charger_not_controllable"


async def test_an_integration_nobody_described_is_sent_to_the_generic_type(hass: HomeAssistant) -> None:
    register_shape(hass, SHAPES["wallbox"])  # the selector only exists when a supported device does
    ids = register_shape(hass, Shape("acme_charger", "A1", (E("switch", "A1_charging", "charging", "on"),), {}))

    result = await _to_detected_entities(hass, ids["device_id"], unlisted=True)

    assert result["type"] == FlowResultType.ABORT and result["reason"] == "charger_not_recognised"


async def test_with_evcc_installed_the_flow_warns_and_still_suggests(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    MockConfigEntry(domain="evcc_intg", entry_id="evcc").add_to_hass(hass)

    result = await _to_detected_entities(hass, ids["device_id"])

    assert result["step_id"] == "detected_entities"
    assert "evcc or openWB" in result["description_placeholders"]["warning"]
    assert "Nothing is suggested" not in result["description_placeholders"]["warning"]
    assert _suggested(result)[CONF_CHARGE_CONTROL] == "switch.wallbox_pause_resume"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGE_CONTROL: "switch.wallbox_pause_resume"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_an_easee_charger_can_be_set_up_with_evcc_installed(hass: HomeAssistant) -> None:
    """Field case 2026-10-03: the evcc integration was still installed and the Easee charger, whose
    path has no entity to pick by hand, could not be set up at all."""
    ids = register_shape(hass, SHAPES["easee"])
    MockConfigEntry(domain="evcc_intg", entry_id="evcc").add_to_hass(hass)

    result = await _to_detected_entities(hass, ids["device_id"])
    suggested = _suggested(result)

    assert "evcc or openWB" in result["description_placeholders"]["warning"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {key: value for key, value in suggested.items() if value is not None}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_an_entity_the_path_cannot_be_told_for_is_refused(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    sensor = register(hass, "sensor", "some_sensor", "Some sensor")
    result = await _to_detected_entities(hass, ids["device_id"])

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CHARGE_CONTROL: sensor})

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_CHARGE_CONTROL: "control_path_unknown"}


async def test_the_number_path_needs_the_number_to_write_to(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    result = await _to_detected_entities(hass, ids["device_id"])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CHARGE_CONTROL: "switch.wallbox_pause_resume", CONF_CURRENT_CONTROL: "number"}
    )

    assert result["errors"] == {CONF_CURRENT_CONTROL: "current_limit_required"}


async def test_easee_is_added_through_its_status_sensor_and_its_services(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    result = await _to_detected_entities(hass, ids["device_id"])
    assert _suggested(result)[CONF_CURRENT_CONTROL] == "easee_dynamic_limit"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHARGE_CONTROL: "sensor.easee_status", CONF_CURRENT_CONTROL: "easee_dynamic_limit"},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CONTROL_PATH] == {"kind": "easee", "device_id": ids["device_id"]}
    assert result["data"][CONF_CURRENT_CONTROL] == "easee_dynamic_limit"


# ------------------------------------------------------------ entity config and options flow


async def _loaded_detected_charger(
    hass: HomeAssistant, platform: str, *, current_control: str | None = None, shape: Shape | None = None
) -> MockConfigEntry:
    ids = register_shape(hass, shape or SHAPES[platform])
    found = detect_charger(hass, ids["device_id"])
    data = {
        CONF_ENTRY_TYPE: ENTRY_TYPE_CHARGER,
        "webhook_id": f"hook-{platform}",
        **detected_config(found, current_control=current_control),
    }
    entry = MockConfigEntry(domain=DOMAIN, entry_id=f"det_{platform}", title=platform, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_entity_config_shows_the_control_path_and_its_write_policy(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "wallbox")

    config = await async_get_entity_config(hass, entry.entry_id)

    control = config["control"]
    assert control["platform"] == "wallbox"
    assert control["start_stop"] == {
        "kind": "switch",
        "entity_ids": ["switch.wallbox_pause_resume"],
        "inverted": False,
        "start_option": None,
        "stop_option": None,
    }
    assert control["current"] == {
        "kind": "number",
        "entity_id": "number.wallbox_maximum_charging_current",
        "service": "number.set_value",
        "enabled": True,
    }
    assert control["charging_state"] == {"source": "status", "entity_id": "sensor.wallbox_status_description"}
    assert control["policy"]["min_interval_s"] == 90.0 and control["policy"]["regulator_writes"] is True
    assert control["capabilities"]["regulated_current"] is True
    assert control["conflicts"] == []
    charge_control = next(item for item in config["fields"] if item["field"] == "charge_control")
    assert charge_control["allowed_domains"] == ["switch", "select", "button", "sensor"]


async def test_the_entity_config_names_the_own_modes_that_are_on(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "openevse")

    config = await async_get_entity_config(hass, entry.entry_id)

    assert config["control"]["conflicts"] == [
        {"kind": "own_mode", "entity_id": "switch.openevse_solar_pv_divert", "label": "solar divert", "state": "on"}
    ]


async def test_a_charger_without_a_current_path_reports_none_so_nothing_assumes_one(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "myenergi")

    config = await async_get_entity_config(hass, entry.entry_id)

    assert config["control"]["current"]["kind"] == "none"
    assert config["control"]["capabilities"]["set_current"] is False
    assert config["control"]["start_stop"]["kind"] == "select"
    assert config["control"]["start_stop"]["start_option"] == "Fast"


async def test_a_flash_stored_policy_is_shown_as_not_regulated(hass: HomeAssistant) -> None:
    shape = Shape(
        "garo_wallbox",
        "G1",
        (
            E("select", "G1_mode", "mode", "ALWAYS_OFF", {"options": ["ALWAYS_ON", "ALWAYS_OFF", "SCHEMA"]}),
            E("number", "G1_current_limit", "current_limit", "16", NUMBER_A),
        ),
        {},
    )
    entry = await _loaded_detected_charger(hass, "garo_wallbox", shape=shape)

    control = (await async_get_entity_config(hass, entry.entry_id))["control"]

    assert control["policy"]["flash_stored"] is True and control["policy"]["regulator_writes"] is False
    assert control["capabilities"] == {
        "start_stop": True,
        "set_current": True,
        "regulated_current": False,
        "reads_charging_state": False,
        "reads_measured_current": False,
        "reads_energy_register": False,
    }


async def test_changing_a_detected_chargers_control_follows_the_entity(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "wallbox")
    other = register(hass, "switch", "other_switch", "Other switch")
    current = entry.data[CONF_CHARGE_CONTROL]

    await async_update_entity_config(
        hass,
        entry.entry_id,
        scope="charger",
        expected={"charge_control": current},
        changes={"charge_control": other},
    )

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_CHARGE_CONTROL] == other
    assert updated.data[CONF_CONTROL_PATH] == {"kind": "switch", "entity_id": other, "inverted": False}


async def test_a_control_the_path_cannot_follow_is_refused(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "wallbox")
    sensor = register(hass, "sensor", "some_sensor", "Some sensor")

    with pytest.raises(EntityConfigRefusal) as refusal:
        await async_update_entity_config(
            hass, entry.entry_id, scope="charger", expected={}, changes={"charge_control": sensor}
        )

    assert [error.as_dict() for error in refusal.value.field_errors] == [
        {"field": "charge_control", "code": "control_path_unknown"}
    ]


async def test_the_options_flow_of_a_detected_charger_keeps_its_path_and_takes_the_current_opt_in_away(
    hass: HomeAssistant,
) -> None:
    entry = await _loaded_detected_charger(hass, "wallbox")
    assert entry.data[CONF_CURRENT_CONTROL] == "number"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CHARGE_CONTROL: entry.data[CONF_CHARGE_CONTROL],
            CONF_CURRENT_LIMIT: entry.data[CONF_CURRENT_LIMIT],
            CONF_CURRENT_CONTROL: "",
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_CURRENT_CONTROL] == ""
    assert updated.data[CONF_CONTROL_PATH] == entry.data[CONF_CONTROL_PATH]
    assert updated.data[CONF_CHARGER_PLATFORM] == "wallbox"
    assert updated.data[CONF_CHARGING_STATE] == entry.data[CONF_CHARGING_STATE]


async def test_the_dashboard_says_what_the_adapter_supports(hass: HomeAssistant) -> None:
    with_current = await _loaded_detected_charger(hass, "wallbox")
    start_stop_only = await _loaded_detected_charger(hass, "myenergi")
    opted_out = await _loaded_detected_charger(hass, "nrgkick", current_control="")

    def capabilities(entry: MockConfigEntry) -> dict[str, bool]:
        return dict(capabilities_for(hass, entry, controller_for(hass, entry.entry_id)))

    assert capabilities(with_current)["set_current"] is True
    assert capabilities(with_current)["regulated_current"] is True
    assert capabilities(start_stop_only)["current_limit"] is False
    assert capabilities(start_stop_only)["set_current"] is False
    # A number is configured, so there is a current limit; SpotNav is simply not allowed to set it.
    assert capabilities(opted_out)["current_limit"] is True
    assert capabilities(opted_out)["set_current"] is False
    assert capabilities(opted_out)["regulated_current"] is False


async def test_diagnostics_state_the_adapter_and_its_policy(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "wallbox")

    adapter = (await async_get_config_entry_diagnostics(hass, entry))["controller"]["adapter"]

    assert adapter["platform"] == "wallbox" and adapter["start_stop"] == "switch"
    assert adapter["current"] == "number" and adapter["current_enabled"] is True
    assert adapter["policy"]["min_interval_s"] == 90.0


async def test_easee_charge_control_is_fixed_and_not_offered_for_editing(hass: HomeAssistant) -> None:
    """Easee is started and stopped by its own commands: the status sensor stored as `charge_control`
    only identifies the charger, so the dialog may not offer it as a choice.
    """
    entry = await _loaded_detected_charger(hass, "easee")

    config = await async_get_entity_config(hass, entry.entry_id)

    charge_control = next(item for item in config["fields"] if item["field"] == "charge_control")
    assert charge_control["writable"] is False
    assert config["control"]["start_stop"]["kind"] == "easee"


@pytest.mark.parametrize("domain", ["button", "switch"])
async def test_a_picked_entity_never_replaces_easees_own_commands(hass: HomeAssistant, domain: str) -> None:
    entry = await _loaded_detected_charger(hass, "easee")
    other = register(hass, domain, "easee_start_charging", "Start charging")

    with pytest.raises(EntityConfigRefusal) as refusal:
        await async_update_entity_config(
            hass, entry.entry_id, scope="charger", expected={}, changes={"charge_control": other}
        )

    # A code the card words for people, never a bare "unknown field".
    assert [error.as_dict() for error in refusal.value.field_errors] == [
        {"field": "charge_control", "code": "not_writable"}
    ]
    assert hass.config_entries.async_get_entry(entry.entry_id).data[CONF_CHARGE_CONTROL] == "sensor.easee_status"


async def test_a_button_on_another_detected_charger_is_refused_with_the_path_code(hass: HomeAssistant) -> None:
    entry = await _loaded_detected_charger(hass, "wallbox")
    button = register(hass, "button", "some_button", "Some button")

    with pytest.raises(EntityConfigRefusal) as refusal:
        await async_update_entity_config(
            hass, entry.entry_id, scope="charger", expected={}, changes={"charge_control": button}
        )

    assert [error.as_dict() for error in refusal.value.field_errors] == [
        {"field": "charge_control", "code": "control_path_unknown"}
    ]
