"""A person's Start or Stop pauses Auto, schedule and strategy, for the plug-in session.

While paused this way the person owns the charger: no plan window, top-off, hold or stray-charge stop
starts or stops it. The pause is Auto's own pause with the choice `manual`, carrying the action that set
it, persisted like any pause. It ends when the car is unplugged (a Stop given with no car plugged in
lasts through the next plug-in and ends at the unplug after it), when the person resumes Auto or picks
another pause, or, after a Start, when the car ends the charge by itself; then Auto resumes and replans,
and no window starts the full car again in that plug-in unless its need grew.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.execution.auto_execution import (
    ACTION_RESUME,
    ACTION_STOP,
    AutoControlRefused,
)
from custom_components.spotnav.execution.manual_pause import MANUAL_CAR_IDLE_S
from custom_components.spotnav.planning.auto_settings import (
    MANUAL_SCOPE_NEXT_PLUG_IN,
    MANUAL_SCOPE_PLUG_IN,
    MANUAL_START,
    MANUAL_STOP,
    PAUSE_MANUAL,
    PAUSE_UNTIL_RESUMED,
    PauseIntent,
)

from .helpers import install_schedule
from .pause_world import ENTRY, open_window, pause_world, two_windows
from .relay import FakeScheduler


def _manual(world: Any) -> tuple[str | None, str | None, str | None]:
    pause = world.pause
    return pause.choice, pause.action, pause.scope


# ------------------------------------------------------------------------------------- a Stop


async def test_a_stop_pauses_auto_for_the_plug_in_and_clears_its_plan(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    assert world.controller.charging

    await world.executor.async_manual_stop()

    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert not world.controller.charging
    assert world.controller.plan is None, "a paused Auto keeps no plan of its own"
    assert world.timers.pending == [], "no window of it is left to start"
    await world.shutdown()


async def test_a_stop_survives_a_restart_inside_the_window(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Bug 1: a person's Stop in a window, then Home Assistant restarts in the same window. The Stop was
    memory only and the re-arm started the charge again."""
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    starts = len(world.starts)

    restarted = await world.restart()
    # Even a plan of Auto's that reached the charger some other way does not start under the pause.
    await install_schedule(restarted.controller, open_window())
    await hass.async_block_till_done()

    assert _manual(restarted) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert len(world.starts) == starts
    await restarted.shutdown()


async def test_an_unplug_ends_a_stop(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()

    await world.plug.set(None)  # a blip says nothing
    await world.plug.set(True)
    assert world.pause.choice == PAUSE_MANUAL
    await world.plug.set(False)

    assert world.pause == PauseIntent()
    await world.shutdown()


async def test_a_stop_without_a_car_holds_through_the_next_plug_in_and_ends_at_its_unplug(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers, connected=False)

    await world.executor.async_manual_stop()
    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_NEXT_PLUG_IN)

    await world.plug.set(True)
    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN), "the plug-in it was given for"
    assert await world.executor.async_start_on_plug_in() is False

    restarted = await world.restart()
    assert _manual(restarted) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)

    await restarted.plug.set(False)
    assert restarted.pause == PauseIntent()
    await restarted.shutdown()


async def test_a_stop_without_a_car_survives_a_restart_before_the_plug_in(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers, connected=False)
    await world.executor.async_manual_stop()

    restarted = await world.restart()
    assert _manual(restarted) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_NEXT_PLUG_IN)
    await restarted.plug.set(True)
    assert restarted.pause.scope == MANUAL_SCOPE_PLUG_IN
    await restarted.shutdown()


