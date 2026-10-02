"""The first fix round after the charger audit: Easee read-back, floor and waiting statuses, commands
to unavailable entities, and the OCPP session-limit number.
"""

from __future__ import annotations

import pytest
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
from custom_components.spotnav.execution import charger_adapter
from custom_components.spotnav.execution.charger_adapter import (
    ASSIGN_ASSIGNED,
    ASSIGN_IGNORED_WHILE_PAUSED,
    ASSIGN_RATE_LIMITED,
    ASSIGN_REBOOT_REQUIRED,
    ASSIGN_TARGET_UNAVAILABLE,
    ASSIGN_UNCHANGED,
    ASSIGN_UNCONFIRMED,
    build_adapter,
    ButtonPath,
    EASEE_START_HOLD_S,
    SelectPath,
    SwitchPath,
    WRITE_REGULATOR,
    WRITE_RESEND,
    WRITE_RESTORE,
    WRITE_SESSION_START,
)
from custom_components.spotnav.execution.controller import (
    ChargingController,
    RESTORE_RESTORED,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.ocpp_identity import OcppConnectorTarget

from .charger_helpers import (
    adapter_for,
    Clock,
    detected_config,
    enable_easee_limit_sensor,
    set_easee_limit,
)
from .charger_shapes import register_shape, SHAPES

NUMBER_ATTRS = {"unit_of_measurement": "A", "min": 0, "max": 16, "step": 1}


async def _easee(hass: HomeAssistant, clock: Clock | None = None):
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=clock or Clock())
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    commands = async_mock_service(hass, "easee", "action_command")
    return ids, adapter, limits, commands


def _sent(limits) -> list[int]:
    return [call.data["current"] for call in limits]


# ----------------------------------------------------------------------------- Easee read-back


async def test_the_status_sensor_is_never_a_read_back_source(hass: HomeAssistant) -> None:
    """easee_hass's status sensor has no dynamic limit attribute; one planted there is not read."""
    ids, adapter, limits, _ = await _easee(hass)
    hass.states.async_set("sensor.easee_status", "charging", {"state_dynamicChargerCurrent": 12})

    assert adapter.current.read_back_a() is None
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert _sent(limits) == [12]


async def test_the_limit_is_read_from_the_state_of_the_enabled_limit_sensor(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass)
    assert adapter.current.limit_entity_id() is None, "disabled by default: nothing to read"

    enable_easee_limit_sensor(hass, "12")

    assert adapter.current.limit_entity_id() == "sensor.easee_dynamic_charger_limit"
    assert adapter.current.read_back_a() == 12
    set_easee_limit(hass, "unavailable")
    assert adapter.current.read_back_a() is None


async def test_a_write_nobody_can_read_back_is_unverifiable_and_not_failed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(charger_adapter.asyncio, "sleep", record)
    ids, adapter, limits, _ = await _easee(hass)

    assert await adapter.async_set_current(12, reason=WRITE_RESTORE, verify=True) == ASSIGN_ASSIGNED
    assert slept == [], "there is nothing to wait for"

    enable_easee_limit_sensor(hass, "unknown")
    assert await adapter.async_set_current(12, reason=WRITE_RESTORE, verify=True) == ASSIGN_ASSIGNED
    set_easee_limit(hass, "9")
    assert await adapter.async_set_current(12, reason=WRITE_RESTORE, verify=True) == ASSIGN_UNCONFIRMED


