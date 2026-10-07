"""The car's minimum charge level (`execution/min_soc_floor.py`): below it SpotNav charges at once at the full current
set, under every strategy and through the plan's windows and the sun, but never through a person's pause or Stop; at
the floor the strategy takes over with no stop-start. The pure rules first, then one charger with its execution
boundary (`pause_world`), run in both ownership modes by the suite."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution.min_soc_floor import (
    decide_floor,
    effective_floor,
    FLOOR_END,
    FLOOR_NONE,
    FLOOR_RESTART_MARGIN_PERCENT,
    FLOOR_START,
    FloorFacts,
    FloorInputs,
    known_soc,
    MinSocFloor,
)
from custom_components.spotnav.planning.auto_settings import (
    PAUSE_UNTIL_RESUMED,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
)

from .pause_world import ENTRY, open_window, pause_world, SWITCH

T0 = datetime(2026, 10, 7, 18, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------------- pure


def test_the_floor_is_capped_at_the_target_and_the_cars_own_limit() -> None:
    assert effective_floor(None, target_percent=80, vehicle_max_percent=None) is None
    assert effective_floor(30, target_percent=None, vehicle_max_percent=None) == 30
    assert effective_floor(30, target_percent=80, vehicle_max_percent=90) == 30
    assert effective_floor(60, target_percent=50, vehicle_max_percent=None) == 50
    assert effective_floor(60, target_percent=None, vehicle_max_percent=55.5) == 55


def test_a_level_is_known_fresh_estimated_or_read_since_the_plug_in() -> None:
    plugged = T0 - timedelta(hours=2)
    assert known_soc(soc_percent=25, estimated=False, age_s=60, plugged_in_at=None, now=T0) == 25
    assert known_soc(soc_percent=25, estimated=True, age_s=7200, plugged_in_at=None, now=T0) == 25
    # Stale, but read while the car was plugged in: nothing was delivered since or it would be an estimate.
    assert known_soc(soc_percent=25, estimated=False, age_s=3600, plugged_in_at=plugged, now=T0) == 25
    # Stale and from before the plug-in (the car may have been driven since), or with no plug-in known.
    assert known_soc(soc_percent=25, estimated=False, age_s=3 * 3600, plugged_in_at=plugged, now=T0) is None
    assert known_soc(soc_percent=25, estimated=False, age_s=3600, plugged_in_at=None, now=T0) is None
    assert known_soc(soc_percent=None, estimated=False, age_s=10, plugged_in_at=plugged, now=T0) is None
    assert known_soc(soc_percent=25, estimated=False, age_s=None, plugged_in_at=plugged, now=T0) is None


BELOW = FloorFacts(floor_percent=30, soc_percent=20, connected=True, paused=False, origin=None)


@pytest.mark.parametrize(
    ("facts", "decision"),
    [
        (BELOW, FLOOR_START),
        (replace(BELOW, origin="plan_window"), FLOOR_START),
        (replace(BELOW, origin="solar"), FLOOR_START),
        (replace(BELOW, soc_percent=30), FLOOR_NONE),
        (replace(BELOW, soc_percent=45), FLOOR_NONE),
        (replace(BELOW, soc_percent=None), FLOOR_NONE),
        (replace(BELOW, floor_percent=None), FLOOR_NONE),
        (replace(BELOW, connected=False), FLOOR_NONE),
        (replace(BELOW, paused=True), FLOOR_NONE),
        (replace(BELOW, origin="manual"), FLOOR_NONE),
        (replace(BELOW, balancing_holds_floor=True), FLOOR_NONE),
        (replace(BELOW, retry_wait=True), FLOOR_NONE),
        # Ended at the floor in this plug-in: started again only clearly below it, never at a reading's wobble.
        (replace(BELOW, soc_percent=29, ended_at_floor=True), FLOOR_NONE),
        (replace(BELOW, soc_percent=30 - FLOOR_RESTART_MARGIN_PERCENT - 0.5, ended_at_floor=True), FLOOR_START),
        # The floor's own charge: kept below it, ended at it (floor + 0), when the level is gone, or under a pause.
        (replace(BELOW, origin="min_soc"), FLOOR_NONE),
        (replace(BELOW, origin="min_soc", soc_percent=30), FLOOR_END),
        (replace(BELOW, origin="min_soc", soc_percent=None), FLOOR_END),
        (replace(BELOW, origin="min_soc", floor_percent=None), FLOOR_END),
        (replace(BELOW, origin="min_soc", paused=True), FLOOR_END),
    ],
)
def test_the_floor_decision(facts: FloorFacts, decision: str) -> None:
    assert decide_floor(facts) == decision


# ---------------------------------------------------------------------------------------------- one charger


class Car:
    """What the floor reads about the car a charger plans for."""

    def __init__(self) -> None:
        self.inputs = FloorInputs(min_percent=30, target_percent=80, vehicle_max_percent=None, soc_percent=20.0,
                                  soc_estimated=False, soc_age_s=30.0)

    def __call__(self) -> FloorInputs:
        return self.inputs

    def at(self, percent: float | None, **changes) -> None:
        self.inputs = replace(self.inputs, soc_percent=percent, **changes)


async def _world(hass: HomeAssistant, timers, *, strategy: str | None = None, plan=None, charging=False):
    world = await pause_world(hass, timers, plan=plan, charging=charging)
    if strategy is not None:
        await world.store.async_update(ENTRY, mutate=lambda current: replace(current, strategy=strategy))
    car = Car()
    floor = MinSocFloor(hass, ENTRY, world.executor, world.store, car, interval=None)
    return world, car, floor


def _on(hass: HomeAssistant) -> bool:
    return hass.states.get(SWITCH).state == "on"


async def test_below_the_floor_it_charges_at_once_and_stops_at_the_floor_with_no_plan(
    hass: HomeAssistant, timers
) -> None:
    world, car, floor = await _world(hass, timers)

    await floor.async_evaluate()
    assert _on(hass) and world.controller.charge_origin == "min_soc"
    assert floor.charging_percent == 30

    car.at(29.9)
    await floor.async_evaluate()
    assert _on(hass), "below the floor it goes on"

    car.at(30.0)
    await floor.async_evaluate()
    await hass.async_block_till_done()
    assert not _on(hass) and world.controller.charge_origin is None
    assert floor.charging_percent is None
    await world.shutdown()


async def test_no_flap_at_the_floor_a_wobbling_reading_does_not_start_it_again(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    await floor.async_evaluate()
    car.at(30.0)
    await floor.async_evaluate()
    await hass.async_block_till_done()
    starts = len(world.starts)

    car.at(29.0)
    await floor.async_evaluate()
    assert len(world.starts) == starts and not _on(hass)

    car.at(30 - FLOOR_RESTART_MARGIN_PERCENT - 1)
    await floor.async_evaluate()
    assert _on(hass) and world.controller.charge_origin == "min_soc"
    await world.shutdown()


async def test_a_plan_window_open_now_takes_the_charge_over_at_the_floor(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers, plan=open_window(), charging=True)
    await hass.async_block_till_done()
    assert world.controller.charge_origin in ("plan_window", None)

    await floor.async_evaluate()
    assert _on(hass) and world.controller.charge_origin == "min_soc"
    stops = len(world.stops)

    car.at(31.0)
    await floor.async_evaluate()
    await hass.async_block_till_done()
    assert _on(hass) and world.controller.charge_origin == "plan_window"
    assert len(world.stops) == stops, "no stop-start at the floor"
    await world.shutdown()


async def test_the_windows_end_leaves_the_floor_charge_running(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers, plan=open_window(), charging=True)
    await floor.async_evaluate()
    assert world.controller.charge_origin == "min_soc"

    await world.fire("_async_final_end_callback")
    await hass.async_block_till_done()
    assert _on(hass) and world.controller.charge_origin == "min_soc"
    await world.shutdown()


@pytest.mark.parametrize("strategy", [STRATEGY_SOLAR, STRATEGY_HYBRID])
async def test_under_the_sun_it_charges_at_once_and_hands_the_charge_to_the_sun_at_the_floor(
    hass: HomeAssistant, timers, strategy: str
) -> None:
    world, car, floor = await _world(hass, timers, strategy=strategy)
    await floor.async_evaluate()
    assert _on(hass) and world.controller.charge_origin == "min_soc"
    # The sun does not stop it, nor turn its current down.
    assert await world.executor.async_solar_stop() is True
    await world.executor.async_solar_set_current(6)
    assert _on(hass) and world.controller.charge_origin == "min_soc"

    car.at(30.0)
    await floor.async_evaluate()
    await hass.async_block_till_done()
    assert _on(hass) and world.controller.charge_origin == "solar", "the sun decides from here"
    await world.shutdown()


async def test_a_persons_pause_wins_over_the_floor(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    await floor.async_evaluate()
    assert not _on(hass) and world.controller.charge_origin is None
    await world.shutdown()


async def test_a_pause_chosen_while_the_floor_charges_stops_it(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    await floor.async_evaluate()
    assert _on(hass)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    await floor.async_evaluate()
    await hass.async_block_till_done()
    assert not _on(hass) and world.controller.charge_origin is None
    await world.shutdown()


async def test_a_persons_stop_wins_over_the_floor(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    await floor.async_evaluate()
    assert _on(hass)
    await world.executor.async_manual_stop()
    await hass.async_block_till_done()
    await floor.async_evaluate()
    await hass.async_block_till_done()
    assert not _on(hass) and world.pause.manual
    await world.shutdown()


async def test_a_persons_start_is_theirs(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    await floor.async_evaluate()
    assert world.controller.charge_origin == "manual"
    car.at(50.0)
    await floor.async_evaluate()
    assert _on(hass) and world.controller.charge_origin == "manual"
    await world.shutdown()


@pytest.mark.parametrize(
    "changes",
    [
        {"soc_percent": None},
        {"soc_age_s": 3 * 3600.0},
        {"min_percent": None},
    ],
)
async def test_without_a_known_level_or_a_floor_nothing_starts(hass: HomeAssistant, timers, changes) -> None:
    world, car, floor = await _world(hass, timers)
    car.inputs = replace(car.inputs, **changes)
    await floor.async_evaluate()
    assert not _on(hass) and world.starts == []
    await world.shutdown()


async def test_an_estimated_level_counts(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    car.at(20.0, soc_estimated=True, soc_age_s=3 * 3600.0)
    await floor.async_evaluate()
    assert _on(hass) and world.controller.charge_origin == "min_soc"
    await world.shutdown()


async def test_no_car_starts_nothing(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    await world.plug.set(False)
    await floor.async_evaluate()
    assert not _on(hass) and world.starts == []
    await world.shutdown()


async def test_a_charge_that_ended_without_the_floor_is_tried_again_only_after_a_wait(
    hass: HomeAssistant, timers
) -> None:
    world, car, floor = await _world(hass, timers)
    await floor.async_evaluate()
    await world.switch("off")  # the car or the charger ended it, short of the floor
    await floor.async_evaluate()
    assert not _on(hass) and len(world.starts) == 1
    floor._now = lambda: dt_util.utcnow() + timedelta(minutes=10)  # noqa: SLF001
    await floor.async_evaluate()
    assert _on(hass) and len(world.starts) == 2
    await world.shutdown()


async def test_the_floor_charge_is_still_the_floors_after_a_restart(hass: HomeAssistant, timers) -> None:
    world, car, floor = await _world(hass, timers)
    await floor.async_evaluate()
    assert world.controller.charge_origin == "min_soc"

    world = await world.ha_restart()
    floor = MinSocFloor(hass, ENTRY, world.executor, world.store, car, interval=None)
    assert world.controller.charge_origin == "min_soc" and _on(hass)
    car.at(30.0)
    await floor.async_evaluate()
    await hass.async_block_till_done()
    assert not _on(hass) and world.controller.charge_origin is None
    await world.shutdown()
