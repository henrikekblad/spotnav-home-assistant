"""The sequences the state machine review found untested: a plan replaced during a top-off, and two
timers firing in the same tick (a window's start and a pause's expiry)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.execution.auto_execution import AutoExecutor, EXECUTION_PAUSE_STOP_FAILED
from custom_components.spotnav.planning.auto_settings import PAUSE_NEXT_PERIOD, PauseIntent, async_setup_auto_settings

from .pause_world import pause_world, two_windows
from .test_top_off import _charging_at_the_window_end, _later, _plan


# ----------------------------------------------------------------- a plan replaced during a top-off


async def test_a_top_off_counts_as_a_window_charging_so_a_new_proposal_waits_for_its_end(
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, charger, end = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)
    assert controller.top_off_until is not None
    executor = AutoExecutor(hass, controller, await async_setup_auto_settings(hass))

    assert executor.window_charging_now(), "a reconcile makes a newer proposal wait, as for an open window"
    await executor.async_shutdown()
    await controller.async_shutdown()


async def test_a_plan_installed_during_a_top_off_with_a_window_open_now_keeps_the_charge(
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, charger, end = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)
    now = dt_util.utcnow()

    await controller.async_install(_plan(now + timedelta(minutes=30)))
    await hass.async_block_till_done()

    assert controller.top_off_until is None, "the new plan's windows decide from here"
    assert charger.calls == ["turn_on"], "no stop, and no second start of a charge that runs"
    assert controller.charge_origin == "plan_window"
    await controller.async_shutdown()


async def test_a_plan_installed_during_a_top_off_with_its_window_later_ends_the_charge_once(
    hass: HomeAssistant, freezer: Any
) -> None:
    controller, charger, end = await _charging_at_the_window_end(hass, freezer, to_vehicle_limit=True)

    await controller.async_install(_plan(dt_util.utcnow() + timedelta(hours=3)))
    await hass.async_block_till_done()
    await _later(hass, freezer, 120)

    assert controller.top_off_until is None
    assert charger.calls == ["turn_on", "turn_off"], "the plan's charge outside its new windows is stopped once"
    await controller.async_shutdown()


# ----------------------------------------------------------------------- two timers in one tick


async def test_a_window_start_and_a_pause_expiry_in_the_same_tick_start_the_charge_once(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A pause until the next period whose stop failed keeps its plan; its expiry is that period's start,
    so the window's own start timer and the expiry fire in one tick. Whichever acts first, the window
    starts exactly once."""
    world = await pause_world(hass, None, plan=two_windows())  # real timers
    assert world.controller.charging

    async def failing() -> bool:
        raise HomeAssistantError("the charge control is unavailable")

    original = world.controller.adapter.async_stop
    world.controller.adapter.async_stop = failing  # type: ignore[method-assign]
    await world.executor.async_pause(PAUSE_NEXT_PERIOD)
    assert world.executor.last_error == EXECUTION_PAUSE_STOP_FAILED
    world.controller.adapter.async_stop = original  # type: ignore[method-assign]
    expires_at = world.pause.expires_at
    second_start = world.controller.plan.windows[1][0]
    assert expires_at == second_start

    # The first window ends (a stop the pause agrees with), and the charger is off when the second opens.
    freezer.move_to(world.controller.plan.windows[0][1] + timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not world.controller.charging
    starts = len(world.starts)

    freezer.move_to(second_start + timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert world.pause == PauseIntent()
    assert len(world.starts) == starts + 1, "one start, whichever timer acted first"
    await world.shutdown()
