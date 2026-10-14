"""The car ends a plan's charge short of the target on a level nobody read: the plan waits for the car's level.

The field night of 13-14 October: an OCPP HALO and a Kia EV6 whose cloud last reported 43 %, a target of 91 %. The car
took what it would and stopped at 04:35 (`SuspendedEV`), full at 100 %; SpotNav's estimate from the delivered energy
said 86.2 %. Below the target, the old rule skipped only the window open then, and the next window (05:30-06:00)
started a car that drew nothing while the status said "charging now".

Now no window of the plan starts the car again until its level is read after the end. A reading below the target
(by more than the margin a car that ended a charge may read short of its goal) charges it as planned; one at the
target ends the plan; an unplug or a plug-in, a target raised above the one it ended at, or a person's Start ends
the wait. The end is told once, on the estimate.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.target_stop import SocReading

from .helpers import install_schedule
from .pause_world import pause_world, two_windows, World
from .relay import FakeScheduler

TARGET = 91.0


class Car:
    """The car's level as SpotNav reads it: `read_at` is when it was made (an estimate's is its anchor's)."""

    def __init__(self, percent: float, *, estimated: bool, read_at: Any) -> None:
        self.percent, self.estimated, self.read_at = percent, estimated, read_at

    def __call__(self, _vehicle: str | None) -> SocReading:
        age = (dt_util.utcnow() - self.read_at).total_seconds()
        return SocReading(self.percent, "vehicle", "sensor.ev6", None, age, estimated=self.estimated)

    def reads(self, percent: float) -> None:
        """A new reading from the car's cloud, now."""
        self.percent, self.estimated, self.read_at = percent, False, dt_util.utcnow()


async def _ended(hass: HomeAssistant, timers: FakeScheduler, *, car: Car, target: float = TARGET) -> World:
    world = await pause_world(hass, timers)
    world.controller._soc_reader = car  # noqa: SLF001
    world.controller._car_drawing = lambda: False  # type: ignore[method-assign]  # noqa: SLF001
    await install_schedule(world.controller, {**two_windows(), "target_soc_percent": target})
    await hass.async_block_till_done()
    assert world.controller.charging, "the open window started the plan's charge"
    await world.controller._async_plan_car_ended()  # noqa: SLF001 - the watch's five minutes at SuspendedEV
    await hass.async_block_till_done()
    return world


def _estimate(percent: float = 86.2) -> Car:
    # 43 % read last night, carried forward by the delivered energy.
    return Car(percent, estimated=True, read_at=dt_util.utcnow() - timedelta(hours=6))


async def _next_window(world: World, freezer: Any) -> None:
    freezer.tick(timedelta(hours=2, minutes=1))
    await world.fire("_async_start_callback")