async def test_a_restore_the_charger_cannot_confirm_is_not_reported_as_failed(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    controller = ChargingController(hass, "entry_easee", detected_config(found))
    await controller.async_initialize()
    await controller.async_set_requested_current(16)
    await controller.async_apply_regulated_current(10, must_lower=False)

    restore = await controller.async_restore_current(lowered_by_balancing=True)

    assert (restore.outcome, restore.to_a) == (RESTORE_RESTORED, 16)
    await controller.async_shutdown()


async def test_the_regulator_does_not_resend_a_value_it_cannot_read_back(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    for _ in range(5):
        assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_UNCHANGED
    assert _sent(limits) == [10]
    # A start, a restore and a plug-in resend still send it: the charger may have forgotten it.
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert _sent(limits) == [10, 10]


# ----------------------------------------------------------------------------- Easee 7 A floor


async def test_a_start_and_a_resend_are_never_below_seven_amps(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits, _ = await _easee(hass, clock)

    assert await adapter.async_set_current(6, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(1)
    assert await adapter.async_set_current(6, reason=WRITE_RESEND) == ASSIGN_ASSIGNED
    clock.advance(1)
    assert await adapter.async_set_current(7, reason=WRITE_RESEND) == ASSIGN_ASSIGNED

    assert _sent(limits) == [7, 7, 7], "no 6 A write first, not even the nudge one amp lower"
    assert adapter.min_start_current_a == 7.0


async def test_a_resend_above_the_floor_still_nudges_one_amp_lower(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())

    assert await adapter.async_set_current(8, reason=WRITE_RESEND) == ASSIGN_ASSIGNED

    assert _sent(limits) == [7, 8]


async def test_the_regulator_may_go_to_six_but_not_straight_after_a_start(hass: HomeAssistant) -> None:
    clock = Clock()
    ids, adapter, limits, _ = await _easee(hass, clock)
    await adapter.async_set_current(6, reason=WRITE_SESSION_START)
    clock.advance(EASEE_START_HOLD_S / 2)

    assert await adapter.async_set_current(6, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    assert await adapter.async_set_current(8, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED, "above is free"

    clock.advance(EASEE_START_HOLD_S)
    assert await adapter.async_set_current(6, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    assert _sent(limits) == [7, 8, 6]


async def test_the_planner_reasons_with_the_profile_minimum(hass: HomeAssistant) -> None:
    ids, easee, _, _ = await _easee(hass)
    wallbox_ids = register_shape(hass, SHAPES["wallbox"])
    wallbox = adapter_for(hass, detect_charger(hass, wallbox_ids["device_id"]), clock=Clock())

    assert easee.min_start_current_a == 7.0
    assert wallbox.min_start_current_a == 6.0


# ----------------------------------------------------------------------------- Easee restart


async def test_after_a_restart_a_limit_that_reads_zero_is_a_pause(hass: HomeAssistant) -> None:
    ids, adapter, limits, commands = await _easee(hass, Clock())
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    enable_easee_limit_sensor(hass, "0")
    assert adapter.current._paused() is True  # noqa: SLF001

    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []

    await adapter.async_start()
    set_easee_limit(hass, "16")
    assert [call.data["action_command"] for call in commands] == ["resume"]
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED


async def test_a_limit_that_reads_zero_is_ignored_when_the_sensor_is_not_enabled(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    assert adapter.current._paused() is False  # noqa: SLF001
    assert await adapter.async_set_current(10, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED


async def test_a_plug_in_is_owed_its_limit_even_when_the_old_limit_still_reads_zero(hass: HomeAssistant) -> None:
    ids, adapter, limits, _ = await _easee(hass, Clock())
    enable_easee_limit_sensor(hass, "0")
    hass.states.async_set("sensor.easee_status", "disconnected")
    assert adapter.current._paused() is False  # noqa: SLF001
    hass.states.async_set("sensor.easee_status", "awaiting_start")
    assert adapter.current.needs_resend("disconnected", "awaiting_start") is True

    assert await adapter.async_set_current(10, reason=WRITE_RESEND) == ASSIGN_ASSIGNED


# ----------------------------------------------------------------------------- Easee waiting statuses


@pytest.mark.parametrize(
    "status",
    ["awaiting_scheduled_start", "awaiting_smart_start", "awaiting_load_balancing", "paused_due_to_equalizer"],
)
async def test_the_chargers_own_scheduler_explains_itself(hass: HomeAssistant, status: str) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    controller = ChargingController(hass, "entry_easee", detected_config(found))
    await controller.async_initialize()
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    await controller.async_start()
    assert controller.start_pending is True, "an unanswered Start is pending"
    hass.states.async_set("sensor.easee_status", status)

    assert controller.held_by_charger is True
    assert controller.start_pending is False, "the charger answered: its own scheduler holds it"
    await controller.async_shutdown()


async def test_other_statuses_are_not_a_held_charge(hass: HomeAssistant) -> None:
    ids, adapter, _, _ = await _easee(hass)
    for status in ("awaiting_start", "charging", "ready_to_charge", "completed", "disconnected"):
        hass.states.async_set("sensor.easee_status", status)
        assert adapter.held_by_charger() is False


# ----------------------------------------------------------------------------- Easee authorization


async def test_a_required_authorization_is_a_second_signal_for_start_before_resume(hass: HomeAssistant) -> None:
    ids, adapter, _, commands = await _easee(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": True})

    await adapter.async_start()

    assert [call.data["action_command"] for call in commands] == ["start", "resume"]


async def test_no_start_is_sent_when_authorization_is_not_required_or_the_pause_is_ours(
    hass: HomeAssistant,
) -> None:
    ids, adapter, _, commands = await _easee(hass)
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": False})
    await adapter.async_start()
    assert [call.data["action_command"] for call in commands] == ["resume"]

    commands.clear()
    hass.states.async_set("sensor.easee_status", "charging", {"config_authorizationRequired": True})
    await adapter.async_stop()
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": True})
    await adapter.async_start()
    assert [call.data["action_command"] for call in commands] == ["pause", "resume"]


# ----------------------------------------------------------------------------- unavailable entities


async def _paths(hass: HomeAssistant):
    for entity_id in ("switch.s", "select.x", "button.start", "button.stop"):
        hass.states.async_set(entity_id, "unavailable")
    return (
        SwitchPath(hass, "switch.s"),
        SwitchPath(hass, "switch.s", inverted=True),
        SelectPath(hass, "select.x", start_option="on", stop_option="off"),
        ButtonPath(hass, "button.start", "button.stop"),
    )


async def test_a_switch_that_is_unavailable_is_not_commanded(hass: HomeAssistant) -> None:
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    plain, inverted, _, _ = await _paths(hass)

    for path in (plain, inverted):
        assert await path.async_start() is False
        assert await path.async_stop() is False
    assert on == [] and off == []

    hass.states.async_set("switch.s", "off")
    assert await plain.async_start() is True
    assert await plain.async_stop() is True
    assert len(on) == 1 and len(off) == 1


async def test_a_select_that_is_unavailable_is_not_commanded(hass: HomeAssistant) -> None:
    calls = async_mock_service(hass, "select", "select_option")
    _, _, select, _ = await _paths(hass)

    assert await select.async_start() is False
    assert await select.async_stop() is False
    assert calls == []

    hass.states.async_set("select.x", "off")
    assert await select.async_start() is True
    assert [call.data["option"] for call in calls] == ["on"]


async def test_a_button_that_is_unavailable_is_not_pressed(hass: HomeAssistant) -> None:
    presses = async_mock_service(hass, "button", "press")
    _, _, _, buttons = await _paths(hass)

    assert await buttons.async_start() is False
    assert await buttons.async_stop() is False
    assert presses == []

    hass.states.async_set("button.start", "unknown")  # a button is `unknown` until pressed: available
    assert await buttons.async_start() is True
    assert await buttons.async_stop() is False, "the stop button is still unavailable"
    assert [call.data["entity_id"] for call in presses] == ["button.start"]


async def test_a_missing_entity_is_not_commanded_either(hass: HomeAssistant) -> None:
    calls = async_mock_service(hass, "switch", "turn_on")

    assert await SwitchPath(hass, "switch.gone").async_start() is False
    assert calls == []


async def test_the_controller_does_not_claim_a_start_that_never_went_out(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    found = detect_charger(hass, ids["device_id"])
    turn_on = async_mock_service(hass, "switch", "turn_on")
    controller = ChargingController(hass, "entry_wallbox", detected_config(found))
    await controller.async_initialize()
    hass.states.async_set("sensor.wallbox_status_description", "Paused")
    hass.states.async_set("switch.wallbox_pause_resume", "unavailable")

    assert await controller.async_start() is False

    assert turn_on == []
    assert controller.start_pending is False, "nothing is awaiting an answer"
    hass.states.async_set("switch.wallbox_pause_resume", "off")
    assert await controller.async_start() is True
    assert len(turn_on) == 1 and controller.start_pending is True
    await controller.async_shutdown()


# ----------------------------------------------------------------------------- Wallbox idle value


async def test_the_wallbox_idle_status_is_the_one_home_assistant_reports(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["wallbox"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())

    hass.states.async_set("sensor.wallbox_status_description", "Waiting for car demand")
    assert adapter.progress_status() == "SuspendedEV"
    hass.states.async_set("sensor.wallbox_status_description", "waiting for car demand")
    assert adapter.progress_status() == "SuspendedEV", "the match ignores case"
    hass.states.async_set("sensor.wallbox_status_description", "Connected: waiting car demand")
    assert adapter.progress_status() == "connected: waiting car demand", "the portal wording is not it"


# ----------------------------------------------------------------------------- OCPP session limit


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


# ----------------------------------------------------------------------------- OCPP configure


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


async def test_solar_starts_at_the_chargers_start_minimum_and_runs_down_to_the_floor(
    hass: HomeAssistant,
) -> None:
    from types import SimpleNamespace

    from custom_components.spotnav.execution.solar_execution import SolarExecutionCoordinator

    site = SimpleNamespace(config={})

    def solar_for(platform: str):
        ids = register_shape(hass, SHAPES[platform], suffix=platform)
        adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), clock=Clock())
        fake = SimpleNamespace(
            _charger_entry_id="entry",
            _controller=SimpleNamespace(adapter=adapter, charging=False),
        )
        return SolarExecutionCoordinator._build_controller(fake, site)  # noqa: SLF001

    easee = solar_for("easee")._config  # noqa: SLF001
    assert (easee.start_a, easee.stop_a, easee.min_current_a) == (7.0, 6.0, 6.0)
    other = solar_for("nrgkick")._config  # noqa: SLF001
    assert (other.start_a, other.stop_a, other.min_current_a) == (6.0, 5.0, 6.0)
