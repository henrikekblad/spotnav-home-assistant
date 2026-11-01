"""One history entry per charge, read from a real controller and execution boundary: a stop SpotNav made while the car
stays plugged in (load balancing's pause, the gap between two windows of one plan) does not end the entry; a person's
Stop, and the unplug, do. The facts are the ones the recorder reads in the running integration (`inputs.pause_facts`).
Every test runs in both modes: with the charge-ownership core deciding and with the previous logic."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.planning.planner import FiscalChoice
from custom_components.spotnav.sessions.inputs import pause_facts
from custom_components.spotnav.sessions.model import STARTED_MANUAL, STARTED_PLAN_WINDOW
from custom_components.spotnav.sessions.recorder import END_DEBOUNCE_S, SessionFacts, SessionRecorder
from custom_components.spotnav.sessions.store import SessionStore

from .pause_world import ENTRY, pause_world, two_windows, World
from .relay import FakeScheduler


@pytest.fixture(params=[True, False], ids=["core", "previous"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


class Recording:
    """A session recorder reading a pause world's controller, with a register the test moves."""

    def __init__(self, hass: HomeAssistant, world: World, store: SessionStore) -> None:
        self.world = world
        self.store = store
        self.register = 100.0
        self.recorder = SessionRecorder(
            hass, ENTRY, store,
            facts=self._facts, prices=lambda now: None,
            consume_cause=lambda: self.world.controller.consume_start_cause(),
        )

    def _facts(self) -> SessionFacts:
        controller = self.world.controller
        settings = self.world.store.settings(ENTRY)
        return SessionFacts(
            charging=controller.charging, connected=self.world.plug.connected, register_kwh=self.register,
            register_integrated=False, estimate_kw=None, strategy=settings.strategy, vehicle_id=None,
            vehicle_name=None, solar_share=None, pause=pause_facts(controller, settings, None),
        )

    async def look(self, freezer: Any, minutes: float = 0.0, *, kwh: float = 0.0) -> None:
        if minutes:
            freezer.tick(timedelta(minutes=minutes))
        self.register += kwh
        await self.world.hass.async_block_till_done()
        self.recorder.evaluate(dt_util.utcnow())


@pytest.fixture
async def sessions(hass: HomeAssistant) -> SessionStore:
    store = SessionStore(hass)
    store.set_fiscal_resolver(lambda charger_id, area_id: FiscalChoice())
    await store.async_load()
    return store


async def test_a_persons_charge_paused_twice_by_load_balancing_is_one_entry(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any, sessions: SessionStore, drives: bool
) -> None:
    world = await pause_world(hass, timers)
    recording = Recording(hass, world, sessions)
    controller = world.controller
    started = dt_util.utcnow()

    await world.executor.async_manual_start(10)
    await recording.look(freezer)
    for _pause in range(2):
        await recording.look(freezer, 3, kwh=1.0)
        await controller._regulated_stop("pause")  # noqa: SLF001 - the regulator's own pause
        await recording.look(freezer)
        assert controller.paused_by_balancing
        await recording.look(freezer, 2.5)
        await recording.look(freezer, 2.5)
        assert sessions.closed(ENTRY) == (), "load balancing's pause holds the entry, however long"
        assert await controller.async_battery_probe_start(8)
        await recording.look(freezer)
        assert controller.charging
    await recording.look(freezer, 10, kwh=2.0)
    stopped = dt_util.utcnow()
    await world.executor.async_manual_stop()
    await recording.look(freezer)
    await recording.look(freezer, END_DEBOUNCE_S / 60)

    (done,) = sessions.closed(ENTRY)
    assert (done.start, done.end) == (started, stopped)
    assert done.started_by == STARTED_MANUAL and done.energy_kwh == pytest.approx(4.0)
    await world.shutdown()


async def test_a_persons_stop_and_new_start_are_two_entries(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any, sessions: SessionStore, drives: bool
) -> None:
    world = await pause_world(hass, timers)
    recording = Recording(hass, world, sessions)

    await world.executor.async_manual_start(10)
    await recording.look(freezer)
    await recording.look(freezer, 30, kwh=3.0)
    await world.executor.async_manual_stop()
    await recording.look(freezer)
    await recording.look(freezer, 1)
    await world.executor.async_manual_start(10)
    await recording.look(freezer)
    await recording.look(freezer, 30, kwh=2.0)
    await world.switch("off")  # the car is taken away
    await world.plug.set(False)
    await recording.look(freezer)

    first, second = sessions.closed(ENTRY)
    assert (first.energy_kwh, second.energy_kwh) == (pytest.approx(3.0), pytest.approx(2.0))
    assert first.started_by == second.started_by == STARTED_MANUAL
    await world.shutdown()


async def test_two_windows_of_one_plan_are_one_entry_and_the_unplug_ends_it(
    hass: HomeAssistant, timers: FakeScheduler, freezer: Any, sessions: SessionStore, drives: bool
) -> None:
    """The first window is open (ten minutes in, fifty to go), the second starts two hours later. A load balancing
    pause in the second window, then the unplug, ends the entry at that pause's stop."""
    world = await pause_world(hass, timers, plan=two_windows())
    recording = Recording(hass, world, sessions)
    assert world.controller.charging
    await recording.look(freezer)
    await recording.look(freezer, 50, kwh=5.0)
    await world.fire("_async_end_callback")
    await recording.look(freezer)
    assert not world.controller.charging
    stopped = dt_util.utcnow()
    for _look in range(7):
        await recording.look(freezer, 10)
    assert sessions.closed(ENTRY) == (), "the gap to the plan's next window holds the entry, up to its start"
    await world.fire("_async_start_callback")
    await recording.look(freezer)
    assert world.controller.charging
    await recording.look(freezer, 30, kwh=4.0)
    held_at = dt_util.utcnow()
    await world.controller._regulated_stop("pause")  # noqa: SLF001 - the regulator's own pause
    await recording.look(freezer)
    await recording.look(freezer, 5)
    await world.plug.set(False)
    await recording.look(freezer)

    (done,) = sessions.closed(ENTRY)
    assert done.end == held_at and done.end - done.start > timedelta(hours=2)
    assert done.started_by == STARTED_PLAN_WINDOW and done.energy_kwh == pytest.approx(9.0)
    assert stopped < held_at
    await world.shutdown()
