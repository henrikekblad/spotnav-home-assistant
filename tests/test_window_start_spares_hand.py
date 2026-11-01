"""A plan window that opens while a charge started by hand runs (a person's Start, Charge now) leaves that charge alone:
it stays the person's, nothing is sent at the window's start, and the window's end does not stop it (owner decision
2026-11-01, as docs/strategies.md and docs/card.md promise). Also across a restart before the window opens, in both
restart kinds, where the restart reads the owner from the core's own record alone."""

from __future__ import annotations

from datetime import timedelta

from freezegun import freeze_time
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .pause_world import later_window, pause_world, SWITCH, World
from .relay import FakeScheduler

pytestmark = pytest.mark.usefixtures("offline_relay")

ORIGINS = {"person": "manual", "charge_now": "other"}


async def _started_by_hand(world: World, owner: str) -> None:
    if owner == "person":
        await world.executor.async_manual_start()
    else:
        # Charge now: a start through the charger's own entry that names no cause.
        assert await world.controller.async_start(cause=None)
    await world.hass.async_block_till_done()
    assert world.hass.states.get(SWITCH).state == "on"
    assert world.controller.charge_origin == ORIGINS[owner]


async def _window_passes(world: World, plan: dict[str, str]) -> None:
    """The window's start and its end fire; neither touches the charge."""
    starts, stops = len(world.starts), len(world.stops)
    with freeze_time(dt_util.parse_datetime(plan["start"]) + timedelta(seconds=1)):
        await world.fire("_async_start_callback")
    assert (len(world.starts), len(world.stops)) == (starts, stops), "nothing is sent at the window's start"
    assert world.controller.charge_origin in ORIGINS.values() and not world.controller._is_plan_charge  # noqa: SLF001
    with freeze_time(dt_util.parse_datetime(plan["end"]) + timedelta(seconds=1)):
        await world.fire("_async_final_end_callback")
    assert len(world.stops) == stops, "the window's end does not stop a charge started by hand"
    assert world.hass.states.get(SWITCH).state == "on"


@pytest.mark.parametrize("owner", list(ORIGINS))
async def test_a_window_opening_during_a_charge_started_by_hand_leaves_it_the_persons(
    hass: HomeAssistant, timers: FakeScheduler, owner: str
) -> None:
    plan = later_window()
    world = await pause_world(hass, timers, plan=plan)
    await _started_by_hand(world, owner)
    await _window_passes(world, plan)
    assert world.controller.charge_origin == ORIGINS[owner]
    assert world.controller.session_runner.session.owner == owner
    await world.shutdown()


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("owner", list(ORIGINS))
async def test_a_restart_before_the_window_keeps_a_charge_started_by_hand_the_persons(
    hass: HomeAssistant, timers: FakeScheduler, owner: str
) -> None:
    plan = later_window()
    world = await pause_world(hass, timers, plan=plan)
    await _started_by_hand(world, owner)
    restarted = await world.restart()
    assert restarted.controller.charge_origin == ORIGINS[owner]
    await _window_passes(restarted, plan)
    assert restarted.controller.session_runner.session.owner == owner
    await restarted.shutdown()
