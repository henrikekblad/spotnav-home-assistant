"""A Zaptec paused by SpotNav is resumed by its charge switch; one paused in the Zaptec app is left paused.

The Zaptec integration's charge switch reads on only while the car charges, and its turn-on is the charger's
"Resume charging" (a paused Zaptec takes no current until it is pressed). So SpotNav's start is that press, sent
whenever a window opens on a stopped charger, and a pause from outside inside a window is not fought.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_CHARGE_CONTROL

from .helpers import install_schedule
from .test_window_start_retry import (
    _after,
    _followed_starts,
    _set_currents,
    _shaped_with_config,
    _shut_down_controllers,  # noqa: F401 - the autouse fixture that shuts the controllers down
    _walk,
    _windows,
)

pytestmark = pytest.mark.usefixtures("offline_relay")


async def test_a_zaptec_spotnav_paused_is_resumed_at_the_next_window(hass: HomeAssistant, freezer: Any) -> None:
    controller, config = await _shaped_with_config(hass, "zaptec")
    status = controller.adapter.status_entity_id
    switch = config[CONF_CHARGE_CONTROL]
    calls = _followed_starts(hass)
    hass.states.async_set(status, "connected_requesting")
    hass.states.async_set(switch, "off")
    _set_currents(hass, config, 0)
    await install_schedule(controller, _windows((0, 30), (60, 90)))
    await _after(hass, freezer, 31)
    assert calls == ["start"], "the window's start is the Resume press"
    hass.states.async_set(status, "connected_charging")
    _set_currents(hass, config, 10)
    await _walk(hass, freezer, 30 * 60 + 30)
    assert calls[-1] == "stop", "the first window's end"
    hass.states.async_set(status, "connected_requesting")
    _set_currents(hass, config, 0)
    await _walk(hass, freezer, 31 * 60, step=60)
    assert calls.count("start") == 2, calls


async def test_a_zaptec_paused_in_its_app_inside_a_window_is_not_resumed(hass: HomeAssistant, freezer: Any) -> None:
    controller, config = await _shaped_with_config(hass, "zaptec")
    status = controller.adapter.status_entity_id
    switch = config[CONF_CHARGE_CONTROL]
    calls = _followed_starts(hass)
    hass.states.async_set(status, "connected_requesting")
    hass.states.async_set(switch, "off")
    _set_currents(hass, config, 0)
    await install_schedule(controller, _windows((0, 60)))
    await _after(hass, freezer, 31)
    hass.states.async_set(status, "connected_charging")
    _set_currents(hass, config, 10)
    await _walk(hass, freezer, 10 * 60)
    assert calls == ["start"]

    # The person pauses it in the Zaptec app: the switch reads off, the car draws nothing.
    hass.states.async_set(switch, "off")
    hass.states.async_set(status, "connected_requesting")
    _set_currents(hass, config, 0)
    await _walk(hass, freezer, 40 * 60, step=60)
    assert calls == ["start"], "SpotNav does not press Resume against the person's pause"


async def test_a_zaptec_has_no_phase_switching(hass: HomeAssistant) -> None:
    """SpotNav never switches a Zaptec's phases (its Go 2's "3 to 1 phase switch current" is the person's): no
    switch, so the sun's rules never wait for a phase reading to switch down, and the wiring setting says the phases."""
    controller, _config = await _shaped_with_config(hass, "zaptec")
    assert controller.adapter.phase_switch is None