async def test_a_car_unplugged_while_home_assistant_was_down_ends_a_stop(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    world.plug.connected = False

    restarted = await world.restart()
    await restarted.plug.set(False)
    await hass.async_block_till_done()

    assert restarted.pause == PauseIntent()
    await restarted.shutdown()


# ------------------------------------------------------------------------------------ a Start


async def test_a_start_pauses_auto_and_no_window_end_stops_it(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """Bug 2: windows 01:00-02:00 and 03:00-04:00, a person's Start at 01:30: the end at 02:00 stopped it."""
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    await world.executor.async_manual_start()
    assert _manual(world) == (PAUSE_MANUAL, MANUAL_START, MANUAL_SCOPE_PLUG_IN)
    assert world.controller.charging
    stops = len(world.stops)

    freezer.tick(timedelta(minutes=55))
    await world.fire("_async_end_callback")
    await world.fire("_async_final_end_callback")

    assert world.controller.charging and len(world.stops) == stops
    await world.shutdown()


async def test_a_start_replaces_a_stop_and_a_stop_a_start(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()
    assert world.pause.action == MANUAL_START

    await world.executor.async_manual_stop()
    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN), "still the person's, now a Stop"
    assert not world.controller.charging
    await world.shutdown()


async def test_a_start_with_no_car_is_refused_and_pauses_nothing(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, connected=False)

    with pytest.raises(AutoControlRefused) as refused:
        await world.executor.async_manual_start()

    assert refused.value.code == "vehicle_not_connected"
    assert world.starts == [], "nothing is sent to a charger with no car"
    assert world.pause == PauseIntent()
    await world.shutdown()


async def test_a_start_when_the_connection_is_not_known_goes_out_and_replaces_a_stop(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers, connected=False)
    await world.executor.async_manual_stop()
    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_NEXT_PLUG_IN)
    await world.plug.set(None)  # the status says nothing either way

    await world.executor.async_manual_start()

    assert world.controller.charging
    assert world.pause.action == MANUAL_START, "a Start never leaves the Stop it contradicts"
    await world.shutdown()


async def test_a_start_that_did_not_go_out_leaves_auto_as_it_was(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    before = world.pause

    async def not_executed(_amps: Any = None) -> bool:
        return False

    world.controller.adapter.async_start = not_executed  # type: ignore[method-assign]
    with pytest.raises(Exception):
        await world.executor.async_manual_start()

    assert world.pause == before
    await world.shutdown()


async def test_an_unplug_ends_a_start(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()

    await world.plug.set(False)

    assert world.pause == PauseIntent()
    await world.shutdown()


async def test_a_start_survives_a_restart(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()

    restarted = await world.restart()
    await install_schedule(restarted.controller, two_windows())
    await hass.async_block_till_done()

    assert _manual(restarted) == (PAUSE_MANUAL, MANUAL_START, MANUAL_SCOPE_PLUG_IN)
    assert restarted.controller.charging
    await restarted.shutdown()


async def test_a_full_car_ends_a_start_and_the_open_window_is_not_started_again(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    world = await pause_world(hass, timers)
    drawing = {"value": True}
    world.controller.car_drawing = lambda: drawing["value"]  # type: ignore[method-assign]
    await world.executor.async_manual_start()
    assert world.controller.charging

    drawing["value"] = False
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)  # a look: the car draws nothing now
    await hass.async_block_till_done()
    await world.switch("off")  # and the charge ends by itself
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert world.pause == PauseIntent(), "the car ended the person's charge: Auto resumes"
    starts = len(world.starts)
    # Auto replans and installs a plan whose window is open now: the car is full, nothing restarts.
    await install_schedule(world.controller, two_windows())
    await hass.async_block_till_done()
    assert len(world.starts) == starts

    restarted = await world.restart()  # not after a restart either
    assert len(world.starts) == starts
    freezer.tick(timedelta(hours=2, minutes=1))
    await restarted.fire("_async_start_callback")
    # Second review (R3): with no state of charge to read, the car is not known full: it only stopped
    # drawing, so a later window charges it as planned (only the window open then was skipped).
    assert len(world.starts) == starts + 1, "a later window starts a car not known to be full"
    await restarted.shutdown()


async def test_a_car_known_full_after_it_ended_a_start_is_not_started_by_a_later_window(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """R3: the car ended the person's charge at a state of charge read at or above the plan's target: no
    later window of the plug-in starts it, also after a restart."""
    from types import SimpleNamespace

    world = await pause_world(hass, timers)
    reading = SimpleNamespace(
        soc_percent=80.0, estimated=False, source="entity", vehicle_id=None, age_s=1.0, entity_id=None
    )
    world.controller._soc_reader = lambda _vehicle: reading  # noqa: SLF001
    world.controller.car_drawing = lambda: False  # type: ignore[method-assign]
    await world.executor.async_manual_start()
    await world.switch("off")  # the car ended it, full
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert world.pause == PauseIntent()

    starts = len(world.starts)
    await install_schedule(world.controller, {**two_windows(), "target_soc_percent": 80})
    restarted = await world.restart()
    restarted.controller._soc_reader = lambda _vehicle: reading  # noqa: SLF001
    freezer.tick(timedelta(hours=2, minutes=1))
    await restarted.fire("_async_start_callback")
    assert len(world.starts) == starts, "a later window started a car known full"
    await restarted.shutdown()


async def test_an_estimated_state_of_charge_does_not_make_a_car_known_full(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    """R3: only a read state of charge says the car is full; an estimate carried forward does not."""
    from types import SimpleNamespace

    world = await pause_world(hass, timers)
    world.controller._soc_reader = lambda _vehicle: SimpleNamespace(  # noqa: SLF001
        soc_percent=85.0, estimated=True
    )
    world.controller.car_drawing = lambda: False  # type: ignore[method-assign]
    await world.executor.async_manual_start()
    await world.switch("off")
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    await install_schedule(world.controller, {**two_windows(), "amps": 10})
    starts = len(world.starts)
    freezer.tick(timedelta(hours=2, minutes=1))
    await world.fire("_async_start_callback")
    assert len(world.starts) == starts + 1
    await world.shutdown()


async def test_a_later_window_starts_a_car_whose_charge_dropped_after_it_ended_a_start(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    from types import SimpleNamespace

    world = await pause_world(hass, timers)
    soc = {"value": 80.0}
    world.controller._soc_reader = lambda _vehicle: SimpleNamespace(  # noqa: SLF001
        soc_percent=soc["value"], estimated=False, source="entity", vehicle_id=None, age_s=1.0, entity_id=None
    )
    world.controller.car_drawing = lambda: False  # type: ignore[method-assign]
    await world.executor.async_manual_start()
    await world.switch("off")  # the car ended it
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert world.pause == PauseIntent()

    # The car ended it at the plan's target, so it is known full (R3: the hysteresis is that case's rule).
    await install_schedule(world.controller, {**two_windows(), "target_soc_percent": 80})
    starts = len(world.starts)
    soc["value"] = 79.0  # less than the hysteresis
    freezer.tick(timedelta(hours=2, minutes=1))
    await world.fire("_async_start_callback")
    assert len(world.starts) == starts

    await install_schedule(world.controller, {**two_windows(), "target_soc_percent": 80})
    soc["value"] = 77.0  # the car was used: its need grew
    freezer.tick(timedelta(hours=2, minutes=1))
    await world.fire("_async_start_callback")
    assert len(world.starts) == starts + 1
    await world.shutdown()


async def test_a_car_that_stops_drawing_ends_a_start_after_the_idle_time(
    hass: HomeAssistant, timers: FakeScheduler, freezer
) -> None:
    world = await pause_world(hass, timers)
    drawing = {"value": True}
    world.controller.car_drawing = lambda: drawing["value"]  # type: ignore[method-assign]
    await world.executor.async_manual_start()
    drawing["value"] = False
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    freezer.tick(timedelta(seconds=MANUAL_CAR_IDLE_S - 60))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert world.pause.choice == PAUSE_MANUAL

    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert world.pause == PauseIntent()
    assert world.controller.charging, "Auto resumes; it does not stop a charge to resume"
    await world.shutdown()


async def test_a_charge_stopped_by_other_means_keeps_the_start_pause(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers)
    world.controller.car_drawing = lambda: True  # type: ignore[method-assign]
    await world.executor.async_manual_start()
    assert world.controller.charging

    await world.switch("off")  # the charger's own app, say: the car was still drawing

    assert world.pause.choice == PAUSE_MANUAL
    await world.shutdown()


# ------------------------------------------------------------------------ resuming, other pauses


async def test_resume_auto_ends_a_manual_pause(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    assert world.executor.automatic_decision().action == ACTION_RESUME

    await world.executor.async_manual_action(ACTION_RESUME)

    assert world.pause == PauseIntent()
    await world.shutdown()


async def test_another_pause_choice_replaces_a_manual_pause(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()

    await world.executor.async_manual_action(ACTION_STOP, PAUSE_UNTIL_RESUMED)

    assert world.pause.choice == PAUSE_UNTIL_RESUMED and world.pause.action is None
    with pytest.raises(AutoControlRefused):
        await world.executor.async_manual_action(ACTION_STOP, PAUSE_MANUAL)
    await world.shutdown()


async def test_manual_is_never_offered_as_a_pause_choice(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    assert PAUSE_MANUAL not in world.executor.pause_choices()
    await world.executor.async_manual_stop()
    assert PAUSE_MANUAL not in world.executor.pause_choices()
    assert world.executor.automatic_decision().choices == ()
    await world.shutdown()


async def test_a_regular_pause_is_kept_by_a_persons_start_and_stop(hass: HomeAssistant, timers: FakeScheduler) -> None:
    """A pause the person chose for a span (until tomorrow, until resumed) already holds Auto: a Start or
    Stop under it acts on the charger and leaves that pause as it is."""
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    paused = world.pause

    await world.executor.async_manual_start()
    assert world.pause == paused and world.controller.charging
    await world.executor.async_manual_stop()
    assert world.pause == paused and not world.controller.charging
    await world.shutdown()


async def test_an_unplug_while_paused_changes_no_regular_pause(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    paused = world.pause

    await world.plug.set(False)
    await world.plug.set(True)

    assert world.pause == paused
    await world.shutdown()


async def test_follow_ends_a_manual_pause(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()

    # No plan to follow (Auto has not replanned here): the pause's end was the request, and no error.
    await world.executor.async_manual_follow()

    assert world.pause == PauseIntent()
    await world.shutdown()


async def test_cancel_is_a_stop(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())

    await world.executor.async_manual_cancel()

    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert world.controller.plan is None and not world.controller.charging
    await world.shutdown()


# ---------------------------------------------------------------------------- the rest of the rule


async def test_under_a_manual_pause_nothing_is_expected_to_charge(hass: HomeAssistant, timers: FakeScheduler) -> None:
    """No "stopped unexpectedly" or "did not start" for a charge a person stopped: the plan expects nothing."""
    world = await pause_world(hass, timers, plan=two_windows())
    assert world.controller.plan_expects_charge
    await world.executor.async_manual_stop()
    # A plan that reached the charger anyway expects nothing either.
    await install_schedule(world.controller, two_windows())
    await hass.async_block_till_done()

    assert not world.controller.plan_expects_charge
    await world.shutdown()


async def test_a_stored_person_stop_of_an_older_release_becomes_a_manual_pause(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers)
    saved = await world.controller._store.async_load()  # noqa: SLF001 - the older release's record
    saved["person_stopped"] = True
    await world.controller._store.async_save(saved)  # noqa: SLF001

    restarted = await world.restart()

    assert _manual(restarted) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    resaved = await restarted.controller._store.async_load()  # noqa: SLF001
    assert "person_stopped" not in resaved
    await restarted.shutdown()


async def test_the_pause_is_stored_with_its_action_and_scope(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()

    stored = world.store.settings(ENTRY).as_dict()["pause"]
    assert stored["choice"] == PAUSE_MANUAL and stored["expires_at"] is None
    assert (stored["action"], stored["scope"]) == (MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert PauseIntent.from_stored(stored) == world.pause
    await world.shutdown()


def test_a_regular_pause_is_stored_exactly_as_before() -> None:
    now = dt_util.utcnow()
    intent = PauseIntent(choice=PAUSE_UNTIL_RESUMED, admitted_at=now)
    assert set(intent.as_dict()) == {"choice", "admitted_at", "expires_at"}


@pytest.mark.parametrize(
    "raw",
    [
        {"choice": "manual", "admitted_at": None, "expires_at": None},
        {"choice": "manual", "admitted_at": None, "expires_at": None, "action": "pause", "scope": "plug_in"},
        {"choice": "manual", "admitted_at": None, "expires_at": None, "action": "stop", "scope": "forever"},
        {"choice": "until_resumed", "admitted_at": None, "expires_at": None, "action": "stop", "scope": "plug_in"},
    ],
)
def test_a_stored_manual_pause_must_say_what_set_it(raw: dict[str, Any]) -> None:
    from custom_components.spotnav.planning.auto_settings import AutoSettingsError

    with pytest.raises(AutoSettingsError):
        PauseIntent.from_stored(raw)


# ---------------------------------------------------------------------------- reviews of the rule


async def test_a_plug_in_reported_while_a_stop_is_stored_keeps_the_stop_for_that_plug_in(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Stop with no car; the car is plugged in while the pause is being written: the plug-in is decided
    against the stored pause afterwards, so the Stop holds for that plug-in and ends at its unplug."""
    world = await pause_world(hass, timers, connected=False)
    real_update = world.store.async_update

    async def update_with_a_plug_in(*args: Any, **kwargs: Any) -> Any:
        # Reported while the pause is on its way to the store (not waited for: the Stop holds the lock).
        world.plug.connected = True
        hass.states.async_set("switch.a", "off", {"plug": True, "stamp": "during the write"})
        return await real_update(*args, **kwargs)

    world.store.async_update = update_with_a_plug_in  # type: ignore[method-assign]
    await world.executor.async_manual_stop()
    world.store.async_update = real_update  # type: ignore[method-assign]
    await hass.async_block_till_done()

    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    await world.plug.set(False)
    await hass.async_block_till_done()
    assert world.pause == PauseIntent()
    await world.shutdown()


async def test_a_safety_stop_of_a_persons_start_is_resumed_by_load_balancing(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start(10)
    assert world.controller.charging

    await world.controller._regulated_stop("safety_stop", cause="rate_limited")  # noqa: SLF001

    assert world.controller.paused_by_balancing, "the person's charge waits for room, as after a pause"
    assert await world.controller.async_battery_probe_start(8)
    assert world.controller.charging
    await world.shutdown()


async def test_a_start_whose_pause_cannot_be_stored_still_charges_and_says_so(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    from custom_components.spotnav.execution.auto_execution import AutoControlCommitted

    world = await pause_world(hass, timers)

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    world.store.async_update = broken  # type: ignore[method-assign]
    with pytest.raises(AutoControlCommitted):
        await world.executor.async_manual_start(10)

    assert world.controller.charging, "the charger command decides"
    await world.shutdown()
