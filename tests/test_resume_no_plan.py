"""Resume with no plan after it stops the charge it handed to the plan (2.5.1).

A person's Resume hands their Start's charge to the plan (`tests/test_resume_ends_start.py`), and the plan decides it
when it is armed again or installed. The recalculation after Resume can end with no plan at all: prices missing, or
the need already met. Nothing was armed then, and the charge ran on as the plan's with no plan until the car was full.
Now, after a person's Resume and its recalculation, a plan charge with no plan in force is stopped as the plan's stray
(`Rearm` past its last window): no notice, no notification. The car's minimum level, the sun's charge, a Charge-now
start, a pause or the hybrid hand-off, and a charger that is not charging, are left alone; a plan that is installed
decides through its own re-arm, as before.

The whole stack first (real price repository, settings store, preview, execution boundary and charging controller), so
the check runs where Resume's recalculation ends, for the card's action and for the button alike. Then the rule as
the core decides it, and one bare charger with its execution boundary for what is left alone.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.ownership import decide, Keep
from custom_components.spotnav.core.session import (
    ChargeSession,
    OWNER_CHARGE_NOW,
    OWNER_MIN_SOC,
    OWNER_NONE,
    OWNER_PLAN,
    OWNER_SOLAR,
)
from custom_components.spotnav.execution.auto_execution import ACTION_RESUME, ACTION_START
from custom_components.spotnav.execution.min_soc_floor import FloorInputs, MinSocFloor
from custom_components.spotnav.planning.auto_controller import LiveVehicleFacts
from custom_components.spotnav.planning.auto_settings import DRIVER_TARGET_SOC, PauseIntent, TargetSocIntent

from .harness import Session
from .pause_world import ENTRY, pause_world, SWITCH, World
from .relay import Clock, FakeScheduler, SE4, StubTransport, TODAY, serve

pytestmark = pytest.mark.usefixtures("offline_relay")

T0 = datetime(2026, 11, 9, 22, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_real_clock(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    """Home Assistant's own clock pinned to the harness clock (the controller's plan rules read `dt_util.utcnow`)."""
    now = clock()
    monkeypatch.setattr(dt_util, "utcnow", lambda *_args, **_kwargs: now)


def _records(runner) -> list[dict]:
    return list(runner.events)


def _commands(runner) -> list[tuple[str, str | None]]:
    return [
        (command.get("kind"), command.get("reason"))
        for record in _records(runner)
        for command in record.get("core", ())
    ]


def _notices(runner) -> list[tuple[str, str | None]]:
    return [command for command in _commands(runner) if command[0] == "notify"]


async def _person_started(session: Session) -> None:
    """The person's Start, the charger reporting the charge: Auto paused for the plug-in, the charge theirs."""
    await session.preview.async_manual_action(ACTION_START)
    session.hass.states.async_set(session.charge_control, "on")
    await session.hass.async_block_till_done()
    assert session.controller.charging
    assert session.controller.session_runner.session.owner == "person"


# ------------------------------------------------------------------------------------------- the whole stack


async def test_resume_with_no_prices_stops_the_charge_as_the_plans_stray(session: Session) -> None:
    serve(session.transport)
    session.transport.serve(session.transport.day_path(SE4, TODAY), 500, "boom")
    assert (await session.set_auto()).proposal is None
    await _person_started(session)
    offs = len(session.off_calls)

    await session.preview.async_manual_action(ACTION_RESUME)
    await session.hass.async_block_till_done()

    assert session.controller.plan is None, "the recalculation installed no plan"
    assert len(session.off_calls) == offs + 1, "stopped: no plan runs the charge it was handed"
    assert ("stop", "stray") in _commands(session.controller.session_runner)
    assert _notices(session.controller.session_runner) == [], "no notice: the plan's own stop"
    assert session.settings().pause == PauseIntent(), "the plan's stop pauses nothing"
    assert session.controller.session_runner.session.owner == OWNER_NONE


async def test_the_button_s_resume_does_the_same(session: Session) -> None:
    serve(session.transport)
    session.transport.serve(session.transport.day_path(SE4, TODAY), 500, "boom")
    await session.set_auto()
    await _person_started(session)
    offs = len(session.off_calls)

    await session.preview.async_resume()
    await session.hass.async_block_till_done()

    assert len(session.off_calls) == offs + 1
    assert _notices(session.controller.session_runner) == []


