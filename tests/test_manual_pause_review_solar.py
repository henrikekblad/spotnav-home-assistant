"""Review: Auto resuming after the car ended a person's Start must not restart a full car (solar)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.planning.auto_settings import MANUAL_START, PauseIntent
from custom_components.spotnav.runtime import executor_for

from .test_manual_pause_solar import _solar_on
from .world import set_charger_delivered_a, set_site_power_w, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _run(hass: HomeAssistant, stale: bool) -> int:
    charger, site, controller, coordinator, clock, turn_on, turn_off = await _solar_on(hass)
    executor = executor_for(hass, charger.entry_id)
    await executor.async_manual_start()
    assert executor.pause_intent.action == MANUAL_START
    if stale:
        # What solar kept from a charge the car ended earlier in this plug-in, before the person's Start;
        # its retry has long passed. Solar is torn down while paused, so nothing clears it.
        controller._solar_car_ended = {  # noqa: SLF001
            "cause": "car_stopped",
            "retry_at": (dt_util.utcnow() - timedelta(hours=1)).isoformat(),
            "next_retry_s": 3600.0,
            "context": {
                "plugged_in_at": None if controller.plugged_in_at is None else controller.plugged_in_at.isoformat(),
                "soc_percent": None,
                "limit_percent": None,
                "target_percent": None,
            },
        }
    # The car is full: the charge ends by itself, and the watch ends the person's pause.
    hass.states.async_set(f"switch.{charger.entry_id}", "off")
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    await executor._async_manual_charge_ended()  # noqa: SLF001 - the watch's verdict
    assert executor.pause_intent == PauseIntent()
    before = len(turn_on)
    set_site_power_w(hass, "solar_site", -3000.0)
    for t in range(3000, 3400, 20):
        clock.value = float(t)
        await tick_site(hass, site)
    return len(turn_on) - before


async def test_control_no_stale_record(hass: HomeAssistant) -> None:
    assert await _run(hass, stale=False) == 0


async def test_stale_record_restarts_the_full_car(hass: HomeAssistant) -> None:
    assert await _run(hass, stale=True) == 0, "solar starts the car the watch just found full"
