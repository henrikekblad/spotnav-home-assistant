"""Review C: second-round review of the manual-pause fix round."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED

from .pause_world import pause_world, SWITCH
from .relay import FakeScheduler


def _slow_turn_off(hass: HomeAssistant, gate: asyncio.Event, stops: list[Any]) -> None:
    async def handle(call: Any) -> None:
        stops.append(call)
        await gate.wait()
        current = hass.states.get(SWITCH)
        hass.states.async_set(SWITCH, "off", None if current is None else current.attributes)

    hass.services.async_register("switch", "turn_off", handle)


async def test_c1_regulator_pause_racing_a_persons_stop_resumes_their_charge(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Auto paused 'until I resume'; the person starts a charge. The regulator decides a pause (below the
    floor) just as the person presses Stop: it reads 'running, the person's' before it waits for the
    operation lock the Stop holds, and remembers it as balancing's pause after the Stop. With the span pause
    (no manual Stop is stored), `person_resume` is allowed: the regulator starts the charge the person just
    stopped.

    LATENT: in production the site's `_charge_still_wanted` forgets the balancing pause under a span pause
    before it calls the resume (see c5), so this only bites once C5 is completed there."""
    world = await pause_world(hass, timers)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    await world.executor.async_manual_start(10)
    assert world.controller.charging

    gate = asyncio.Event()
    stops: list[Any] = []
    _slow_turn_off(hass, gate, stops)
    person = hass.async_create_task(world.executor.async_manual_stop())
    await asyncio.sleep(0)
    for _ in range(5):
        await asyncio.sleep(0)
    assert stops, "the person's stop is on its way"
    regulator = hass.async_create_task(world.controller._regulated_stop("pause"))  # noqa: SLF001
    for _ in range(5):
        await asyncio.sleep(0)
    gate.set()
    await person
    await regulator
    await hass.async_block_till_done()
    assert not world.controller.charging

    resumed = await world.controller.async_battery_probe_start(8)
    await hass.async_block_till_done()
    assert not resumed and not world.controller.charging, "the person's Stop is undone by load balancing"
    await world.shutdown()


