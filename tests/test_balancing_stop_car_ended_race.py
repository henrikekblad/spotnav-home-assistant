"""A look at the charge between load balancing's stop and its result never reads the car as ending the charge.

The noon of 2026-11-23: a person's Start, the house over the fuse, load balancing's stop. The charger turned off and
its connector said `SuspendedEV` before the stop's result came back to the core; only that result holds the charge
back (`balancing_paused`), so the watch of the person's charge (`ManualChargeWatch`) took the charger off for the car
ending the charge, ended the person's Start and recorded the car as done at 67 %. Load balancing's stop on its way
(the core's stop for it awaiting its result, or the controller sending it) is now its pause for both watches: the
person's and the plan's (`PlanChargeWatch`).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.spotnav.execution.plan_car_ended import PLAN_CAR_IDLE_S
from custom_components.spotnav.planning.auto_settings import MANUAL_START

from .pause_world import SWITCH, World, open_window, pause_world
from .relay import FakeScheduler


def _connector_says(monkeypatch, world: World, status: str) -> None:
    controller = world.controller
    real = controller.charge_progress_facts
    monkeypatch.setattr(controller, "charge_progress_facts", lambda: replace(real(), connector_status=status))


class _HeldStop:
    """`switch.turn_off` that does not return until released: the stop is on its way, its result held back.
    `report_off`: the charger reads off at once (its answer reached Home Assistant before the service returned)."""

    def __init__(self, hass: HomeAssistant, *, report_off: bool) -> None:
        self.hass = hass
        self.report_off = report_off
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        hass.services.async_register("switch", "turn_off", self._turn_off)

    async def _turn_off(self, call: ServiceCall) -> None:
        if self.report_off:
            current = self.hass.states.get(SWITCH)
            self.hass.states.async_set(SWITCH, "off", None if current is None else dict(current.attributes))
        self.entered.set()
        await self.release.wait()


async def _stop_held_back(hass: HomeAssistant, world: World, *, report_off: bool) -> tuple[_HeldStop, Any]:
    held = _HeldStop(hass, report_off=report_off)
    task = hass.async_create_task(world.controller._regulated_stop("pause"))  # noqa: SLF001
    await asyncio.wait_for(held.entered.wait(), 5)
    for _ in range(10):
        await asyncio.sleep(0)
    assert world.controller.balancing_stop_on_its_way
    assert not world.controller._runner.session.balancing_paused, "the core holds nothing back before the result"  # noqa: SLF001
    return held, task


async def test_a_persons_charge_stopped_by_load_balancing_is_not_ended_by_the_car_before_the_result(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch
) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert world.controller.charge_control_on and world.pause.action == MANUAL_START
    _connector_says(monkeypatch, world, "SuspendedEV")

    held, task = await _stop_held_back(hass, world, report_off=True)
    # The charger reads off and its connector says `SuspendedEV`: the listener's look, and one more.
    assert not world.controller.charge_control_on
    world.executor._look_at_manual_charge()  # noqa: SLF001
    assert not world.executor._manual_end_pending, "not read as the car ending the charge"  # noqa: SLF001

    held.release.set()
    await task
    await hass.async_block_till_done()
    world.executor._look_at_manual_charge()  # noqa: SLF001
    await hass.async_block_till_done()
    assert world.pause.action == MANUAL_START, "the person's Start holds"
    session = world.controller._runner.session  # noqa: SLF001
    assert session.balancing_paused and session.paused_origin == "person" and session.car_ended is None
    assert not world.controller.balancing_stop_on_its_way
    await world.shutdown()


async def test_the_plans_charge_stopped_by_load_balancing_is_not_ended_by_the_car_before_the_result(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, freezer
) -> None:
    world = await pause_world(hass, timers, plan=open_window())
    if not world.controller.charge_control_on:
        await world.fire("_async_start_callback")
    controller = world.controller
    assert controller.charge_control_on and controller._is_plan_charge  # noqa: SLF001
    # The car asks for no current: the plan's watch counts it idle from this look on.
    _connector_says(monkeypatch, world, "SuspendedEV")
    controller._look_at_plan_charge()  # noqa: SLF001
    freezer.tick(timedelta(seconds=PLAN_CAR_IDLE_S - 1))
    controller._look_at_plan_charge()  # noqa: SLF001

    # Load balancing's stop goes out; the charger has not answered it yet when the idle time is up.
    held, task = await _stop_held_back(hass, world, report_off=False)
    freezer.tick(timedelta(seconds=2))
    spawned: list[str] = []
    monkeypatch.setattr(controller, "_async_spawn", lambda coro, name: (spawned.append(name), coro.close()))
    controller._look_at_plan_charge()  # noqa: SLF001
    assert spawned == [], "not read as the car ending the plan's charge"

    held.release.set()
    await task
    await hass.async_block_till_done()
    session = controller._runner.session  # noqa: SLF001
    assert session.balancing_paused and session.paused_origin == "plan" and session.car_ended is None
    await world.shutdown()
