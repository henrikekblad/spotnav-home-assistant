"""OCPP: the original `ChangeConfiguration` path, its configure answer, and the session-limit number."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    MODE_OCPP,
)
from custom_components.spotnav.execution.chargers.adapter import build_adapter
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_REBOOT_REQUIRED,
    ASSIGN_TARGET_UNAVAILABLE,
    WRITE_REGULATOR,
    WRITE_SESSION_START,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.ocpp_identity import OcppConnectorTarget

from ..charger_helpers import adapter_for, Clock
from ..charger_shapes import register_shape, SHAPES

NUMBER_ATTRS = {"unit_of_measurement": "A", "min": 0, "max": 16, "step": 1}


async def test_the_ocpp_adapter_is_the_original_path_with_no_policy_of_its_own(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.cp_charge_control", "off")
    config = {
        CONF_MODE: MODE_OCPP,
        CONF_CHARGE_CONTROL: "switch.cp_charge_control",
        CONF_CURRENT_LIMIT: "",
        CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CONF_OCPP_CHARGE_POINT_ID: "cp",
        CONF_OCPP_CONNECTOR_ID: 1,
    }
    adapter = build_adapter(
        hass, config, ocpp_target=lambda: OcppConnectorTarget("cp", 1), energy_entity_id=None
    )

    assert adapter.is_ocpp
    assert adapter.policy.min_interval_s == 0 and not adapter.policy.flash_stored
    assert adapter.capabilities.regulated_current is True
    assert adapter.describe()["current"]["service"] == "ocpp.configure"
    assert adapter.describe()["start_stop"]["kind"] == "switch"


async def test_an_ocpp_entry_with_the_number_path_writes_the_number_under_the_local_policy(hass) -> None:
    from custom_components.spotnav.const import (
        CONF_CHARGE_CONTROL,
        CONF_CURRENT_CONTROL,
        CONF_CURRENT_LIMIT,
        CONF_MODE,
        MODE_OCPP,
    )
    from custom_components.spotnav.execution.chargers.generic import NumberCurrent

    adapter = build_adapter(
        hass,
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: "switch.x_connector_1_charge_control",
            CONF_CURRENT_CONTROL: "number",
            CONF_CURRENT_LIMIT: "number.x_connector_1_session_current_limit",
        },
        ocpp_target=lambda: None,
        energy_entity_id=None,
    )

    assert isinstance(adapter.current, NumberCurrent)
    assert adapter.policy.min_interval_s == 10.0 and adapter.policy.zero_pauses is False


def _ocpp_number_adapter(hass: HomeAssistant):
    return build_adapter(
        hass,
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: "switch.cp_connector_1_charge_control",
            CONF_CURRENT_CONTROL: "number",
            CONF_CURRENT_LIMIT: "number.cp_connector_1_session_current_limit",
        },
        ocpp_target=lambda: None,
        energy_entity_id=None,
        now=Clock(),
    )


async def test_the_session_limit_is_writable_while_it_reads_unknown(hass: HomeAssistant) -> None:
    adapter = _ocpp_number_adapter(hass)
    calls = async_mock_service(hass, "number", "set_value")
    entity = "number.cp_connector_1_session_current_limit"

    hass.states.async_set(entity, "unknown", NUMBER_ATTRS)
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [call.data["value"] for call in calls] == [10]

    hass.states.async_set(entity, "unavailable")
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_TARGET_UNAVAILABLE
    assert len(calls) == 1, "before the transaction nothing is written"


async def test_other_numbers_stay_unwritable_while_unknown(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())
    calls = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("number.wallbox_maximum_charging_current", "unknown", NUMBER_ATTRS)

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_TARGET_UNAVAILABLE
    assert calls == []


async def test_the_session_current_is_written_once_the_transaction_has_started(hass: HomeAssistant) -> None:
    entity = "number.cp_connector_1_session_current_limit"
    switch = "switch.cp_connector_1_charge_control"
    hass.states.async_set(switch, "off")
    hass.states.async_set(entity, "unavailable")
    turn_on = async_mock_service(hass, "switch", "turn_on")
    calls = async_mock_service(hass, "number", "set_value")
    controller = ChargingController(
        hass,
        "entry_ocpp_number",
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: switch,
            CONF_CURRENT_CONTROL: "number",
            CONF_CURRENT_LIMIT: entity,
            CONF_OCPP_CHARGE_POINT_ID: "cp",
            CONF_OCPP_CONNECTOR_ID: 1,
        },
    )
    await controller.async_initialize()

    await controller.async_start(amps=10)

    assert len(turn_on) == 1
    assert calls == [], "the number is not there before the transaction"
    assert controller.requested_current_a == 10

    # The charger starts the transaction: the number appears, `unknown` until its first write.
    hass.states.async_set(switch, "on")
    hass.states.async_set(entity, "unknown", NUMBER_ATTRS)
    await hass.async_block_till_done()

    assert [call.data["value"] for call in calls] == [10]
    hass.states.async_set(entity, "10", NUMBER_ATTRS)
    await hass.async_block_till_done()
    assert len(calls) == 1, "the retry is one write, not a loop"
    await controller.async_shutdown()


def _ocpp_adapter(hass: HomeAssistant, reply: dict | None, *, supports_response: bool = True):
    from homeassistant.core import SupportsResponse

    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.16"})
    calls: list[ServiceCall] = []

    async def configure(call: ServiceCall):
        calls.append(call)
        return reply

    hass.services.async_register(
        "ocpp",
        "configure",
        configure,
        supports_response=SupportsResponse.OPTIONAL if supports_response else SupportsResponse.NONE,
    )
    adapter = build_adapter(
        hass,
        {
            CONF_MODE: MODE_OCPP,
            CONF_CHARGE_CONTROL: "switch.cp_charge_control",
            CONF_CURRENT_LIMIT: "",
            CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
            CONF_OCPP_CHARGE_POINT_ID: "cp",
            CONF_OCPP_CONNECTOR_ID: 1,
        },
        ocpp_target=lambda: OcppConnectorTarget("cp", 1),
        energy_entity_id=None,
    )
    return adapter, calls


async def test_a_reboot_required_answer_is_reported_not_swallowed(hass: HomeAssistant) -> None:
    adapter, calls = _ocpp_adapter(hass, {"reboot_required": True})

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_REBOOT_REQUIRED
    assert len(calls) == 1
    assert adapter.current.last_written_a is None, "stored by the charge point, not in effect"


async def test_an_accepted_answer_is_assigned(hass: HomeAssistant) -> None:
    adapter, calls = _ocpp_adapter(hass, {"reboot_required": False})

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [call.data["value"] for call in calls] == ["1.10"]


async def test_a_configure_service_that_gives_no_response_is_still_called(hass: HomeAssistant) -> None:
    adapter, calls = _ocpp_adapter(hass, None, supports_response=False)

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert len(calls) == 1
