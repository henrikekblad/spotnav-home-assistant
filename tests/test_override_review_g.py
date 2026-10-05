"""Adversarial review round G: the ownership core merged with 1.11, its findings kept as tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_CHARGE_CONTROL
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.auto_execution import AutoExecutor
from custom_components.spotnav.execution.controller import ChargingController, SESSION_STORE_KEY

from .pause_world import ENTRY, later_window, pause_world, SWITCH, World
from .relay import FakeScheduler
from .test_replug import Plug


@pytest.fixture
def drives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", True)


async def _ha_restart(world: World) -> World:
    """Home Assistant stops and starts again, as it really does: `EVENT_HOMEASSISTANT_STOP` shuts the boundary
    down (`__init__._async_stop`), but config entries are not unloaded, so `ChargingController.async_shutdown` never
    runs. What is on disk is what the store holds when the process ends."""
    hass = world.hass
    await world.executor.async_shutdown()
    disk = dict(await world.controller._store.async_load())  # noqa: SLF001
    # The process ends: nothing of the old controller runs again (its shutdown is only to drop its listeners).
    world.controller._cancel_session_save()  # noqa: SLF001
    await world.controller.async_shutdown()
    await world.controller._store.async_save(disk)  # noqa: SLF001
    controller = ChargingController(hass, ENTRY, {CONF_CHARGE_CONTROL: SWITCH})
    executor = AutoExecutor(hass, controller, world.store)
    plug = Plug.__new__(Plug)
    plug.hass, plug.control, plug.connected = hass, SWITCH, world.plug.connected
    controller.adapter.vehicle_connected = lambda: plug.connected  # type: ignore[method-assign]
    await executor.async_start()
    await controller.async_initialize()
    await executor.async_after_restore()
    await hass.async_block_till_done()
    return World(hass, controller, executor, world.store, plug, world.timers, world.starts, world.stops)


# ---------------------------------------------------------------------------------------------- (b) restart


@pytest.fixture(params=[False, True], ids=["off", "on"])
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


@pytest.mark.shadow_disagreement_expected
async def test_rev_g_suns_charge_survives_a_real_ha_restart(
    hass: HomeAssistant, timers: FakeScheduler, mode: bool
) -> None:
    """The sun's start (`cause="solar"`, as `AutoExecutor.async_solar_start` sends it) with a plan window ahead, then Home Assistant restarts within the session
    record's debounce. Today's keys say `charge_origin=solar`, which today's re-arm spares; the core's record was last written inside the start
    (owner none, the start's result not back yet) and the change after it waits for a later decision or a shutdown
    that HA's stop never calls. The restore takes the stale record as the truth and clears today's origin."""
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    assert await world.controller.async_start(cause="solar")
    await hass.async_block_till_done()
    assert world.controller.charge_origin == "solar"
    stored = (await world.controller._store.async_load()).get(SESSION_STORE_KEY)  # noqa: SLF001
    stops = len(world.stops)

    restarted = await _ha_restart(world)
    origin = restarted.controller.charge_origin
    await restarted.switch("on")
    hass.states.async_set(SWITCH, "on", {"report": "after the restart"})
    await hass.async_block_till_done()
    assert (origin, len(world.stops) - stops) == ("solar", 0), (
        f"after the restart: origin={origin!r}, stops sent={len(world.stops) - stops} "
        f"(the record on disk said owner={stored.get('owner') if stored else None!r})"
    )
    await restarted.shutdown()


# ---------------------------------------------------------------------------------------------- (a) inertness


async def test_rev_g_off_the_stop_under_a_persons_stop_reads_no_state_of_charge(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Option off. Inside a window, after the car ended a person's charge (known full), with the hold guard
    blocking (an Auto plan under the person's Stop, as `__init__` wires it): the charger begins by itself and the
    stop under the person's Stop is sent. Main reads no state of charge for that stop; the branch's
    `_recheck_facts` calls `_car_ended_holds_window` -> `_car_ended_need_grew` -> `SocReader.read`, which may move
    and save the SoC anchor (`soc_estimate.SocReader.read` -> `_save`): a read and a storage write the shadow adds."""
    from homeassistant.util import dt as dt_util

    from .pause_world import two_windows

    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", False, raising=False)
    world = await pause_world(hass, timers, plan=two_windows())
    controller = world.controller
    controller.set_hold_guard(lambda: True)
    await world.executor.async_manual_stop()
    # A schedule installed after the Stop (the person's Stop clears the plan it found; a webhook or a service call
    # installs another), with a window open now.
    from .helpers import install_schedule

    await install_schedule(controller, two_windows())
    await hass.async_block_till_done()
    assert controller.plan is not None
    reads: list[Any] = []
    controller._soc_reader = lambda vehicle: reads.append(vehicle)  # noqa: SLF001
    controller._car_ended_at = dt_util.utcnow() - timedelta(minutes=5)  # noqa: SLF001
    controller._car_ended_soc = 90  # noqa: SLF001
    monkeypatch.setattr(controller, "_car_ended_known_full", lambda: True)
    stops = len(world.stops)

    await world.switch("on")
    assert len(world.stops) == stops + 1, "the stop under the person's Stop went out"
    assert reads == [], f"the stop read the car's state of charge {len(reads)} time(s)"
    await world.shutdown()


@pytest.mark.parametrize("what", ["claim", "hold", "stray", "person_hold"])
async def test_rev_g_off_a_recheck_today_declines_reads_no_state_of_charge(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, what: str
) -> None:
    """Option off. A background task's re-check whose rule today short-circuits (Auto holds it back) reads nothing
    of the car: the shadow's facts take the car-ended rule's answer from today's own read, never one of their own."""
    from homeassistant.util import dt as dt_util

    from .pause_world import two_windows

    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", False, raising=False)
    world = await pause_world(hass, timers, plan=two_windows())
    controller = world.controller
    reads: list[Any] = []
    controller._soc_reader = lambda vehicle: reads.append(vehicle)  # noqa: SLF001
    controller._car_ended_at = dt_util.utcnow() - timedelta(minutes=5)  # noqa: SLF001
    controller._car_ended_soc = 90  # noqa: SLF001
    monkeypatch.setattr(controller, "_car_ended_known_full", lambda: True)
    sent: list[str] = []

    async def act() -> bool:
        sent.append(what)
        return True

    async with controller._lock:  # noqa: SLF001
        assert not await controller._recheck(what, lambda: False, act)  # noqa: SLF001
    assert (sent, reads) == ([], []), f"the re-check sent {sent} and read the car's state of charge {len(reads)} time(s)"
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- (c) leaks


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
async def test_rev_g_shadow_diagnostics_name_no_entity(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    import json
    import re

    from .pause_world import two_windows

    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", on)
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_start()
    await world.executor.async_manual_stop()
    await world.switch("on")
    await world.plug.set(False)
    await world.plug.set(True)
    await world.controller.async_start(cause="solar")
    await world.controller.async_stop()
    text = json.dumps(world.controller.ownership_shadow.diagnostics(), default=str)
    entityish = sorted(set(re.findall(r"\b[a-z_]+\.[a-z0-9_]+\b", text)))
    assert "switch.a" not in text and ENTRY not in text and not entityish, entityish
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- (b) give-up counting


@pytest.mark.parametrize("gap_s", [5, 31, 200])
async def test_rev_g_give_up_counts_the_same_in_both_modes(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, freezer: Any, gap_s: int
) -> None:
    """Could not break: a charger that begins again by itself under a person's Stop, ten times, `gap_s` apart, is
    sent the same stops and reaches the give-up at the same report whether the core drives or not."""
    seen: dict[bool, list[tuple[int, bool]]] = {}
    for on in (False, True):
        monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", on)
        world = await pause_world(hass, timers)
        await world.executor.async_manual_stop()
        stops = len(world.stops)
        seen[on] = []
        for _ in range(10):
            freezer.tick(timedelta(seconds=gap_s))
            await world.switch("off")
            await world.switch("on")
            seen[on].append((len(world.stops) - stops, world.controller._person_hold_gave_up))  # noqa: SLF001
        await world.shutdown()
        hass.services.async_remove("switch", "turn_on")
        hass.services.async_remove("switch", "turn_off")
    assert seen[True] == seen[False], seen


# ---------------------------------------------------------------------------------------------- pre-existing, copied


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
async def test_rev_g_preexisting_a_replan_outside_the_windows_leaves_a_charge_now_running(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    """Pre-existing in main, copied by the core (`ownership._rearm`): a window's end spares a Charge-now start
    (`WINDOW_END_SPARED`, I3) but the re-arm outside the windows spares only a person's and the sun's, so a Charge-now
    started between windows (a webhook start, a button with no Auto) is stopped by the next plan installed."""
    from .helpers import install_schedule

    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", on)
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    assert await world.controller.async_start()
    await hass.async_block_till_done()
    stops = len(world.stops)
    await install_schedule(world.controller, {"periods": [later_window(hours=3)], "amps": 10})
    await hass.async_block_till_done()
    assert len(world.stops) == stops, "the replan stopped the Charge-now charge"
    await world.shutdown()


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
async def test_a_replan_outside_the_windows_leaves_a_persons_start_running(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    """The webhook's and the card's Start (`api.webhook.async_manual_action` -> `AutoExecutor.async_manual_start`)
    between the plan's windows: the next plan installed stops nothing."""
    from .helpers import install_schedule

    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", on)
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert world.controller.charging
    stops = len(world.stops)
    await install_schedule(world.controller, {"periods": [later_window(hours=3)], "amps": 10})
    await hass.async_block_till_done()
    assert len(world.stops) == stops, "the replan stopped the person's Start"
    await world.shutdown()


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
async def test_a_restart_outside_the_windows_leaves_a_charge_now_running(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    """A Charge-now start with no Auto between the plan's windows, then a restart: the re-arm at the restore stops
    nothing (only the plan's own charge is the plan's to stop)."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", on)
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    assert await world.controller.async_start()
    await hass.async_block_till_done()
    assert world.controller.charge_origin == "other"
    stops = len(world.stops)
    restarted = await world.restart()
    hass.states.async_set(SWITCH, "on", {"report": "after the restart"})
    await hass.async_block_till_done()
    assert len(world.stops) == stops, "the re-arm at the restore stopped the Charge-now charge"
    assert restarted.controller.charge_origin == "other"
    await restarted.shutdown()


@pytest.mark.usefixtures("offline_relay")
@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
async def test_rev_g_preexisting_the_departure_does_not_cycle_a_charge_the_next_plan_continues(
    hass: HomeAssistant, transport: Any, monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    """The author's lead. 08:00, departure 10:00, far more asked than two hours give (best effort, rising prices).
    At 10:00 the last window's end and the departure's replan fall due together. In every run here (branch both
    modes, main) the last window's end goes first: the plan's charge is stopped and the next departure's plan,
    whose first window opens at 10:00, starts it again at once (turn_off, turn_on in the same second). The other
    order (replan first) keeps it running. A needless contactor cycle at the departure, not made worse by the core."""
    from datetime import time

    from freezegun import freeze_time
    from homeassistant.core import callback
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from .relay import serve as serve_prices
    from .test_dashboard_api import NOW
    from .test_replug import _car

    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", on)
    serve_prices(transport, rising=True)
    calls: list[tuple[str, int]] = []
    with freeze_time(NOW) as frozen:
        begin = dt_util.utcnow()

        @callback
        def on_call(event: Any) -> None:
            data = event.data
            if data["service"] in ("turn_on", "turn_off"):
                calls.append((data["service"], int((dt_util.utcnow() - begin).total_seconds())))
                targets = data["service_data"]["entity_id"]
                for entity_id in [targets] if isinstance(targets, str) else targets:
                    hass.states.async_set(entity_id, "on" if data["service"] == "turn_on" else "off")

        hass.bus.async_listen("call_service", on_call)
        await _car(hass, frozen, kwh=100.0, departure=time(10, 0), departure_enabled=True)
        for delta in (timedelta(hours=1, minutes=59), timedelta(minutes=1), timedelta(minutes=20)):
            frozen.tick(delta)
            async_fire_time_changed(hass, dt_util.utcnow())
            await hass.async_block_till_done()
    at_departure = [call for call in calls if call[1] == 7200]
    assert at_departure != [("turn_off", 7200), ("turn_on", 7200)], calls


@pytest.mark.usefixtures("offline_relay")
@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
@pytest.mark.parametrize("first", ["window_end", "next_plan"])
async def test_the_next_plan_beginning_at_the_departure_takes_the_charge_over_in_either_order(
    hass: HomeAssistant, transport: Any, monkeypatch: pytest.MonkeyPatch, on: bool, first: str
) -> None:
    """A best-effort plan to a 10:00 departure, and the next departure's plan waiting for the boundary with its first
    window opening at 10:00. Whether the last window's end or the next plan's install comes first, the charge goes
    on: no stop, no start, and the plan's end recorded as no `plan_done`."""
    from datetime import time

    from freezegun import freeze_time
    from homeassistant.core import callback
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from .relay import serve as serve_prices
    from .test_dashboard_api import NOW
    from .test_replug import _car

    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", on)
    serve_prices(transport, rising=True)
    calls: list[tuple[str, int]] = []
    with freeze_time(NOW) as frozen:
        begin = dt_util.utcnow()

        @callback
        def on_call(event: Any) -> None:
            data = event.data
            if data["service"] in ("turn_on", "turn_off"):
                calls.append((data["service"], int((dt_util.utcnow() - begin).total_seconds())))
                targets = data["service_data"]["entity_id"]
                for entity_id in [targets] if isinstance(targets, str) else targets:
                    hass.states.async_set(entity_id, "on" if data["service"] == "turn_on" else "off")

        hass.bus.async_listen("call_service", on_call)
        car = await _car(hass, frozen, kwh=100.0, departure=time(10, 0), departure_enabled=True)
        frozen.tick(timedelta(hours=1, minutes=59))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        old_plan = car.controller.plan
        frozen.tick(timedelta(minutes=1))
        assert car.executor.successor_continues(), "the next departure's plan waits for the boundary"
        if first == "next_plan":
            await car.executor.async_apply_pending()
            assert car.controller.plan is not old_plan
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert car.controller.plan is not old_plan, "the next plan is installed"
        assert car.controller.charging
        completion = car.controller.completion_record
        frozen.tick(timedelta(minutes=20))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert [call for call in calls if call[1] >= 7200] == [], calls
    assert completion is None or completion.get("reason") != "plan_done", completion


async def test_a_next_plan_that_is_not_installed_after_all_leaves_the_window_end_as_ever(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary said the next plan takes the charge over, but its install did not happen (the proposal went stale
    meanwhile): the plan's last window's end stops the plan's charge as it always did."""
    from homeassistant.util import dt as dt_util

    from .pause_world import open_window

    world = await pause_world(hass, timers, plan={"periods": [open_window()], "amps": 10})
    assert world.controller.charging
    monkeypatch.setattr(world.executor, "successor_continues", lambda: True)
    applied: list[bool] = []

    async def apply() -> None:
        applied.append(True)

    monkeypatch.setattr(world.executor, "async_apply_pending", apply)
    stops = len(world.stops)
    world.controller._async_final_end_callback(dt_util.utcnow())  # noqa: SLF001 - the last window's end is due
    await hass.async_block_till_done()
    assert applied == [True] and len(world.stops) == stops + 1
    await world.shutdown()
