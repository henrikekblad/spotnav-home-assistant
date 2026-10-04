"""What a restart used to forget (the state machine review's I6, I5 and its stale origin): a charge load
balancing paused, a person's override of the hold, an Easee pause of ours, and that a charge ended by
itself."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.chargers.base import ASSIGN_IGNORED_WHILE_PAUSED, WRITE_RESEND
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.flows.charger_detection import detect_charger

from .charger_helpers import detected_config
from .charger_shapes import register_shape, SHAPES
from .helpers import install_schedule
from .pause_world import later_window
from .test_replug import Plug, _obedient_switch

CONFIG = {CONF_CHARGE_CONTROL: "switch.a"}


async def _controller(hass: HomeAssistant, config: dict[str, Any] = CONFIG) -> ChargingController:
    controller = ChargingController(hass, "entry_a", config)
    await controller.async_initialize()
    return controller


async def _restart(hass: HomeAssistant, controller: ChargingController, config: dict[str, Any] = CONFIG) -> ChargingController:
    await controller.async_shutdown()
    return await _controller(hass, config)


async def test_a_charge_balancing_paused_is_still_resumed_after_a_restart(hass: HomeAssistant) -> None:
    """Load balancing paused a person's charge outside any window; after a restart its regulator must still
    resume it, as the person's."""
    hass.states.async_set("switch.a", "off")
    _obedient_switch(hass)
    controller = await _controller(hass)
    await controller.async_start(10, manual=True)
    await controller._regulated_stop("pause")  # noqa: SLF001 - the regulator's pause below the floor
    assert controller.paused_by_balancing

    restarted = await _restart(hass, controller)

    assert restarted.paused_by_balancing, "the wish to charge survives the restart"
    assert await restarted.async_battery_probe_start(8)
    assert restarted.charge_origin == "manual"
    await restarted.async_shutdown()


async def test_a_persons_override_of_the_hold_is_still_respected_after_a_restart(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    starts, stops = _obedient_switch(hass)
    controller = await _controller(hass)
    plug = Plug(hass, controller, "switch.a")
    await plug.set(True, control="off")
    await install_schedule(controller, later_window())
    await plug.set(True, control="on")  # the charger begins by itself outside the window: held
    await hass.async_block_till_done()
    assert len(stops) == 1
    await plug.set(True, control="on")  # the person starts it again: an override
    await hass.async_block_till_done()
    assert len(stops) == 1 and controller.hold_overridden

    restarted = await _restart(hass, controller)
    plug = Plug(hass, restarted, "switch.a")
    plug.connected = True
    await plug.set(True, control="off")
    await plug.set(True, control="on")  # stopped and started again before the window
    await hass.async_block_till_done()

    assert len(stops) == 1, "the person's override of the hold is not stopped once more"
    await restarted.async_shutdown()


async def test_a_charge_that_ended_by_itself_is_nobodys_after_a_restart(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    _obedient_switch(hass)
    controller = await _controller(hass)
    await controller.async_start(10, manual=True)
    assert controller.charge_origin == "manual"
    hass.states.async_set("switch.a", "off")  # the car ended it by itself
    await hass.async_block_till_done()
    assert controller.charge_origin is None

    restarted = await _restart(hass, controller)

    assert restarted.charge_origin is None, "a stale origin would spare the next charge the charger begins"
    await restarted.async_shutdown()


async def test_an_easee_pause_of_ours_is_still_ours_after_a_restart(hass: HomeAssistant) -> None:
    """Without the limit sensor nothing can be read back: a restart forgot the pause, and a resend after a
    blip or a regulator write would resume the charge behind it."""
    ids = register_shape(hass, SHAPES["easee"])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    async_mock_service(hass, "easee", "action_command")
    limits = async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    hass.states.async_set("sensor.easee_status", "charging")
    controller = await _controller(hass, config)
    await controller.async_stop()
    hass.states.async_set("sensor.easee_status", "awaiting_start")

    restarted = await _restart(hass, controller, config)

    assert await restarted.adapter.async_set_current(10, reason=WRITE_RESEND) == ASSIGN_IGNORED_WHILE_PAUSED
    assert limits == []
    await restarted.async_shutdown()