async def test_no_later_window_starts_a_car_that_ended_the_charge_below_the_target_by_the_estimate(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    world = await _ended(hass, timers, car=_estimate())
    controller = world.controller

    assert controller.car_ended_wait == {"soc_percent": 86.2, "estimated": True, "target_percent": TARGET}
    assert controller.plan_expects_charge is False, "no plan_stopped for a window the car is not started in"
    completion = controller.completion_record
    assert completion is not None
    assert (completion["reason"], completion["soc_percent"], completion["basis"]) == ("target", 86.2, "estimate")

    await world.switch("off")  # the open window ends
    starts = len(world.starts)
    await _next_window(world, freezer)

    assert len(world.starts) == starts, "a later window started a car that took nothing more"
    assert controller.plan is not None, "the plan holds, waiting for the car's level"
    assert controller.car_ended_wait is not None
    await world.shutdown()


async def test_a_reading_below_the_target_after_the_end_charges_the_car_as_planned(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    car = _estimate()
    world = await _ended(hass, timers, car=car)
    await world.switch("off")
    freezer.tick(timedelta(minutes=30))
    car.reads(80.0)
    assert world.controller.car_ended_wait is None, "the car's level is known again"

    starts = len(world.starts)
    await _next_window(world, freezer)

    assert len(world.starts) == starts + 1
    await world.shutdown()


async def test_a_reading_within_the_margin_below_the_target_still_waits_and_ends_the_plan(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    """A car read at 90.5 % after it ended a charge to 91 % is full for that charge (a reading rounds): the plan
    ends there, told with the car's own level."""
    car = _estimate()
    world = await _ended(hass, timers, car=car)
    await world.switch("off")
    freezer.tick(timedelta(minutes=30))
    car.reads(90.5)

    await world.controller._async_enforce_target()  # noqa: SLF001 - the reading's listener
    await hass.async_block_till_done()

    assert world.controller.plan is None
    completion = world.controller.completion_record
    assert completion is not None and (completion["soc_percent"], completion["basis"]) == (90.5, "reading")
    record = world.controller.target_stop_record
    assert record is not None and record["car_ended"] is True
    await world.shutdown()


async def test_a_reading_at_the_target_after_the_end_ends_the_plan_complete(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    car = _estimate()
    world = await _ended(hass, timers, car=car)
    await world.switch("off")
    freezer.tick(timedelta(hours=1))
    car.reads(100.0)

    await world.controller._async_enforce_target()  # noqa: SLF001
    await hass.async_block_till_done()

    assert world.controller.plan is None
    assert world.controller.car_ended_wait is None
    completion = world.controller.completion_record
    assert completion is not None and completion["soc_percent"] == 100.0
    starts = len(world.starts)
    await _next_window(world, freezer)
    assert len(world.starts) == starts
    await world.shutdown()


async def test_a_raised_target_charges_the_car_again(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    world = await _ended(hass, timers, car=_estimate())
    await world.switch("off")
    assert world.controller.car_ended_waits_for(target_percent=TARGET, vehicle_id=None)
    assert not world.controller.car_ended_waits_for(target_percent=95.0, vehicle_id=None)

    plan = two_windows()
    await install_schedule(world.controller, {**plan, "periods": plan["periods"][1:], "target_soc_percent": 95.0})
    starts = len(world.starts)
    await _next_window(world, freezer)

    assert len(world.starts) == starts + 1
    await world.shutdown()


async def test_a_plug_in_again_ends_the_wait(hass: HomeAssistant, timers: FakeScheduler, freezer: Any) -> None:
    world = await _ended(hass, timers, car=_estimate())
    await world.switch("off")
    await world.plug.set(False)
    await world.plug.set(True)

    assert world.controller.car_ended_wait is None
    assert not world.controller.car_ended_waits_for(target_percent=TARGET, vehicle_id=None)
    await world.shutdown()


async def test_a_persons_start_ends_the_wait(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await _ended(hass, timers, car=_estimate())
    await world.switch("off")

    await world.executor.async_manual_start()

    assert world.controller.car_ended_wait is None
    await world.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_the_wait_survives_a_restart(hass: HomeAssistant, timers: FakeScheduler, freezer: Any) -> None:
    car = _estimate()
    world = await _ended(hass, timers, car=car)
    await world.switch("off")

    restarted = await world.restart()
    restarted.controller._soc_reader = car  # noqa: SLF001
    restarted.controller._car_drawing = lambda: False  # type: ignore[method-assign]  # noqa: SLF001
    starts = len(world.starts)
    await _next_window(restarted, freezer)

    assert len(world.starts) == starts
    assert restarted.controller.car_ended_wait == {"soc_percent": 86.2, "estimated": True, "target_percent": TARGET}
    await restarted.shutdown()


async def test_a_fresh_reading_below_the_target_at_the_end_keeps_the_old_rule(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    """A car read just now below the target stopped for a reason of its own (a timer, a preconditioning pause): only
    the window open then is skipped, and a later one charges it as planned."""
    car = Car(70.0, estimated=False, read_at=dt_util.utcnow() - timedelta(seconds=30))
    world = await _ended(hass, timers, car=car)
    assert world.controller.car_ended_wait is None
    await world.switch("off")

    starts = len(world.starts)
    await _next_window(world, freezer)

    assert len(world.starts) == starts + 1
    await world.shutdown()


async def test_an_older_reading_below_the_target_waits_too_and_is_shown_as_read(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any
) -> None:
    """No energy register to carry the reading forward: the car's last report, from before the charge, is the level
    it ended at as far as anyone knows. Nothing is told as the charge's end on it."""
    car = Car(43.0, estimated=False, read_at=dt_util.utcnow() - timedelta(hours=6))
    world = await _ended(hass, timers, car=car)

    assert world.controller.car_ended_wait == {"soc_percent": 43.0, "estimated": False, "target_percent": TARGET}
    assert world.controller.completion_record is None
    await world.switch("off")
    starts = len(world.starts)
    await _next_window(world, freezer)
    assert len(world.starts) == starts
    await world.shutdown()
