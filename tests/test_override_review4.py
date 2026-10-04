"""Review E: adversarial review of 3745725 (per-charger operations, stop under `_assign_lock`, R5)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError


pytestmark = pytest.mark.usefixtures("offline_relay")


def _lower(amps: float) -> Any:
    from custom_components.spotnav.site.regulator import RegulatorDecision

    return RegulatorDecision(
        proposed_current_a=amps, reason="reducing_current_due_to_active_import_overload", limiting_phase=None
    )


async def test_e2_r5_a_stop_that_fails_on_its_way_still_counts_towards_giving_up(
    hass: HomeAssistant, timers: Any, freezer: Any
) -> None:
    """E2: R5 says every stop sent under a person's Stop counts, taken or not. A stop whose service call
    raises (a cloud that times out after the command left, and the charger did pause) is not counted
    (`_person_hold_stop_locked` counts only what `_automatic_stop_locked` says went out), so a charger that
    begins again after each such stop is stopped every 30 s for ever and SpotNav never gives up or says so."""
    from .pause_world import pause_world

    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    calls: list[Any] = []

    async def failing(call: Any) -> None:
        calls.append(call)
        raise HomeAssistantError("timed out")

    hass.services.async_register("switch", "turn_off", failing)
    for _ in range(8):
        freezer.tick(timedelta(seconds=40))
        await world.switch("off")
        await world.switch("on")
        await hass.async_block_till_done()
    sent = len(calls)
    gave_up = world.controller.ignores_person_stop
    await world.shutdown()
    assert sent <= 3 and gave_up, f"{sent} stops in under six minutes, given up: {gave_up}"
