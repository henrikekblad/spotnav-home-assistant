"""A changed charging current ("Laddström", the settings' `amps`) reaches the charge that runs, and a person's Start.

The report (2026-11-02, 1.22.0, a Charge Amps charger written through its `number.*_max_current`): the plan was
made at 16 A, the person lowered the setting to 11 A while tomorrow's prices were awaited (so no new plan was made),
and the charger stayed at 16 A; a Stop and a Start then started it at 16 A again, from the stale plan.

The rule: the current the person set is the charge's current. A change of it is written to a charge SpotNav runs (a
plan's, a top-off's, a person's, a Charge-now one), capped by load balancing as every start is and through the
charger's own write policy (its least interval between writes, a flash-stored setting never rewritten); a write the
policy holds back goes out once it allows. Every start that names no current starts at it. The sun's charges write
their own current, under the setting as their ceiling, and are left alone.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.util import dt as dt_util

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CURRENT_CONTROL_NUMBER,
)
from custom_components.spotnav.execution.auto_execution import AutoExecutor
from custom_components.spotnav.execution.charger_profiles import WritePolicy
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.planning.auto_settings import async_setup_auto_settings, AutoSettingsStore

from .charger_helpers import Clock
from .helpers import install_schedule
from .relay import FakeScheduler
from .test_replug import _obedient_switch, Plug

SWITCH = "switch.a"
NUMBER = "number.a_max_current"
ENTRY = "entry_a"


class World:
    def __init__(
        self,
        hass: HomeAssistant,
        controller: ChargingController,
        executor: AutoExecutor,
        store: AutoSettingsStore,
        timers: FakeScheduler,
        clock: Clock,
        writes: list[float],
    ) -> None:
        self.hass, self.controller, self.executor, self.store = hass, controller, executor, store
        self.timers, self.clock, self.writes = timers, clock, writes

    async def set_amps(self, amps: int) -> None:
        """The person sets the charging current, as the card and the number entity do (through the boundary)."""
        await self.executor.async_update_settings(mutate=lambda current: replace(current, amps=amps))
        await self.hass.async_block_till_done()

    async def fire(self, name: str) -> None:
        for timer in list(self.timers.pending):
            if getattr(timer.action, "__name__", "") == name:
                timer.fire()
        await self.hass.async_block_till_done()

    async def shutdown(self) -> None:
        await self.executor.async_shutdown()
        await self.controller.async_shutdown()


async def _world(hass: HomeAssistant, timers: FakeScheduler, *, amps: int = 16) -> World:
    """A plain switch charger whose current is a number (the policy's least interval between writes: 90 s), with
    its settings store at `amps`, its execution boundary and a car plugged in."""
    hass.states.async_set(SWITCH, "off")
    hass.states.async_set(NUMBER, "32", {"unit_of_measurement": "A", "min": 6, "max": 32})
    _obedient_switch(hass)
    writes: list[float] = []

    async def set_value(call: ServiceCall) -> None:
        writes.append(float(call.data["value"]))
        hass.states.async_set(NUMBER, str(call.data["value"]), hass.states.get(NUMBER).attributes)

    hass.services.async_register("number", "set_value", set_value)
    store = await async_setup_auto_settings(hass)
    await store.async_update(ENTRY, mutate=lambda current: replace(current, amps=amps))
    controller = ChargingController(
        hass,
        ENTRY,
        {CONF_CHARGE_CONTROL: SWITCH, CONF_CURRENT_LIMIT: NUMBER, CONF_CURRENT_CONTROL: CURRENT_CONTROL_NUMBER},
    )
    clock = Clock()
    controller.adapter.current._limiter._now = clock  # noqa: SLF001 - the policy's clock, moved by hand
    executor = AutoExecutor(hass, controller, store)
    await executor.async_start()
    await controller.async_initialize()
    await executor.async_after_restore()
    plug = Plug(hass, controller, SWITCH)
    await plug.set(False)
    await plug.set(True)
    await hass.async_block_till_done()
    return World(hass, controller, executor, store, timers, clock, writes)


def _open_window(amps: int = 16) -> dict[str, Any]:
    now = dt_util.utcnow()
    return {
        "start": (now - timedelta(minutes=10)).isoformat(),
        "end": (now + timedelta(minutes=50)).isoformat(),
        "amps": amps,
    }


def _later_window(amps: int = 16) -> dict[str, Any]:
    start = dt_util.utcnow() + timedelta(hours=2)
    return {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(), "amps": amps}


async def test_a_lowered_setting_is_written_once_to_the_plans_running_charge(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await _world(hass, timers)
    await install_schedule(world.controller, _open_window(16))
    await hass.async_block_till_done()
    assert world.controller.charging
    assert world.writes == [16]
    world.clock.advance(120)

    await world.set_amps(11)

    assert world.writes == [16, 11], "the new current goes to the charge that runs, once"
    assert world.controller.requested_current_a == 11
    await world.shutdown()


async def test_a_persons_start_after_a_lowered_setting_starts_at_it(hass: HomeAssistant, timers: FakeScheduler) -> None:
    """The report's Start: the plan (made at 16 A) is stale, the setting says 11 A."""
    world = await _world(hass, timers)
    await install_schedule(world.controller, _later_window(16))
    await hass.async_block_till_done()
    await world.set_amps(11)
    assert world.writes == [], "nothing runs, so nothing is written"

    await world.executor.async_manual_start()
    await hass.async_block_till_done()

    assert world.controller.charging
    assert world.writes == [11]
    assert world.controller.requested_current_a == 11
    await world.shutdown()


async def test_a_persons_start_after_a_stop_starts_at_the_lowered_setting(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Stop, lower, Start: the last current asked for (16 A) is not the setting any more."""
    world = await _world(hass, timers)
    await install_schedule(world.controller, _open_window(16))
    await hass.async_block_till_done()
    await world.executor.async_manual_stop()
    await hass.async_block_till_done()
    assert not world.controller.charging
    await world.set_amps(11)
    world.clock.advance(120)

    await world.executor.async_manual_start()
    await hass.async_block_till_done()

    assert world.writes == [16, 11]
    await world.shutdown()


async def test_a_change_inside_the_policys_interval_waits_for_it_and_writes_the_last(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await _world(hass, timers)
    await install_schedule(world.controller, _open_window(16))
    await hass.async_block_till_done()
    assert world.writes == [16]
    world.clock.advance(10)

    await world.set_amps(12)
    await world.set_amps(11)
    assert world.writes == [16], "the charger's least interval between writes holds"

    world.clock.advance(85)
    await world.fire("_setting_current_retry")

    assert world.writes == [16, 11], "once allowed, the setting as it stands now, once"
    assert world.controller.requested_current_a == 11
    await world.shutdown()


async def test_the_suns_charge_is_left_alone(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await _world(hass, timers)
    await world.controller.async_start(8, cause="solar")
    await hass.async_block_till_done()
    assert world.writes == [8]
    world.clock.advance(120)

    await world.set_amps(11)

    assert world.writes == [8], "the sun writes its own current (the setting is its ceiling)"
    assert world.controller.requested_current_a == 8
    await world.shutdown()


async def test_a_plan_window_that_opens_after_the_change_starts_at_the_setting(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await _world(hass, timers)
    await install_schedule(world.controller, _later_window(16))
    await hass.async_block_till_done()
    await world.set_amps(11)

    await world.fire("_async_start_callback")

    assert world.controller.charging
    assert world.writes == [11], "the plan was made at 16 A; the setting says 11 A"
    await world.shutdown()


async def test_the_write_is_capped_by_load_balancing(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await _world(hass, timers)
    await install_schedule(world.controller, _open_window(16))
    await hass.async_block_till_done()
    world.clock.advance(120)
    world.controller.set_start_cap(lambda: 9.0)

    await world.set_amps(12)

    assert world.writes == [16, 9], "never more than the site allows now"
    assert world.controller.requested_current_a == 12, "the request stands, for the regulator to give when it can"
    await world.shutdown()


async def test_a_flash_stored_current_is_not_rewritten_during_the_charge(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await _world(hass, timers)
    world.controller.adapter.current.policy = WritePolicy(min_interval_s=60.0, flash_stored=True)
    await install_schedule(world.controller, _open_window(16))
    await hass.async_block_till_done()
    world.clock.advance(120)

    await world.set_amps(11)

    assert world.writes == [16], "a flash-stored setting is written at a start only"
    assert world.controller.requested_current_a == 11
    retries = [t for t in world.timers.pending if getattr(t.action, "__name__", "") == "_setting_current_retry"]
    assert retries == [], "nothing to retry: the policy will not allow it"
    await world.shutdown()


async def test_nothing_is_written_while_no_charge_of_ours_runs(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await _world(hass, timers)

    await world.set_amps(11)

    assert world.writes == []
    await world.shutdown()
