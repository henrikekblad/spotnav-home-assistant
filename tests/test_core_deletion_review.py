"""What the review of the deletion step found, pinned: a check of the person intent that waits for another task's
feed, a strategy change the core cannot decide kept as it is, an event whose decision at once failed not decided again,
no feed left open behind a write that raised, and the upgrade from today's keys alone (a charger an earlier release ran
with the previous logic) for each group the core keeps."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.core import events as ev
from custom_components.spotnav.core.session import ChargeSession, ManualPause
from custom_components.spotnav.execution import auto_execution as auto_execution_module
from custom_components.spotnav.execution import session_runner
from custom_components.spotnav.execution.controller import SESSION_ORDER_KEY, SESSION_STORE_KEY
from custom_components.spotnav.execution.session_runner import SessionRunner

from .pause_world import later_window, pause_world, SWITCH, two_windows, World
from .relay import FakeScheduler


def _break_once(monkeypatch: pytest.MonkeyPatch, kind: str) -> list[str]:
    """The core raises for the first event of `kind`, and decides every other as ever."""
    real = session_runner.decide
    broken: list[str] = []

    def decide(session: Any, event: Any, now: Any) -> Any:
        if event.kind == kind and not broken:
            broken.append(kind)
            raise RuntimeError(f"the core cannot decide {kind}")
        return real(session, event, now)

    monkeypatch.setattr(session_runner, "decide", decide)
    return broken


# ---------------------------------------------------------------------------------------------- the intent's check


@pytest.mark.core_failure_expected
async def test_an_intent_check_asked_for_while_another_feed_is_open_runs_once_it_closes() -> None:
    boundary: dict[str, Any] = {"manual": None}
    runner = SessionRunner(intent=lambda: (boundary["manual"], None))
    gate = asyncio.Event()

    async def other_task_feed() -> None:
        token = runner.begin()  # a re-check's feed, open across its service call
        await gate.wait()
        runner.end(token, ev.Timer(control_on=True, start_pending=False, hold_stop_overdue=False))

    task = asyncio.create_task(other_task_feed())
    await asyncio.sleep(0)
    boundary["manual"] = ManualPause("stop", "plug_in")
    runner.check_intent("person_stop")
    assert runner.session.manual is None, "waits: the other feed's decision may not be written yet"
    gate.set()
    await task
    assert runner.session.manual == ManualPause("stop", "plug_in") and runner.session.held_off_by_person
    assert runner.counts["failures"] == {"intent/-": 1}, "a difference is a failure, told"


@pytest.mark.core_failure_expected
async def test_a_persons_stop_the_core_could_not_be_told_during_another_command_still_holds_the_charger_off(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person's Stop whose connection cannot be read (the core is not told), while another task's feed is open: the
    pause they stored becomes the session's once that feed closes, so a charge the charger then begins by itself is
    stopped (C7)."""
    world = await pause_world(hass, timers)
    controller = world.controller
    runner = controller.session_runner
    real = controller.adapter.vehicle_connected
    calls: list[int] = []

    def unreadable_once() -> bool | None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the connection cannot be read")
        return real()

    monkeypatch.setattr(controller.adapter, "vehicle_connected", unreadable_once)
    gate = asyncio.Event()

    async def other_command() -> None:
        token = runner.begin()
        await gate.wait()
        runner.cancel(token)

    task = asyncio.create_task(other_command())
    await asyncio.sleep(0)
    await world.executor.async_manual_stop()
    assert world.executor.pause_intent.manual and runner.session.manual is None
    gate.set()
    await task
    await hass.async_block_till_done()
    assert runner.session.held_off_by_person
    stops = len(world.stops)
    await world.switch("on")  # the charger begins by itself
    assert len(world.stops) == stops + 1 and hass.states.get(SWITCH).state == "off"
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- Keep


