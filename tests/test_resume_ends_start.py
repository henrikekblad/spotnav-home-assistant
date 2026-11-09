"""Resume (Återuppta) hands the charge back to the plan (2.5.1).

The field case of 2026-11-08: a person's Stop, then Start (which pauses Auto), then Resume. The plan was installed again
with its next window hours ahead, and the charge ran on as the person's, so the person had to press Stop, which paused
Auto again, and Resume once more. Now Resume is the person's newest word, "let the plan decide again": it ends their
Start. Outside every window of the plan the charge stops as the plan's own stop outside its windows, with no notice;
inside an open window it goes on as the plan's own charge. A Stop followed by Resume, and a charge that is not the
person's Start, are as before.

The pure rule first (`core/ownership._resume`), then one bare charger with its execution boundary, where the plan the
recalculation after Resume installs is installed by hand (`install_schedule`), as Auto would.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import decide, Keep
from custom_components.spotnav.core.session import (
    ChargeSession,
    ManualPause,
    OWNER_MIN_SOC,
    OWNER_NONE,
    OWNER_PERSON,
    OWNER_PLAN,
    OWNER_SOLAR,
)
from custom_components.spotnav.execution.auto_execution import ACTION_RESUME
from custom_components.spotnav.planning.auto_settings import PauseIntent

from .helpers import install_schedule
from .pause_world import later_window, open_window, pause_world, World
from .relay import FakeScheduler

pytestmark = pytest.mark.usefixtures("offline_relay")

T0 = datetime(2026, 11, 8, 22, 0, tzinfo=timezone.utc)
STARTED = ChargeSession(plugged=True, owner=OWNER_PERSON, manual=ManualPause("start", "plug_in"))


def _resume(session: ChargeSession, reason: str = ev.RESUME_PERSON) -> tuple[ChargeSession, list[str]]:
    after, commands = decide(session, ev.Resume(reason=reason), T0)
    return after, [command.kind for command in commands if not isinstance(command, Keep)]


# ------------------------------------------------------------------------------------------------- the rule


def test_resume_ends_a_persons_start_and_hands_its_charge_to_the_plan() -> None:
    after, commands = _resume(STARTED)
    assert after.manual is None
    assert after.owner == OWNER_PLAN, "the plan decides the charge from now on"
    assert commands == [], "nothing is sent here: the plan decides when it is armed or installed"


def test_resume_hands_a_persons_charge_load_balancing_holds_back_to_the_plan() -> None:
    held = STARTED.with_changes(owner=OWNER_NONE, balancing_paused=True, paused_origin=OWNER_PERSON)
    after, _ = _resume(held)
    assert after.balancing_paused and after.paused_origin == OWNER_PLAN


@pytest.mark.parametrize("owner", [OWNER_SOLAR, OWNER_MIN_SOC, "charge_now", OWNER_NONE])
def test_resume_leaves_a_charge_that_is_not_the_persons_start_as_it_is(owner: str) -> None:
    after, _ = _resume(STARTED.with_changes(owner=owner, manual=None))
    assert after.owner == owner


def test_follow_and_an_expired_pause_do_not_end_a_persons_start() -> None:
    assert _resume(STARTED, ev.RESUME_FOLLOW)[0].owner == OWNER_PERSON
    assert _resume(STARTED.with_changes(manual=None), ev.RESUME_EXPIRED)[0].owner == OWNER_PERSON


# ------------------------------------------------------------------------------------------------- the charger


async def _started_by_the_person(hass: HomeAssistant, timers: FakeScheduler, plan: dict) -> World:
    """A plan, then the person's Stop and Start (the night of 2026-11-08): the charge runs as theirs, Auto paused."""
    world = await pause_world(hass, timers, plan=plan)
    await world.executor.async_manual_stop()
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert world.controller.charging
    assert world.controller.session_runner.session.owner == OWNER_PERSON
    return world


def _notices(world: World) -> list[dict]:
    return [
        command
        for record in world.controller.session_runner.events
        for command in record.get("core", ())
        if command.get("kind") == "notify"
    ]


async def test_resume_outside_every_window_stops_the_persons_charge_as_the_plans_own(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await _started_by_the_person(hass, timers, later_window(hours=4))
    stops = len(world.stops)

    await world.executor.async_manual_action(ACTION_RESUME)
    assert world.pause == PauseIntent()
    # The recalculation the Resume starts installs the plan again, its next window hours ahead.
    await install_schedule(world.controller, later_window(hours=4))
    await hass.async_block_till_done()

    assert len(world.stops) == stops + 1, "stopped: the plan does not charge now"
    assert not world.controller.charging
    assert world.pause == PauseIntent(), "the plan's own stop pauses nothing"
    assert world.controller.plan is not None and world.controller.plan_expects_charge is False
    assert _notices(world) == [], "no notice: the plan stopped its own charge outside its windows"

    # The plan starts at its next window.
    starts = len(world.starts)
    await world.fire("_async_start_callback")
    assert len(world.starts) == starts + 1 and world.controller.charging
    await world.shutdown()


async def test_resume_inside_an_open_window_keeps_the_charge_as_the_plans(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await _started_by_the_person(hass, timers, open_window())
    stops = len(world.stops)

    await world.executor.async_manual_action(ACTION_RESUME)
    await install_schedule(world.controller, open_window())
    await hass.async_block_till_done()

    assert len(world.stops) == stops, "not stopped"
    assert world.controller.charging
    assert world.controller.session_runner.session.owner == OWNER_PLAN
    assert world.controller.charge_origin == "plan_window"
    # The window's end is the plan's: its own charge stops there.
    await world.fire("_async_final_end_callback")
    assert len(world.stops) == stops + 1
    await world.shutdown()


async def test_resume_after_a_persons_stop_is_as_before(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=later_window(hours=4))
    await world.executor.async_manual_stop()
    starts, stops = len(world.starts), len(world.stops)

    await world.executor.async_manual_action(ACTION_RESUME)
    await install_schedule(world.controller, later_window(hours=4))
    await hass.async_block_till_done()

    assert (len(world.starts), len(world.stops)) == (starts, stops), "nothing now"
    await world.fire("_async_start_callback")
    assert len(world.starts) == starts + 1, "the plan starts at its next window"
    await world.shutdown()


async def test_a_start_with_no_resume_after_it_runs_through_the_plan_as_before(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await _started_by_the_person(hass, timers, later_window(hours=4))
    stops = len(world.stops)

    # A plan installed while the person's Start holds (a Charge-now caller, a later calculation) stops nothing.
    await install_schedule(world.controller, later_window(hours=4))
    await hass.async_block_till_done()

    assert len(world.stops) == stops and world.controller.charging
    assert world.controller.session_runner.session.owner == OWNER_PERSON
    await world.shutdown()
