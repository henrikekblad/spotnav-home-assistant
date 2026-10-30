"""Hardware quirks other projects found, replayed against SpotNav: what the controller and the site make of them.

* ABB Terra AC: a firmware self-reset puts the current limit back at 32 A (JernejHren/ABB-Terra-AC #7), so a charge
  SpotNav pauses at 0 A can start again by itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util

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