@pytest.mark.core_failure_expected
async def test_a_strategy_change_the_core_cannot_decide_keeps_the_plan_and_its_charge(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep: nothing is stopped and the plan stays applied with its charge the plan's, so its window's end still ends
    it; the next reconcile asks again and the core decides then."""
    from dataclasses import replace

    world = await pause_world(hass, timers, plan=two_windows())
    executor, controller = world.executor, world.controller
    assert controller.charging and controller.charge_origin == "plan_window"
    assert executor.applied is not None, "an Auto plan applied"
    monkeypatch.setattr(auto_execution_module, "application_for", lambda *_args, **_kwargs: None)
    settings = replace(world.store.settings("entry_a"), strategy=STRATEGY_SOLAR)
    broken = _break_once(monkeypatch, "strategy_change")
    stops = len(world.stops)
    async with executor._lock:  # noqa: SLF001
        result = await executor._reconcile_locked(settings, object(), attempt=executor.begin_attempt())  # noqa: SLF001
    await hass.async_block_till_done()
    assert broken and result is not None and executor.applied is not None, "still applied"
    assert len(world.stops) == stops and controller.plan is not None
    assert controller.charge_origin == "plan_window" and controller.session_runner.session.owner == "plan"
    assert controller.session_runner.counts["failures"] == {"decide_early/strategy_change": 1}
    await world.shutdown()


@pytest.mark.core_failure_expected
def test_an_event_whose_decision_at_once_failed_is_not_decided_again_at_its_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = session_runner.decide

    def decide(session: Any, event: Any, now: Any) -> Any:
        calls.append(event.kind)
        if event.kind == "direct_start" and calls.count("direct_start") == 1:
            raise RuntimeError("the core cannot decide it now")
        return real(session, event, now)

    monkeypatch.setattr(session_runner, "decide", decide)
    runner = SessionRunner()
    runner.session = ChargeSession(plugged=True)
    token = runner.begin(early=ev.DirectStart(manual=False, cause="other"))
    runner.end(token, ev.DirectStart(manual=False, cause="other"))
    assert calls == ["direct_start"], "decided once, at once, and that failed"
    assert runner.session == ChargeSession(plugged=True), "no start awaited that nobody sent"
    assert runner.counts["failures"] == {"decide_early/direct_start": 1}


# ---------------------------------------------------------------------------------------------- no feed left open


async def test_a_resume_that_raises_after_its_write_leaves_no_feed_open(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await pause_world(hass, timers)
    await world.executor.async_manual_stop()
    runner = world.controller.session_runner

    def broken() -> None:
        raise RuntimeError("the watch could not be set")

    real = world.executor._sync_manual_watch  # noqa: SLF001
    monkeypatch.setattr(world.executor, "_sync_manual_watch", broken)
    with pytest.raises(RuntimeError):
        await world.executor.async_resume()
    monkeypatch.setattr(world.executor, "_sync_manual_watch", real)
    assert not runner._any_open(), "no feed left open"  # noqa: SLF001
    assert runner.session.manual is None, "the resume the store holds is the session's"
    await world.shutdown()


# ---------------------------------------------------------------------------------------------- the upgrade from keys


async def _as_the_previous_logic_left_it(world: World) -> None:
    """Today's keys alone on disk, as a charger an earlier release ran with the previous logic left them: the record
    of the core's is dropped, and the controller going down saves nothing more."""
    controller = world.controller
    controller._cancel_session_save()  # noqa: SLF001
    await world.hass.async_block_till_done()
    record = dict(await controller._store.async_load())  # noqa: SLF001
    record.pop(SESSION_STORE_KEY, None)
    record.pop(SESSION_ORDER_KEY, None)
    await controller._store.async_save(record)  # noqa: SLF001

    async def nothing() -> None:
        return None

    controller._async_save = nothing  # type: ignore[method-assign]  # noqa: SLF001


async def _upgraded(world: World) -> World:
    await _as_the_previous_logic_left_it(world)
    restarted = await world.restart()
    stored = await restarted.controller._store.async_load()  # noqa: SLF001
    assert SESSION_STORE_KEY in stored, "migrated once and written at once"
    return restarted


@pytest.mark.usefixtures("both_restarts")
async def test_an_upgrade_keeps_a_balancing_pause_and_its_regulator_gives_the_charge_back(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """Load balancing held a person's charge back; after the upgrade its memory is the session's, and the regulator's
    resume gives the charge back as the person's."""
    world = await pause_world(hass, timers)
    await world.executor.async_manual_start(10)
    await world.controller._regulated_stop("pause")  # noqa: SLF001 - the regulator's own pause
    await hass.async_block_till_done()
    assert world.controller.paused_by_balancing and world.controller.paused_charge_origin == "manual"

    restarted = await _upgraded(world)
    controller = restarted.controller
    session = controller.session_runner.session
    assert (session.balancing_paused, session.paused_origin) == (True, "person")
    assert controller.balancing_holds("manual")
    assert await controller.async_battery_probe_start(8)
    await hass.async_block_till_done()
    assert controller.charging and controller.charge_origin == "manual" and not controller.paused_by_balancing
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_an_upgrade_keeps_a_held_plug_in_session(hass: HomeAssistant, timers: FakeScheduler) -> None:
    """The charger began by itself outside the windows and was held; after the upgrade the hold is the session's, so
    the person starting it again is their override, not stopped a second time."""
    world = await pause_world(hass, timers, plan={"periods": [later_window()], "amps": 10})
    await world.switch("on")
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "off" and world.controller.session_runner.session.held

    restarted = await _upgraded(world)
    session = restarted.controller.session_runner.session
    assert session.held and not session.overridden
    stops = len(world.stops)
    await restarted.switch("on")
    await hass.async_block_till_done()
    assert len(world.stops) == stops and hass.states.get(SWITCH).state == "on", "a person's override goes on"
    assert restarted.controller.session_runner.session.overridden
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_an_upgrade_keeps_a_car_ended_wait(hass: HomeAssistant, timers: FakeScheduler) -> None:
    from .test_core_car_ended_memory import _plan_ended, _record
    from .test_car_ended_waits_for_level import _estimate

    world = await _plan_ended(hass, timers, _estimate())
    before = _record(world)
    assert before["car_ended_wait"] is not None

    restarted = await _upgraded(world)
    assert _record(restarted) == before
    ended = restarted.controller.session_runner.session.car_ended
    assert ended is not None and ended.wait is not None and ended.wait.to_dict() == before["car_ended_wait"]
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_an_upgrade_keeps_a_persons_stop(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan=two_windows())
    await world.executor.async_manual_stop()
    starts = len(world.starts)

    restarted = await _upgraded(world)
    session = restarted.controller.session_runner.session
    assert session.manual == ManualPause("stop", "plug_in")
    assert len(world.starts) == starts, "the open window does not start under the person's Stop"
    await restarted.switch("on")  # the charger begins by itself: stopped (C7)
    assert hass.states.get(SWITCH).state == "off"
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_an_upgrade_keeps_a_persons_start(hass: HomeAssistant, timers: FakeScheduler) -> None:
    world = await pause_world(hass, timers, plan={"periods": [later_window(0.0)], "amps": 10})
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert world.controller.charge_origin == "manual"

    restarted = await _upgraded(world)
    controller = restarted.controller
    session = controller.session_runner.session
    assert session.owner == "person" and session.manual == ManualPause("start", "plug_in")
    stops = len(world.stops)
    hass.states.async_set(SWITCH, "on", {"report": "after the upgrade"})
    await hass.async_block_till_done()
    assert len(world.stops) == stops and controller.charge_origin == "manual", "the person's charge goes on"
    await restarted.shutdown()