async def test_resume_with_the_need_met_stops_the_charge_as_the_plans_stray(
    hass: HomeAssistant, transport: StubTransport, clock: Clock, timers: FakeScheduler
) -> None:
    session = Session(hass, transport, clock, timers)
    # The car is already past its target: the answer is nothing to charge, with no plan.
    session.vehicle_reader = lambda vehicle_id: LiveVehicleFacts(
        vehicle_id=vehicle_id, soc_percent=85.0, reported_capacity_kwh=60.0
    )
    await session.start()
    serve(session.transport)
    await session.set_auto(driver=DRIVER_TARGET_SOC, target=TargetSocIntent(vehicle_id="car-1", target_percent=80.0))
    await _person_started(session)
    offs = len(session.off_calls)

    await session.preview.async_manual_action(ACTION_RESUME)
    await hass.async_block_till_done()

    assert session.preview.snapshot().state == "nothing_to_charge"
    assert session.controller.plan is None
    assert len(session.off_calls) == offs + 1
    commands = _commands(session.controller.session_runner)
    # The need-met path ends a plan in force only; the person's Start had dropped it, so the stray rule stops it.
    assert ("stop", "stray") in commands and ("stop", "need_met") not in commands
    assert _notices(session.controller.session_runner) == []


async def test_a_failing_stray_stop_never_fails_the_committed_resume(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(session.transport)
    session.transport.serve(session.transport.day_path(SE4, TODAY), 500, "boom")
    await session.set_auto()
    await _person_started(session)

    async def broken() -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(session.controller, "async_stop_stray_without_plan", broken)
    await session.preview.async_manual_action(ACTION_RESUME)
    await session.preview.async_resume()

    assert session.settings().pause == PauseIntent(), "the resume stands"


# ------------------------------------------------------------------------------------------------- the rule


def _rearm(session: ChargeSession, **facts) -> list[tuple[str, str]]:
    facts.setdefault("control_on", True)
    _, commands = decide(session, ev.Rearm(past_last=True, **facts), T0)
    return [(command.kind, command.reason) for command in commands if not isinstance(command, Keep)]


def test_a_plan_charge_with_no_plan_is_the_plans_stray() -> None:
    assert _rearm(ChargeSession(plugged=True, owner=OWNER_PLAN)) == [("stop", "stray")]


@pytest.mark.parametrize("owner", [OWNER_MIN_SOC, OWNER_SOLAR, OWNER_CHARGE_NOW, OWNER_NONE])
def test_a_charge_the_plan_does_not_own_goes_on(owner: str) -> None:
    assert _rearm(ChargeSession(plugged=True, owner=owner)) == []


@pytest.mark.parametrize("facts", [{"solar_holds": True}, {"handed_off": True}, {"control_on": False}])
def test_the_sun_the_hybrid_hand_off_and_a_charger_seen_off_spare_it(facts: dict) -> None:
    assert _rearm(ChargeSession(plugged=True, owner=OWNER_PLAN), **facts) == []


# ------------------------------------------------------------------------------------------- one bare charger


class _Car:
    def __init__(self) -> None:
        self.inputs = FloorInputs(
            min_percent=30, target_percent=80, vehicle_max_percent=None, soc_percent=20.0, soc_estimated=False,
            soc_age_s=30.0,
        )

    def __call__(self) -> FloorInputs:
        return self.inputs


async def test_the_cars_minimum_level_is_not_stopped(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    floor = MinSocFloor(hass, ENTRY, world.executor, world.store, _Car(), interval=None)
    await floor.async_evaluate()
    assert world.controller.charge_origin == "min_soc" and world.controller.charging
    stops = len(world.stops)

    # The Resume button (the card offers no Resume with nothing paused).
    await world.executor.async_resume()
    # What the preview does once Resume's recalculation has installed nothing.
    assert await world.controller.async_stop_stray_without_plan() is False

    assert len(world.stops) == stops and world.controller.charging
    assert world.controller.charge_origin == "min_soc"
    await world.shutdown()


async def test_with_no_charge_running_nothing_is_sent(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    await world.switch("off")
    assert not world.controller.charging
    starts, stops = len(world.starts), len(world.stops)
    events = len(_records(world.controller.session_runner))

    await world.executor.async_manual_action(ACTION_RESUME)
    resumed = len(_records(world.controller.session_runner))
    assert await world.controller.async_stop_stray_without_plan() is False

    assert (len(world.starts), len(world.stops)) == (starts, stops)
    assert len(_records(world.controller.session_runner)) == resumed > events, "no re-arm fed: nothing to decide"
    await world.shutdown()


async def test_a_plan_in_force_is_left_to_its_own_re_arm(hass: HomeAssistant, timers: FakeScheduler) -> None:
    from .helpers import install_schedule
    from .pause_world import open_window

    world: World = await pause_world(hass, timers)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    await world.executor.async_manual_action(ACTION_RESUME)
    await install_schedule(world.controller, open_window())
    await hass.async_block_till_done()
    stops = len(world.stops)

    assert await world.controller.async_stop_stray_without_plan() is False

    assert len(world.stops) == stops and world.controller.charging
    assert hass.states.get(SWITCH).state == "on"
    await world.shutdown()
