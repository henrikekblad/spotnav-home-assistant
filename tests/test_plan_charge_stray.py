"""A charge a plan window of ours started is stopped when it runs outside every window of the
installed plan (replaced, restarted); a person's charge, solar's and a window's own charge are not."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution.controller import ChargingController

from .helpers import install_schedule


def _plan(start_h: float, end_h: float) -> dict[str, Any]:
    now = dt_util.utcnow()
    return {
        "start": (now + timedelta(hours=start_h)).isoformat(),
        "end": (now + timedelta(hours=end_h)).isoformat(),
        "amps": 10,
    }


async def _running_in_window(hass: HomeAssistant):
    """A charger whose own window (now-1h .. now+1h) started its charge, switch on."""
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    starts = async_mock_service(hass, "switch", "turn_on")
    stops = async_mock_service(hass, "switch", "turn_off")
    await install_schedule(controller, _plan(-1, 1))
    assert len(starts) == 1
    hass.states.async_set("switch.a", "on")
    await hass.async_block_till_done()
    assert stops == []
    return controller, stops


async def test_replacing_the_plan_with_one_that_excludes_now_stops_our_charge(hass: HomeAssistant) -> None:
    controller, stops = await _running_in_window(hass)

    await install_schedule(controller, _plan(10, 12))

    assert len(stops) == 1
    await controller.async_shutdown()


async def test_a_replacement_that_still_covers_now_keeps_the_charge(hass: HomeAssistant) -> None:
    controller, stops = await _running_in_window(hass)

    await install_schedule(controller, _plan(-0.5, 2))

    assert stops == []
    await controller.async_shutdown()


async def test_a_charge_that_strays_later_is_stopped_when_it_is_seen(hass: HomeAssistant) -> None:
    """The stop at the install could not be sent (the control was not readable): the next report does."""
    controller, stops = await _running_in_window(hass)
    hass.states.async_set("switch.a", "unavailable")
    await hass.async_block_till_done()
    await install_schedule(controller, _plan(10, 12))
    assert stops == []

    hass.states.async_set("switch.a", "on")
    await hass.async_block_till_done()

    assert len(stops) == 1
    await controller.async_shutdown()


async def test_a_restart_outside_every_window_stops_the_charge_we_started(hass: HomeAssistant) -> None:
    with freeze_time(dt_util.utcnow()) as clock:
        controller, stops = await _running_in_window(hass)
        await controller.async_shutdown()
        clock.tick(timedelta(hours=3))  # the window is over, HA restarts
        hass.states.async_remove("switch.a")  # the charger's entities are not there yet
        restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
        await restarted.async_initialize()
        assert stops == []

        hass.states.async_set("switch.a", "on")
        await hass.async_block_till_done()

        assert len(stops) == 1
        await restarted.async_shutdown()


async def test_a_restart_outside_every_window_leaves_a_charge_nobody_of_ours_started(
    hass: HomeAssistant,
) -> None:
    with freeze_time(dt_util.utcnow()) as clock:
        hass.states.async_set("switch.a", "off")
        controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
        await controller.async_initialize()
        stops = async_mock_service(hass, "switch", "turn_off")
        async_mock_service(hass, "switch", "turn_on")
        await install_schedule(controller, _plan(-1, 1))
        await controller.async_start(manual=True)  # the person starts it: no longer a plan charge
        hass.states.async_set("switch.a", "on")
        await hass.async_block_till_done()
        await controller.async_shutdown()
        clock.tick(timedelta(hours=3))
        hass.states.async_remove("switch.a")
        restarted = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
        await restarted.async_initialize()

        hass.states.async_set("switch.a", "on")
        await hass.async_block_till_done()

        assert stops == []
        await restarted.async_shutdown()


async def test_a_persons_start_outside_a_window_is_left_alone(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.a", "off")
    controller = ChargingController(hass, "entry_a", {CONF_CHARGE_CONTROL: "switch.a"})
    await controller.async_initialize()
    stops = async_mock_service(hass, "switch", "turn_off")
    async_mock_service(hass, "switch", "turn_on")
    await install_schedule(controller, _plan(10, 12))

    await controller.async_start(manual=True)
    hass.states.async_set("switch.a", "on")
    await hass.async_block_till_done()
    await install_schedule(controller, _plan(11, 13))

    stops.clear()  # the install re-arms outside a window and stops a running charge, as it always did
    hass.states.async_set("switch.a", "off")
    await hass.async_block_till_done()
    await controller.async_start(manual=True)
    hass.states.async_set("switch.a", "on")
    await hass.async_block_till_done()
    assert stops == []
    await controller.async_shutdown()


async def test_something_else_owning_the_charger_is_never_stopped_by_it(hass: HomeAssistant) -> None:
    controller, stops = await _running_in_window(hass)
    controller.set_hold_guard(lambda: True)  # solar or hybrid runs the charger
    controller.plan.periods = [
        {"start": (dt_util.utcnow() + timedelta(hours=10)).isoformat(),
         "end": (dt_util.utcnow() + timedelta(hours=12)).isoformat()}
    ]

    hass.states.async_set("switch.a", "unavailable")
    hass.states.async_set("switch.a", "on")
    await hass.async_block_till_done()

    assert stops == []
    await controller.async_shutdown()
