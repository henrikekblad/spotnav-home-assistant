"""The controller driving a charger through its adapter: state, start, stop, current and the
regulator's single write, for chargers that are not OCPP.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.execution.charger_adapter import (
    ASSIGN_FLASH_GUARD,
    ASSIGN_RATE_LIMITED,
)
from custom_components.spotnav.execution.controller import (
    ChargingController,
    REGULATED_HELD,
    REGULATED_STOPPED,
    REGULATED_WROTE,
    RESTORE_FAILED,
    RESTORE_NOT_NEEDED,
    RESTORE_RESTORED,
)

from .charger_helpers import Clock, detected_config, enable_easee_limit_sensor
from .charger_shapes import register_shape, SHAPES


async def _controller(
    hass: HomeAssistant, platform: str, *, clock: Clock | None = None, current_control: str | None = None
) -> ChargingController:
    ids = register_shape(hass, SHAPES[platform])
    found = detect_charger(hass, ids["device_id"])
    controller = ChargingController(hass, f"entry_{platform}", detected_config(found, current_control=current_control))
    if clock is not None:
        # The adapter's own clock: a test moves it by hand.
        controller.adapter.current._limiter._now = clock  # noqa: SLF001
    await controller.async_initialize()
    return controller


def _record(hass: HomeAssistant, calls: list[str], domain: str, service: str) -> None:
    async def handler(call: ServiceCall) -> None:
        calls.append(f"{domain}.{service}")

    hass.services.async_register(domain, service, handler)


async def test_a_charger_that_waits_for_the_car_is_not_charging_but_is_still_stopped(
    hass: HomeAssistant,
) -> None:
    """Wallbox's switch means "not paused". The status says what the charger does; a stop must still
    turn the enabled switch off, or the charger would start the moment the car asks.
    """
    controller = await _controller(hass, "wallbox")
    hass.states.async_set("sensor.wallbox_status_description", "Waiting for car demand")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    turn_on = async_mock_service(hass, "switch", "turn_on")

    assert controller.charging is False
    assert controller.charge_expected_now is True  # the control is on: a Start is in effect

    await controller.async_start()
    assert turn_on == []  # enabled already: no second Start

    await controller.async_stop()
    assert [call.data["entity_id"] for call in turn_off] == ["switch.wallbox_pause_resume"]
    await controller.async_shutdown()


async def test_a_disabled_charger_that_is_not_charging_is_not_stopped_again(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "wallbox")
    hass.states.async_set("switch.wallbox_pause_resume", "off")
    hass.states.async_set("sensor.wallbox_status_description", "Paused")
    turn_off = async_mock_service(hass, "switch", "turn_off")

    await controller.async_stop()

    assert turn_off == []
    await controller.async_shutdown()


async def test_an_inverted_switch_starts_by_turning_off(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "v2c")
    hass.states.async_set("switch.v2c_paused", "on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    turn_on = async_mock_service(hass, "switch", "turn_on")

    assert controller.charging is False
    await controller.async_start()
    assert [call.data["entity_id"] for call in turn_off] == ["switch.v2c_paused"]

    hass.states.async_set("switch.v2c_paused", "off")
    assert controller.charging is True
    await controller.async_stop()
    assert [call.data["entity_id"] for call in turn_on] == ["switch.v2c_paused"]
    await controller.async_shutdown()


async def test_a_button_pair_is_started_and_always_stopped(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "lektrico")
    hass.states.async_set("sensor.lektrico_state", "available")
    presses = async_mock_service(hass, "button", "press")

    await controller.async_start()
    await controller.async_stop()

    assert [call.data["entity_id"] for call in presses] == [
        "button.lektrico_charge_start",
        "button.lektrico_charge_stop",
    ]
    await controller.async_shutdown()


async def test_a_session_start_writes_the_current_before_the_start(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "wallbox")
    hass.states.async_set("switch.wallbox_pause_resume", "off")
    hass.states.async_set("sensor.wallbox_status_description", "Paused")
    order: list[str] = []
    _record(hass, order, "number", "set_value")
    _record(hass, order, "switch", "turn_on")

    await controller.async_start(10)

    assert order == ["number.set_value", "switch.turn_on"]
    assert controller.requested_current_a == 10
    await controller.async_shutdown()


async def test_peblar_is_started_first_because_it_ignores_a_current_while_paused(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "peblar")
    hass.states.async_set("switch.peblar_charge", "off")
    hass.states.async_set("sensor.peblar_cp_state", "no_ev_connected")
    order: list[str] = []

    async def turn_on(call: ServiceCall) -> None:
        order.append("switch.turn_on")
        hass.states.async_set("switch.peblar_charge", "on")

    hass.services.async_register("switch", "turn_on", turn_on)
    _record(hass, order, "number", "set_value")

    await controller.async_start(10)

    assert order == ["switch.turn_on", "number.set_value"]
    await controller.async_shutdown()


async def test_without_the_opt_in_a_start_records_the_current_and_writes_none(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "wallbox", current_control="")
    hass.states.async_set("switch.wallbox_pause_resume", "off")
    number = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "switch", "turn_on")

    await controller.async_start(10)

    assert number == [] and controller.requested_current_a == 10
    assert controller.is_commandable is False
    await controller.async_shutdown()


async def test_a_charger_with_no_current_path_is_not_commandable(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "myenergi")

    assert controller.is_commandable is False
    assert controller.adapter.capabilities.set_current is False
    await controller.async_shutdown()


async def test_the_progress_check_reads_a_non_ocpp_chargers_status_and_current(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "nrgkick")
    hass.states.async_set("sensor.nrgkick_status", "charging")

    facts = controller.charge_progress_facts()
    assert facts.connector_status == "Charging"
    assert facts.current_import_a == pytest.approx(12.1)

    for phase in (1, 2, 3):
        hass.states.async_set(f"sensor.nrgkick_l{phase}_current", "0.0", {"unit_of_measurement": "A"})
    assert controller.charge_progress_facts().current_import_a == 0.0
    await controller.async_shutdown()


async def test_easee_is_told_its_limit_again_when_a_car_is_plugged_in(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "easee")
    enable_easee_limit_sensor(hass, "0")
    hass.states.async_set("sensor.easee_status", "disconnected")
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    await controller.async_set_requested_current(12)
    await hass.async_block_till_done()
    assert limits == []

    hass.states.async_set("sensor.easee_status", "awaiting_start")
    await hass.async_block_till_done()

    assert [call.data["current"] for call in limits] == [11, 12]
    await controller.async_shutdown()


# --------------------------------------------------------------- the regulator's one write


async def test_a_regulated_write_goes_out_when_the_policy_allows(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "wallbox", clock=Clock())
    number = async_mock_service(hass, "number", "set_value")

    result = await controller.async_apply_regulated_current(10, must_lower=False)

    assert (result.outcome, result.written) == (REGULATED_WROTE, True)
    assert [call.data["value"] for call in number] == [10]
    await controller.async_shutdown()


async def test_a_refused_optional_reduction_holds_and_does_not_stop_the_charge(hass: HomeAssistant) -> None:
    clock = Clock()
    controller = await _controller(hass, "wallbox", clock=clock)
    number = async_mock_service(hass, "number", "set_value")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    await controller.async_apply_regulated_current(14, must_lower=False)
    clock.advance(20)

    result = await controller.async_apply_regulated_current(10, must_lower=False)

    assert (result.outcome, result.code, result.written) == (REGULATED_HELD, ASSIGN_RATE_LIMITED, False)
    assert len(number) == 1 and turn_off == []
    await controller.async_shutdown()


async def test_a_reduction_the_fuse_needs_that_cannot_be_written_in_time_stops_the_charge(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    controller = await _controller(hass, "wallbox", clock=clock)
    number = async_mock_service(hass, "number", "set_value")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    await controller.async_apply_regulated_current(14, must_lower=False)
    hass.states.async_set("number.wallbox_maximum_charging_current", "14", {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})
    clock.advance(20)

    result = await controller.async_apply_regulated_current(10, must_lower=True)

    assert (result.outcome, result.code, result.written) == (REGULATED_STOPPED, "safety_stop", False)
    assert len(number) == 1  # nothing was written faster than the policy
    assert [call.data["entity_id"] for call in turn_off] == ["switch.wallbox_pause_resume"]
    await controller.async_shutdown()


async def test_a_refused_write_that_is_not_below_what_the_charger_has_does_not_stop_it(
    hass: HomeAssistant,
) -> None:
    clock = Clock()
    controller = await _controller(hass, "wallbox", clock=clock)
    async_mock_service(hass, "number", "set_value")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    await controller.async_apply_regulated_current(10, must_lower=False)
    hass.states.async_set("number.wallbox_maximum_charging_current", "10", {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})
    clock.advance(20)

    result = await controller.async_apply_regulated_current(12, must_lower=True)

    assert result.outcome == REGULATED_HELD and turn_off == []
    await controller.async_shutdown()


async def test_a_pause_below_the_floor_stops_the_charge_instead_of_writing_zero(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "peblar")
    number = async_mock_service(hass, "number", "set_value")
    turn_off = async_mock_service(hass, "switch", "turn_off")

    result = await controller.async_apply_regulated_current(0, must_lower=False)

    assert (result.outcome, result.code) == (REGULATED_STOPPED, "pause")
    assert number == [] and len(turn_off) == 1
    await controller.async_shutdown()


async def test_a_flash_setting_is_never_written_by_the_regulator_and_the_fuse_stops_it(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["alfen_wallbox"])
    found = detect_charger(hass, ids["device_id"])
    hass.states.async_set("switch.alfen_enable", "on")
    found.charge_control = "switch.alfen_enable"
    found.control_path = {"kind": "switch", "entity_id": "switch.alfen_enable", "inverted": False}
    controller = ChargingController(hass, "alfen", detected_config(found))
    await controller.async_initialize()
    number = async_mock_service(hass, "number", "set_value")
    turn_off = async_mock_service(hass, "switch", "turn_off")

    assert controller.is_commandable is True
    assert controller.adapter.capabilities.regulated_current is False

    held = await controller.async_apply_regulated_current(12, must_lower=False)
    assert (held.outcome, held.code) == (REGULATED_HELD, ASSIGN_FLASH_GUARD)

    stopped = await controller.async_apply_regulated_current(10, must_lower=True)
    assert (stopped.outcome, stopped.code) == (REGULATED_STOPPED, "safety_stop")
    assert number == [] and len(turn_off) == 1
    await controller.async_shutdown()


async def test_a_failing_stop_is_reported_and_does_not_raise(hass: HomeAssistant) -> None:
    clock = Clock()
    controller = await _controller(hass, "wallbox", clock=clock)
    async_mock_service(hass, "number", "set_value")

    async def boom(call: ServiceCall) -> None:
        raise RuntimeError("cloud down")

    hass.services.async_register("switch", "turn_off", boom)
    await controller.async_apply_regulated_current(14, must_lower=False)
    clock.advance(1)

    result = await controller.async_apply_regulated_current(10, must_lower=True)

    assert (result.outcome, result.code) == (REGULATED_HELD, "stop_failed")
    await controller.async_shutdown()


# ------------------------------------------------------------------------------- the restore


async def test_the_restore_writes_a_lowered_current_back_and_confirms_it(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "wallbox", clock=Clock())
    await controller.async_set_requested_current(16)
    hass.states.async_set("number.wallbox_maximum_charging_current", "10", {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})

    async def set_value(call: ServiceCall) -> None:
        hass.states.async_set(
            call.data["entity_id"], str(call.data["value"]), {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1}
        )

    hass.services.async_register("number", "set_value", set_value)

    restore = await controller.async_restore_current(lowered_by_balancing=True)

    assert (restore.outcome, restore.from_a, restore.to_a) == (RESTORE_RESTORED, 10, 16)
    again = await controller.async_restore_current(lowered_by_balancing=True)
    assert again.outcome == RESTORE_NOT_NEEDED
    await controller.async_shutdown()


async def test_a_restore_the_policy_refuses_is_reported_as_failed_with_its_code(hass: HomeAssistant) -> None:
    clock = Clock()
    controller = await _controller(hass, "wallbox", clock=clock)
    async_mock_service(hass, "number", "set_value")
    await controller.async_set_requested_current(16)
    await controller.async_apply_regulated_current(10, must_lower=False)
    hass.states.async_set("number.wallbox_maximum_charging_current", "10", {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1})
    clock.advance(5)

    restore = await controller.async_restore_current(lowered_by_balancing=True)

    assert (restore.outcome, restore.code) == (RESTORE_FAILED, ASSIGN_RATE_LIMITED)
    await controller.async_shutdown()


async def _easee_commands(hass: HomeAssistant) -> list[str]:
    calls: list[str] = []

    async def handler(call: ServiceCall) -> None:
        calls.append(call.data["action_command"])

    hass.services.async_register("easee", "action_command", handler)
    return calls


@pytest.mark.parametrize("status", ["awaiting_start", "ready_to_charge"])
async def test_a_manual_start_on_easee_sends_resume_whatever_the_idle_status(
    hass: HomeAssistant, status: str
) -> None:
    """Manual Start -> `ChargingController.async_start(manual=True)` -> adapter -> `EaseeCommandPath`.

    Neither idle status is `charging`, a held status or an enabled switch (Easee reports none), so the
    controller does not skip the command, and the charge control being a sensor changes nothing: the
    Easee path calls its own service by device id and never looks at an entity's availability.
    """
    controller = await _controller(hass, "easee")
    hass.states.async_set("sensor.easee_status", status, {"config_authorizationRequired": False})
    commands = await _easee_commands(hass)

    assert controller.charging is False and controller.held_by_charger is False

    assert await controller.async_start(manual=True) is True

    # `awaiting_start` without a pause of ours may be a charger an earlier version deauthorized,
    # so the start authorizes first; `ready_to_charge` is authorized already.
    assert commands == (["start", "resume"] if status == "awaiting_start" else ["resume"])
    await controller.async_shutdown()


async def test_a_manual_start_on_easee_still_resumes_with_a_pause_flag_left_set_and_a_low_current(
    hass: HomeAssistant,
) -> None:
    controller = await _controller(hass, "easee")
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": False})
    commands = await _easee_commands(hass)
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    await controller.async_stop()
    assert commands == ["pause"] and controller.adapter.path.paused is True
    commands.clear()

    assert await controller.async_start(6, manual=True) is True

    assert commands == ["resume"]
    assert controller.adapter.path.paused is False
    await controller.async_shutdown()


async def test_a_manual_start_on_easee_authorizes_first_when_authorization_is_owed(hass: HomeAssistant) -> None:
    controller = await _controller(hass, "easee")
    hass.states.async_set("sensor.easee_status", "awaiting_start", {"config_authorizationRequired": True})
    commands = await _easee_commands(hass)

    assert await controller.async_start(manual=True) is True

    assert commands == ["start", "resume"]
    await controller.async_shutdown()