async def test_c2_a_car_that_stopped_drawing_below_the_plans_target_is_charged_by_a_later_window(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """P5's rule holds every later window of the plug-in unless the SoC dropped by the hysteresis. But
    'the car ended the charge' is only 'the car stopped drawing' (its own timer, a preconditioning pause, a
    fault), not 'the car is full': at 40 % with a plan to 80 % the night's window never starts, and the car
    is not charged for the departure. Before the fix only the window open at that moment was skipped."""
    from datetime import timedelta
    from types import SimpleNamespace

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.spotnav.planning.auto_settings import PauseIntent

    from .helpers import install_schedule
    from .pause_world import two_windows

    world = await pause_world(hass, timers)
    world.controller._soc_reader = lambda _vehicle: SimpleNamespace(  # noqa: SLF001
        soc_percent=40.0, estimated=False, source="entity", vehicle_id="car", age_s=1.0, entity_id=None
    )
    world.controller.car_drawing = lambda: False  # type: ignore[method-assign]
    await world.executor.async_manual_start()
    await world.switch("off")  # the car stopped drawing (its own departure timer), far from full
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert world.pause == PauseIntent()

    await install_schedule(world.controller, {**two_windows(), "target_soc_percent": 80, "vehicle_id": "car"})
    starts = len(world.starts)
    freezer.tick(timedelta(hours=2, minutes=1))
    await world.fire("_async_start_callback")
    assert len(world.starts) == starts + 1, "the later window does not charge a car at 40 % toward 80 %"
    await world.shutdown()


async def test_c3_a_charger_that_keeps_reporting_on_under_a_persons_stop_is_not_sent_a_stop_per_report(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Under a person's Stop the charger begins by itself and keeps saying 'on' for a while after each
    stop it accepted (an OCPP status that lags, meter values every few seconds). Every report sends
    another stop: 20 reports, 20 stop commands."""
    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    assert world.pause.action == "stop"

    stops: list[Any] = []

    async def accepted_but_still_on(call: Any) -> None:
        stops.append(call)  # accepted; the status still says on

    hass.services.async_register("switch", "turn_off", accepted_but_still_on)
    for n in range(20):
        hass.states.async_set(SWITCH, "on", {"plug": True, "meter": n})
        await hass.async_block_till_done()
    assert len(stops) <= 3, f"{len(stops)} stop commands for 20 reports of one charge"
    await world.shutdown()


async def test_c4_a_start_whose_pause_cannot_be_stored_after_a_stop_is_stopped_by_the_stops_hold(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Stop, then Start while the settings write fails. Spec C3: the charger command decides and the charge
    stands (`reconcile_failed` reported). But the stored pause is still the person's Stop, so C7's rule
    stops the charge the person just started at the charger's next report."""
    import pytest

    from custom_components.spotnav.execution.auto_execution import AutoControlCommitted

    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    world.store.async_update = broken  # type: ignore[method-assign]
    with pytest.raises(AutoControlCommitted):
        await world.executor.async_manual_start(10)
    await hass.async_block_till_done()
    hass.states.async_set(SWITCH, "on", {"plug": True, "meter": 1})  # the next report
    await hass.async_block_till_done()
    assert world.controller.charging, f"the person's Start was stopped; pause {world.pause.action}"
    await world.shutdown()


async def test_c5_the_regulator_drops_a_persons_charge_paused_under_a_span_pause(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch
) -> None:
    """Spec C5: a charge a person started under a span pause ('until I resume') is resumed by load balancing
    like a manual Start. The controller's gate now allows it (`person_resume`), but the regulator's own
    pre-check before it ever calls the resume (`SiteCapacityController._charge_still_wanted`, used by both
    `_async_maybe_resume_paused_charge` and the battery probe) still treats any admitted pause but a manual
    Start as 'not wanted' and forgets the balancing pause: the person's charge is never resumed."""
    from types import SimpleNamespace

    from custom_components.spotnav.site import site_capacity_controller as scc

    world = await pause_world(hass, timers)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    await world.executor.async_manual_start(10)
    await world.controller._regulated_stop("pause")  # noqa: SLF001
    assert world.controller.paused_by_balancing

    monkeypatch.setattr(scc, "domain_data", lambda _hass: SimpleNamespace(auto_store=world.store))
    fake_site = SimpleNamespace(hass=hass)
    wanted = scc.SiteCapacityController._charge_still_wanted(fake_site, "entry_a", world.controller)  # noqa: SLF001
    assert wanted and world.controller.paused_by_balancing, "the regulator forgets the person's paused charge"
    await world.shutdown()



async def test_r4_the_regulator_forgets_a_persons_paused_charge_under_their_stop(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch
) -> None:
    """R4: a person's charge held by load balancing is wanted under any pause but the person's Stop."""
    from types import SimpleNamespace

    from custom_components.spotnav.site import site_capacity_controller as scc

    world = await pause_world(hass, timers)
    await world.executor.async_manual_start(10)
    await world.executor.async_manual_stop()
    assert world.pause.action == "stop"
    world.controller._remember_paused_charge("manual", False)  # noqa: SLF001 - as if still held back

    monkeypatch.setattr(scc, "domain_data", lambda _hass: SimpleNamespace(auto_store=world.store))
    fake_site = SimpleNamespace(hass=hass)
    wanted = scc.SiteCapacityController._charge_still_wanted(fake_site, "entry_a", world.controller)  # noqa: SLF001
    assert not wanted and not world.controller.paused_by_balancing
    await world.shutdown()


async def test_r4_a_plan_charge_paused_under_a_span_pause_is_not_wanted(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch
) -> None:
    """R4: under a span pause only a person's charge is wanted; one of the plan's is forgotten."""
    from types import SimpleNamespace

    from custom_components.spotnav.site import site_capacity_controller as scc

    world = await pause_world(hass, timers)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    world.controller._remember_paused_charge("plan_window", True)  # noqa: SLF001

    monkeypatch.setattr(scc, "domain_data", lambda _hass: SimpleNamespace(auto_store=world.store))
    fake_site = SimpleNamespace(hass=hass)
    wanted = scc.SiteCapacityController._charge_still_wanted(fake_site, "entry_a", world.controller)  # noqa: SLF001
    assert not wanted and not world.controller.paused_by_balancing
    await world.shutdown()


async def test_r5_stops_under_a_persons_stop_are_spaced_and_given_up_after_three(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """R5: a charger that keeps saying 'on' after each stop it accepted gets one stop per 30 s, also with no
    new report; after three such stops SpotNav gives up and says so; a report of the charger off, or an
    unplug, starts afresh."""
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.spotnav.execution.controller import PERSON_HOLD_STOP_GAP_S

    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    stops: list[Any] = []

    async def accepted_but_still_on(call: Any) -> None:
        stops.append(call)

    hass.services.async_register("switch", "turn_off", accepted_but_still_on)
    hass.states.async_set(SWITCH, "on", {"plug": True, "meter": 0})
    await hass.async_block_till_done()
    assert len(stops) == 1

    for n in range(1, 6):
        freezer.tick(timedelta(seconds=PERSON_HOLD_STOP_GAP_S + 1))
        async_fire_time_changed(hass)  # no report: the gap's own timer looks again
        await hass.async_block_till_done()
        hass.states.async_set(SWITCH, "on", {"plug": True, "meter": n})
        await hass.async_block_till_done()
    assert len(stops) == 3, f"{len(stops)} stops"
    assert world.controller.ignores_person_stop

    await world.switch("off")  # the charger took one at last
    assert not world.controller.ignores_person_stop
    hass.states.async_set(SWITCH, "on", {"plug": True, "meter": 99})  # and begins by itself again
    await hass.async_block_till_done()
    assert len(stops) == 4
    await world.shutdown()


async def test_r5_a_charger_that_takes_each_stop_is_stopped_each_time_it_begins_again(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """R5: a charger that takes the stop and later begins by itself again is stopped again (never given up),
    no sooner than the gap after the last stop."""
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    before = len(world.stops)
    for _ in range(5):
        freezer.tick(timedelta(minutes=5))
        async_fire_time_changed(hass)
        await world.switch("on")  # begins by itself; the obedient switch takes the stop
        await hass.async_block_till_done()
    assert len(world.stops) - before == 5
    assert not world.controller.ignores_person_stop
    await world.shutdown()


async def test_r6_a_start_whose_pause_cannot_be_saved_owns_the_charge_and_the_save_is_retried(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """R6: Stop, then Start while the settings file cannot be written. The pause in memory is the person's
    Start at once, so the Stop's hold never stops the charge, however long the charger keeps reporting; the
    file is written again until it takes the Start."""
    from datetime import timedelta

    import pytest
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.spotnav.execution.auto_execution import AutoControlCommitted, PAUSE_SAVE_RETRY_S

    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    inner = world.store._store  # noqa: SLF001 - the file underneath
    real_save = inner.async_save
    failing = {"on": True}
    saved: list[Any] = []

    async def save(document: Any) -> None:
        if failing["on"]:
            raise OSError("disk full")
        saved.append(document)
        await real_save(document)

    inner.async_save = save  # type: ignore[method-assign]
    with pytest.raises(AutoControlCommitted):
        await world.executor.async_manual_start(10)
    assert world.pause.action == "start", "memory holds what the person did"
    stops = len(world.stops)
    for n in range(4):
        freezer.tick(timedelta(seconds=31))
        async_fire_time_changed(hass)
        hass.states.async_set(SWITCH, "on", {"plug": True, "meter": n})
        await hass.async_block_till_done()
    assert world.controller.charging and len(world.stops) == stops

    failing["on"] = False
    freezer.tick(timedelta(seconds=PAUSE_SAVE_RETRY_S + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert saved and not world.store.unsaved
    stored = saved[-1]["chargers"]["entry_a"]["settings"]["pause"]
    assert stored["action"] == "start"
    await world.shutdown()


async def test_a_target_stop_not_executed_still_arms_the_plans_window_timers(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """A plan whose target is already reached arrives while its window is open, on a button charger whose
    stop button is unavailable (its state cannot say it is off, and it is not seen charging): the target's
    stop is not executed and the plan stays. Its window timers are armed all the same, so its windows'
    ends and starts still decide."""
    from types import SimpleNamespace

    from custom_components.spotnav.execution.controller import ChargingController

    from .helpers import install_schedule
    from .pause_world import two_windows
    from .test_spot_fix_review import BUTTONS

    hass.states.async_set("button.start", "unavailable")
    hass.states.async_set("button.stop", "unavailable")
    controller = ChargingController(hass, "entry_a", BUTTONS)
    await controller.async_initialize()
    controller._soc_reader = lambda _vehicle: SimpleNamespace(  # noqa: SLF001
        soc_percent=85.0, estimated=False, source="entity", vehicle_id=None, age_s=1.0, entity_id=None
    )
    await install_schedule(controller, {**two_windows(), "target_soc_percent": 80})
    await hass.async_block_till_done()
    assert controller.plan is not None, "the stop did not go out: the plan stays"
    armed = {getattr(timer.action, "__name__", "") for timer in timers.pending}
    assert "_async_start_callback" in armed and armed & {"_async_end_callback", "_async_final_end_callback"}, armed
    await controller.async_shutdown()


async def test_an_unplug_during_a_stop_not_executed_ends_the_balancing_pause(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """A balancing pause is held for a person's charge; a stop goes to the charger, which is unplugged
    while the command is on its way and then reports it not executed. The unplug happened: the stop's
    failure branch does not put the balancing pause back for a plug-in that is over."""
    import pytest

    from custom_components.spotnav.execution.controller import ChargingExecutionError

    world = await pause_world(hass, timers)
    await world.executor.async_manual_start(10)
    controller = world.controller
    controller._remember_paused_charge("manual", False)  # noqa: SLF001 - balancing holds a person's charge

    async def unplugged_on_the_way() -> bool:
        world.plug.connected = False
        hass.states.async_set(SWITCH, "on", {"plug": False, "stamp": "unplug"})
        for _ in range(5):
            await asyncio.sleep(0)
        return False

    controller.adapter.async_stop = unplugged_on_the_way  # type: ignore[method-assign]
    with pytest.raises(ChargingExecutionError):
        await controller.async_stop(balancing=True)
    await hass.async_block_till_done()
    assert not controller._paused_by_balancing, "a balancing pause outlived the unplug"  # noqa: SLF001
    await world.shutdown()


async def test_an_unplug_during_a_resume_that_did_not_start_ends_the_balancing_pause(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """The regulator resumes a person's charge load balancing held; the car is unplugged while the start is
    on its way and the start does not go out. The unplug wins: nothing is left to resume."""
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start(10)
    controller = world.controller
    await controller._regulated_stop("pause")  # noqa: SLF001
    assert controller.paused_by_balancing

    async def unplugged_on_the_way(_amps: int | None = None) -> bool:
        world.plug.connected = False
        hass.states.async_set(SWITCH, "off", {"plug": False, "stamp": "unplug"})
        for _ in range(5):
            await asyncio.sleep(0)
        return False

    controller.adapter.async_start = unplugged_on_the_way  # type: ignore[method-assign]
    assert not await controller.async_battery_probe_start(8)
    await hass.async_block_till_done()
    assert not controller.paused_by_balancing, "a balancing pause outlived the unplug"
    await world.shutdown()
