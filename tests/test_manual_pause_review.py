"""Adversarial review of part A (manual pause). Each test states the expected behaviour by the spec."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

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
from .pause_world import later_window, open_window, pause_world, two_windows
from .relay import FakeScheduler


def _manual(world: Any) -> tuple[str | None, str | None, str | None]:
    pause = world.pause
    return pause.choice, pause.action, pause.scope


async def test_r1_stop_without_car_plug_in_during_restart_ends_at_the_unplug_after_it(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Stop with no car -> HA restarts; the car is plugged in while HA is down -> the car is unplugged.
    Spec A3: the pause survives the next plug-in and ends at the unplug after it."""
    world = await pause_world(hass, timers, connected=False)
    await world.executor.async_manual_stop()
    assert _manual(world) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_NEXT_PLUG_IN)

    world.plug.connected = True  # plugged in while Home Assistant was down
    restarted = await world.restart()
    await hass.async_block_till_done()

    await restarted.plug.set(False)  # the unplug that ends that plug-in session

    assert restarted.pause == PauseIntent(), f"pause outlives its plug-in session: {_manual(restarted)}"
    await restarted.shutdown()


async def test_r2_stop_while_status_says_nothing_then_plug_in_keeps_the_stop(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Car unplugged (known), then the status says nothing either way (a Wallbox 'Ready', an Easee blip)
    when the person presses Stop -> scope plug_in. The car is then plugged in: the pause ends at the plug-in
    and the open window starts, although the person pressed Stop before plugging in."""
    world = await pause_world(hass, timers, connected=False, plan=two_windows())
    await world.plug.set(None)
    await world.executor.async_manual_stop()
    assert world.pause.manual
    starts = len(world.starts)

    await world.plug.set(True)
    await install_schedule(world.controller, open_window())  # Auto replans at the plug-in
    await world.executor.async_start_on_plug_in()
    await hass.async_block_till_done()

    assert world.pause.manual, "the Stop ended at the very plug-in it was given before"
    assert len(world.starts) == starts, "the charger starts although the person said Stop"
    await world.shutdown()


async def test_r3_a_stop_that_cannot_store_its_pause_still_stops_the_charger(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """The settings write of the manual pause fails (storage error): the person's Stop never reaches the
    charger, which keeps charging."""
    world = await pause_world(hass, timers, plan=two_windows())
    assert world.controller.charging

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    world.store.async_update = broken  # type: ignore[method-assign]
    with pytest.raises(Exception):
        await world.executor.async_manual_stop()

    assert not world.controller.charging, "a person's Stop did not stop the charger"
    await world.shutdown()


async def test_r4_legacy_person_stopped_with_an_open_window_does_not_start_at_upgrade(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """An older release stored person_stopped beside a plan whose window is open. The upgrade's restore
    re-arms the plan (and starts its open window) before the executor migrates person_stopped."""
    world = await pause_world(hass, timers, plan=two_windows())
    await world.controller.async_stop()  # the older release's person's Stop: plan kept, charger off
    assert not world.controller.charging and world.controller.plan is not None
    saved = await world.controller._store.async_load()  # noqa: SLF001
    saved["person_stopped"] = True
    await world.controller._store.async_save(saved)  # noqa: SLF001
    starts = len(world.starts)

    restarted = await world.restart()

    assert _manual(restarted) == (PAUSE_MANUAL, MANUAL_STOP, MANUAL_SCOPE_PLUG_IN)
    assert len(world.starts) == starts, "the window started although the stored Stop is now a manual pause"
    assert not restarted.controller.charging
    await restarted.shutdown()


async def test_r5_start_under_an_until_resumed_pause_is_resumed_after_balancing_pauses_it(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Auto paused 'until I resume'; the person presses Start; load balancing pauses it below the floor.
    The regulator's resume is refused by the gate (balancing_resume under a non-manual pause)."""
    world = await pause_world(hass, timers)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    await world.executor.async_manual_start(10)
    assert world.controller.charging and world.pause.choice == PAUSE_UNTIL_RESUMED
    await world.controller._regulated_stop("pause")  # noqa: SLF001
    assert world.controller.paused_by_balancing

    assert await world.controller.async_battery_probe_start(8), "the person's charge is never resumed"
    await world.shutdown()


async def test_r6_start_with_no_car_under_a_stop_for_the_next_plug_in(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Stop with no car (next_plug_in), then Start with no car. Spec A3: Start with no car is refused. Here
    the command goes out, the Stop pause stays, and the charger charges at plug-in under 'Stopped
    manually'."""
    world = await pause_world(hass, timers, connected=False)
    await world.executor.async_manual_stop()
    starts = len(world.starts)
    try:
        await world.executor.async_manual_start()
    except Exception:  # noqa: BLE001
        pass
    await world.plug.set(True)
    contradictory = world.controller.charging and world.pause.action == MANUAL_STOP
    assert len(world.starts) == starts or world.pause.action == MANUAL_START, (
        f"start sent with no car; pause {_manual(world)}, charging={world.controller.charging}"
    )
    assert not contradictory
    await world.shutdown()


async def test_r2b_harm_the_window_starts(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, connected=False, plan=two_windows())
    await world.plug.set(None)
    await world.executor.async_manual_stop()
    starts = len(world.starts)
    await world.plug.set(True)
    await hass.async_block_till_done()
    await install_schedule(world.controller, open_window())
    await world.executor.async_start_on_plug_in()
    await hass.async_block_till_done()
    assert len(world.starts) == starts, f"started {len(world.starts) - starts} time(s); charging={world.controller.charging}"
    await world.shutdown()


async def test_r7_stop_without_car_then_charger_starts_by_itself_at_plug_in(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """'Do not charge when I plug in': the charger (free-charging OCPP, Easee without auth) begins by
    itself at the plug-in. Before 1.11 Auto's kept plan held such a charge back outside a window; now the
    Stop cleared the plan and nothing stops it."""
    world = await pause_world(hass, timers, connected=False, plan=later_window())
    await world.executor.async_manual_stop()
    stops = len(world.stops)
    await world.plug.set(True, control="on")  # begins by itself at the plug-in
    await hass.async_block_till_done()
    assert world.pause.manual and world.pause.action == MANUAL_STOP
    assert not world.controller.charging, f"charging under a person's Stop (stops sent: {len(world.stops) - stops})"
    await world.shutdown()
