"""A charge that starts by itself outside every planned window is stopped once per plug-in."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.execution.window_hold import HOLD, NOTHING, OVERRIDE, WindowHold
from custom_components.spotnav.flows.charger_detection import detect_charger

from .charger_helpers import detected_config
from .charger_shapes import register_shape, SHAPES
from .helpers import install_schedule

STATUS = "sensor.charger_connector_1_status_connector"


# --- the rule on its own -------------------------------------------------------------------------


def test_the_rule_holds_once_then_respects_a_person_and_a_session_end_resets_it() -> None:
    hold = WindowHold()
    hold.baseline(False)
    assert hold.observe(control_on=True, connected=True, gap=True) == HOLD
    assert hold.observe(control_on=False, connected=True, gap=True) == NOTHING
    assert hold.observe(control_on=True, connected=True, gap=True) == OVERRIDE and hold.overridden
    assert hold.observe(control_on=False, connected=True, gap=True) == NOTHING and not hold.overridden
    assert hold.observe(control_on=False, connected=False, gap=True) == NOTHING
    assert hold.observe(control_on=True, connected=True, gap=True) == HOLD


def test_a_charge_that_already_runs_or_is_spotnavs_or_is_inside_a_window_is_left_alone() -> None:
    hold = WindowHold()
    hold.baseline(True)
    assert hold.observe(control_on=True, connected=True, gap=True) == NOTHING
    hold.observe(control_on=False, connected=True, gap=True)
    hold.spotnav_started()
    assert hold.observe(control_on=True, connected=True, gap=True) == NOTHING
    hold.observe(control_on=False, connected=True, gap=True)
    assert hold.observe(control_on=True, connected=True, gap=False) == NOTHING


# --- the controller -------------------------------------------------------------------------------


def _windows(hours_ahead: float = 2.0) -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=hours_ahead)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10}


async def _switch_controller(hass: HomeAssistant, plan: dict[str, Any] | None = None):
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    if plan is not None:
        await install_schedule(controller, plan)
    return controller, async_mock_service(hass, "switch", "turn_off"), async_mock_service(hass, "switch", "turn_on")


async def _plug_in_charging(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "on")
    await hass.async_block_till_done()


async def test_a_charge_that_starts_by_itself_between_windows_is_stopped_once(hass: HomeAssistant) -> None:
    controller, stops, _ = await _switch_controller(hass, _windows())

    await _plug_in_charging(hass)

    assert len(stops) == 1
    hass.states.async_set("switch.a", "off")  # the charger obeys
    await hass.async_block_till_done()
    assert controller.hold_until is not None
    await controller.async_shutdown()


async def test_a_charge_spotnav_started_is_not_stopped(hass: HomeAssistant) -> None:
    controller, stops, starts = await _switch_controller(hass, _windows())

    await controller.async_start(manual=True)
    assert controller.hold_until is None  # a start the charger has not reported yet is not a wait
    await _plug_in_charging(hass)

    assert len(starts) == 1 and stops == []
    await controller.async_shutdown()


async def test_a_second_start_after_the_hold_is_respected_and_noticed(hass: HomeAssistant) -> None:
    controller, stops, _ = await _switch_controller(hass, _windows())
    await _plug_in_charging(hass)
    hass.states.async_set("switch.a", "off")
    await hass.async_block_till_done()
    assert controller.hold_until is not None and not controller.hold_overridden

    await _plug_in_charging(hass)

    assert len(stops) == 1
    assert controller.hold_overridden is True and controller.hold_until is None
    await controller.async_shutdown()


async def test_unplugging_starts_a_new_session(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    hass.states.async_set(STATUS, "Available")
    config = {
        CONF_CHARGE_CONTROL: "switch.a",
        CONF_CURRENT_LIMIT: "number.charger_connector_1_session_current_limit",
        CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CONF_OCPP_CHARGE_POINT_ID: "charger",
        CONF_OCPP_CONNECTOR_ID: 1,
    }
    controller = ChargingController(hass, "entry_a", config)
    await controller.async_initialize()
    await install_schedule(controller, _windows())
    stops = async_mock_service(hass, "switch", "turn_off")

    hass.states.async_set(STATUS, "Preparing")
    assert controller.hold_until is not None  # plugged in and waiting for the window
    await _plug_in_charging(hass)
    assert len(stops) == 1
    hass.states.async_set("switch.a", "off")
    hass.states.async_set(STATUS, "Available")
    await hass.async_block_till_done()
    assert controller.hold_until is None  # nobody is plugged in

    hass.states.async_set(STATUS, "Preparing")
    await _plug_in_charging(hass)

    assert len(stops) == 2 and not controller.hold_overridden
    await controller.async_shutdown()


async def test_the_next_window_starts_as_usual_and_ends_the_session(hass: HomeAssistant) -> None:
    plan = _windows()
    controller, stops, starts = await _switch_controller(hass, plan)
    await _plug_in_charging(hass)
    hass.states.async_set("switch.a", "off")
    await hass.async_block_till_done()

    with freeze_time(dt_util.parse_datetime(plan["start"]) + timedelta(seconds=1)):
        await controller._async_start_window()  # noqa: SLF001 - the timer's own entry

    assert len(starts) == 1 and len(stops) == 1
    assert controller._hold.held is False  # noqa: SLF001
    await controller.async_shutdown()


async def test_nothing_is_held_without_a_plan_or_after_the_last_window(hass: HomeAssistant) -> None:
    controller, stops, _ = await _switch_controller(hass)
    await _plug_in_charging(hass)
    assert stops == [] and controller.hold_until is None
    await controller.async_shutdown()

    await hass.async_block_till_done()
    controller, stops, _ = await _switch_controller(hass, _windows())
    hass.states.async_set("switch.a", "off")
    plan = controller.plan
    with freeze_time(dt_util.parse_datetime(plan.end) + timedelta(minutes=1)):
        await _plug_in_charging(hass)
    assert stops == []
    await controller.async_shutdown()


async def test_a_guard_that_says_something_else_owns_the_charger_holds_nothing(hass: HomeAssistant) -> None:
    controller, stops, _ = await _switch_controller(hass, _windows())
    controller.set_hold_guard(lambda: True)

    await _plug_in_charging(hass)

    assert stops == [] and controller.hold_until is None
    await controller.async_shutdown()


async def test_a_charge_solar_or_hybrid_execution_started_is_spotnavs(hass: HomeAssistant) -> None:
    controller, stops, starts = await _switch_controller(hass, _windows())

    await controller.async_start(10)  # what a solar start calls
    await _plug_in_charging(hass)

    assert len(starts) == 1 and stops == []
    await controller.async_shutdown()


async def test_a_restart_mid_gap_with_the_car_charging_stops_it_as_before(hass: HomeAssistant) -> None:
    controller, stops, _ = await _switch_controller(hass, _windows())
    await controller.async_shutdown()
    hass.states.async_set("switch.a", "on")
    restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})

    await restarted.async_initialize()
    await hass.async_block_till_done()

    assert len(stops) == 1  # the re-arm stop alone, not a second one from the hold
    await restarted.async_shutdown()


async def test_an_easee_that_starts_charging_between_windows_is_paused_once(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    hass.states.async_set("sensor.easee_status", "disconnected")
    commands = async_mock_service(hass, "easee", "action_command")
    controller = ChargingController(hass, "entry_easee", config)
    await controller.async_initialize()
    await install_schedule(controller, _windows())
    commands.clear()

    hass.states.async_set("sensor.easee_status", "awaiting_start")
    assert controller.hold_until is not None
    hass.states.async_set("sensor.easee_status", "charging")
    await hass.async_block_till_done()

    assert [call.data["action_command"] for call in commands] == ["pause"]
    await controller.async_shutdown()
