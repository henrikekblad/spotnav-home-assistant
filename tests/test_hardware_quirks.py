"""Hardware quirks other projects found, replayed against SpotNav: what the controller and the site make of them.

* ABB Terra AC: a firmware self-reset puts the current limit back at 32 A (JernejHren/ABB-Terra-AC #7), so a charge
  SpotNav pauses at 0 A can start again by itself.
* GARO: with Always off and a car connected, the status reads UNAVAILABLE instead of CHARGING_PAUSED
  (sockless-coding/garo_wallbox #61).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .helpers import install_schedule
from .test_charger_controller_paths import _controller

pytestmark = pytest.mark.usefixtures("offline_relay")

ABB_LIMIT = "number.abb_terra_ac_current_limit"
ABB_STATUS = "sensor.abb_terra_ac_charging_state"
ABB_ATTRS = {"unit_of_measurement": "A", "min": 0, "max": 32, "step": 1}


def _later_window() -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=2)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": 10}


def _obedient_abb(hass: HomeAssistant) -> list[float]:
    """`number.set_value` on the ABB's limit, recorded, with the number following it as the charger does."""
    written: list[float] = []

    async def handle(call: ServiceCall) -> None:
        written.append(call.data["value"])
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]), ABB_ATTRS)

    hass.services.async_register("number", "set_value", handle)
    hass.services.async_register("button", "press", lambda call: None)
    return written


async def _abb_reset(hass: HomeAssistant) -> None:
    """The firmware resets: the limit is 32 A again and the car charges."""
    hass.states.async_set(ABB_LIMIT, "32", ABB_ATTRS)
    hass.states.async_set(ABB_STATUS, "State C2 - Charging")
    await hass.async_block_till_done()


async def test_an_abb_that_resets_while_paused_outside_the_windows_is_paused_again_once(hass: HomeAssistant) -> None:
    """The start button reads `unknown` until it is first pressed (most ABBs need no authorization), so the charge is
    read on the limit: paused at 0 A is off, and the reset's 32 A a charge that started by itself, held once. A second
    one in the same plug-in cannot be told from a person raising the limit, and is left to run as their start."""
    controller = await _controller(hass, "abb_terra_ac")
    written = _obedient_abb(hass)
    hass.states.async_set(ABB_LIMIT, "0", ABB_ATTRS)
    hass.states.async_set(ABB_STATUS, "State B2 - EV Plug in, charging complete")
    await hass.async_block_till_done()
    await install_schedule(controller, _later_window())
    assert hass.states.get("button.abb_terra_ac_start_charging").state == "unknown"
    assert controller.commands_owed is False

    await _abb_reset(hass)

    assert written == [0], "paused again at once"
    hass.states.async_set(ABB_STATUS, "State B2 - EV Plug in, charging complete")
    await hass.async_block_till_done()

    await _abb_reset(hass)

    assert written == [0], "a second start in the plug-in is a person's"
    await controller.async_shutdown()


GARO_MODE = "select.garo_wallbox_sensor"
GARO_STATUS = "sensor.garo_wallbox_status"


def _obedient_garo(hass: HomeAssistant) -> list[str]:
    """`select.select_option` on the GARO's mode, recorded, with the select following it."""
    chosen: list[str] = []

    async def handle(call: ServiceCall) -> None:
        chosen.append(call.data["option"])
        current = hass.states.get(call.data["entity_id"])
        hass.states.async_set(call.data["entity_id"], call.data["option"], None if current is None else current.attributes)

    hass.services.async_register("select", "select_option", handle)
    return chosen


@pytest.mark.parametrize("status", ["UNAVAILABLE", "unavailable"])
async def test_a_garo_whose_status_reads_unavailable_once_stopped_keeps_its_car_and_is_stopped_once(
    hass: HomeAssistant, freezer, status: str
) -> None:
    """Stopped with Always off and a car connected, the status reads UNAVAILABLE (whether the text or Home Assistant's
    own state): it says nothing, so the car is not taken as gone, the stop is not repeated, the card names no
    connection, and the next window starts the charge as on any stopped charger."""
    controller = await _controller(hass, "garo_wallbox")
    chosen = _obedient_garo(hass)
    heard: list[str] = []
    controller.set_connection_handler(lambda event: heard.append(event) or True)
    hass.states.async_set(GARO_STATUS, "CHARGING")
    await hass.async_block_till_done()
    assert controller.adapter.vehicle_connected() is True

    await controller.async_stop()
    hass.states.async_set(GARO_STATUS, status)
    await hass.async_block_till_done()
    for _ in range(10):
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert chosen == ["ALWAYS_OFF"]
    assert controller.charging is False and controller.commands_owed is False
    assert controller.adapter.vehicle_connected() is None
    assert heard == []
    assert controller.connection() == ("unknown", GARO_STATUS)

    now = dt_util.utcnow()
    await install_schedule(
        controller, {"start": (now - timedelta(minutes=5)).isoformat(), "end": (now + timedelta(hours=1)).isoformat(), "amps": 10}
    )
    await hass.async_block_till_done()

    assert chosen == ["ALWAYS_OFF", "ALWAYS_ON"]
    await controller.async_shutdown()
