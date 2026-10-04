"""Every automatic path asks the execution boundary, under its lock, whether a pause forbids it at the
moment it acts: the window timers, the hold, the stray-charge stop, the claim, the plug-in start and the
regulator's resume.

The report's sequence (I8): a pause at 23:00 whose stop raised keeps the plan and its timers
(`pause_stop_failed`), and at 02:00 the window's start timer started the charge, since neither the hold
guard nor the pause was consulted on that path.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.spotnav.execution.auto_execution import EXECUTION_PAUSE_STOP_FAILED
from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED

from .pause_world import pause_world, two_windows
from .relay import FakeScheduler


def _failing_stops(world) -> None:
    async def failing() -> bool:
        raise HomeAssistantError("the charge control is unavailable")

    world.controller.adapter.async_stop = failing


async def test_a_pause_whose_stop_failed_leaves_no_window_able_to_start(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    assert world.controller.charging, "the first window charges"
    _failing_stops(world)

    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    assert world.executor.last_error == EXECUTION_PAUSE_STOP_FAILED
    assert world.controller.plan is not None, "the plan stays behind a stop that failed"

    await world.switch("off")  # the charge stopped some other way
    world.starts.clear()
    await world.fire("_async_start_callback")

    assert world.starts == [], "a paused charger's window does not start"
    await world.shutdown()


async def test_a_restart_inside_the_window_of_a_paused_plan_starts_nothing(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """The same failed pause, then Home Assistant restarts inside the plan's window: the re-arm of an
    open window is a window start like any other and is refused too."""
    world = await pause_world(hass, timers, plan=two_windows())
    _failing_stops(world)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    await world.switch("off")
    world.starts.clear()

    freezer.tick(2 * 3600 + 600)
    restarted = await world.restart()

    assert restarted.controller.plan is not None and restarted.controller.plan_window_active_now
    assert world.starts == []
    await restarted.shutdown()


@pytest.mark.parametrize("callback", ["_async_end_callback", "_async_final_end_callback"])
async def test_without_a_pause_the_window_timers_still_act(
    hass: HomeAssistant, timers: FakeScheduler, freezer, callback: str
) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    assert len(world.starts) == 1
    if callback == "_async_final_end_callback":
        freezer.tick(2 * 3600 + 60)
        await world.fire("_async_end_callback")
        await world.fire("_async_start_callback")
        assert len(world.starts) == 2
    world.stops.clear()

    await world.fire(callback)

    assert len(world.stops) == 1
    await world.shutdown()
