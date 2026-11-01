"""The owner in the core's session (the core end state plan, step 7): while the core drives, who owns the charge is the
session's `owner`, and today's `charge_origin` and `plan_charge` are derived from it (`controller._CORE_OWNER_ORIGIN`),
read from it, and still written to today's keys, so an older build reads them as before. With the option off,
today's fields stay the truth. Every test here runs in both modes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.execution.controller import _CORE_OWNER_ORIGIN, SESSION_STORE_KEY
from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.runtime import executor_for

from .helpers import install_schedule
from .pause_world import ENTRY, open_window, pause_world, SWITCH, World
from .relay import FakeScheduler
from .world import solar_setup

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture(params=[True], ids=["core"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    return request.param


@pytest.mark.parametrize("cause", [None, "other"])
async def test_the_suns_take_over_never_stops_a_charge_now_start_on_its_way(
    hass: HomeAssistant, drives: bool, cause: str | None
) -> None:
    """A Charge-now start (a webhook's, a caller of `async_start`) whose charger reports charging before the command
    returns: the sun's take-over, decided meanwhile under the boundary's lock alone, must not take it for a charge the
    charger began by itself and stop it once the start is done."""
    charger, _site, controller, _coordinator, _clock, _on, turn_off_calls = await solar_setup(
        hass, strategy=STRATEGY_SOLAR
    )
    executor = executor_for(hass, charger.entry_id)
    switch = f"switch.{charger.entry_id}"
    answered = asyncio.Event()
    release = asyncio.Event()

    async def slow_start(*_args, **_kwargs) -> bool:
        # The charger takes the start and reports charging; the command itself returns later.
        hass.states.async_set(switch, "on")
        answered.set()
        await release.wait()
        return True

    controller.adapter.async_start = slow_start  # type: ignore[method-assign]
    start = hass.async_create_task(controller.async_start(cause=cause))
    await answered.wait()
    assert controller.charging and not controller.self_started_charge()

    take_over = hass.async_create_task(executor.async_solar_take_over_stop())
    await asyncio.sleep(0)
    release.set()
    assert await start
    stopped = await take_over
    await hass.async_block_till_done()

    assert not stopped and turn_off_calls == [], "the sun does not stop a Charge-now start"
    assert controller.charge_origin == "other"
    if drives:
        assert controller.session_runner.session.owner == "charge_now"


# ---------------------------------------------------------------------------------------------- a bare charger


async def _stored(world: World) -> dict[str, Any]:
    await world.hass.async_block_till_done()
    return await world.controller._store.async_load()  # noqa: SLF001


def _owner(world: World) -> tuple[str | None, bool]:
    return world.controller.charge_origin, world.controller._is_plan_charge  # noqa: SLF001


async def _keys_follow(world: World, drives: bool, expected: tuple[str | None, bool]) -> None:
    """The owner is `expected`, and today's keys as saved say the same; driving, so does the core's own record, and
    the session is the truth both are derived from."""
    assert _owner(world) == expected
    stored = await _stored(world)
    assert (stored["charge_origin"], stored["plan_charge"]) == expected, "today's keys never lag the owner"
    if drives:
        session = world.controller.session_runner.session
        assert _CORE_OWNER_ORIGIN.get(session.owner, (None, False)) == expected
        assert stored[SESSION_STORE_KEY]["owner"] == session.stored().owner


async def _start(world: World, owner: str) -> None:
    """A start of SpotNav's for `owner`: a person's Start through the boundary, the plan's window, or a start through
    the charger's own entry naming who asked."""
    if owner == "person":
        await world.executor.async_manual_start()
    elif owner == "plan":
        await install_schedule(world.controller, open_window())
    else:
        cause = {"solar": "solar", "charge_now": None, "min_soc": "min_soc"}[owner]
        assert await world.controller.async_start(cause=cause)
    await world.hass.async_block_till_done()


ORIGINS = {
    "person": ("manual", False),
    "plan": ("plan_window", True),
    "solar": ("solar", False),
    "charge_now": ("other", False),
    "min_soc": ("min_soc", False),
}


@pytest.mark.parametrize("owner", list(ORIGINS))
async def test_the_saved_keys_follow_every_owner_change(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool, owner: str
) -> None:
    """After each event that changes the owner, today's `charge_origin`/`plan_charge` as saved are the session's: a
    rollback, or an older build, reads the right owner. The saves inside a start's or a stop's feed run before its end
    decides the owner; the record saved after it writes the keys again, derived from the session."""
    world = await pause_world(hass, timers)
    await _keys_follow(world, drives, (None, False))
    await _start(world, owner)
    await _keys_follow(world, drives, ORIGINS[owner])
    # The charger is seen off by itself: nobody's.
    await world.switch("off")
    await _keys_follow(world, drives, (None, False))
    await _start(world, owner)
    await _keys_follow(world, drives, ORIGINS[owner])
    # A person's Stop: nobody's.
    await world.executor.async_manual_stop()
    await hass.async_block_till_done()
    await _keys_follow(world, drives, (None, False))
    await world.shutdown()


async def test_readers_are_told_the_owner_at_once(hass: HomeAssistant, timers: FakeScheduler, drives: bool) -> None:
    """Every reader (the dashboard, the grid-charging sensor) is told an owner change at once: a person's Start
    reads `manual` at the notification that follows it, and the charger seen off reads nobody's."""
    world = await pause_world(hass, timers)
    told: list[str | None] = []
    world.controller.add_listener(lambda: told.append(world.controller.charge_origin))
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert told and told[-1] == "manual", told
    told.clear()
    await world.switch("off")
    assert told and told[-1] is None, told
    await world.executor.async_resume()
    # The floor's charge handed to the sun with no command: decided in the boundary's feed, told after it.
    assert await world.executor.async_min_soc_start(None)
    await hass.async_block_till_done()
    assert told[-1] == "min_soc", told
    await world.store.async_update(ENTRY, mutate=lambda settings: replace(settings, strategy=STRATEGY_SOLAR))
    told.clear()
    assert await world.executor.async_min_soc_end()
    await hass.async_block_till_done()
    assert told and told[-1] == "solar", told
    assert (await _stored(world))["charge_origin"] == "solar"
    await world.shutdown()


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("owner", list(ORIGINS))
async def test_a_restart_keeps_every_owner(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool, owner: str
) -> None:
    """A restart during each owner's charge, in both restart kinds: the owner read back is the one before it, from the
    core's own record where it drives (today's keys, written beside it, say the same)."""
    world = await pause_world(hass, timers)
    await _start(world, owner)
    assert hass.states.get(SWITCH).state == "on"
    await _keys_follow(world, drives, ORIGINS[owner])

    restarted = await world.restart()
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "on", "nothing stopped at the restart"
    await _keys_follow(restarted, drives, ORIGINS[owner])
    await restarted.shutdown()


async def test_a_restart_from_todays_keys_alone_reads_the_owner(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """An older build's record (today's keys, no record of the core's own): the owner is read from them, and a charger
    the core drives writes its own record from them at once."""
    world = await pause_world(hass, timers, charging=True)
    stored = await _stored(world)
    stored.pop(SESSION_STORE_KEY, None)
    await world.controller.async_shutdown()
    await world.executor.async_shutdown()
    await world.controller._store.async_save(  # noqa: SLF001
        {**stored, "charge_origin": "manual", "plan_charge": False}
    )
    restarted = await world._start_again()  # noqa: SLF001 - the old controller is down already
    await _keys_follow(restarted, drives, ("manual", False))
    if drives:
        assert restarted.controller.session_runner.session.owner == "person"
        assert (await _stored(restarted))[SESSION_STORE_KEY]["owner"] == "person"
    await restarted.shutdown()


async def test_a_start_that_fails_in_the_middle_of_a_report_owns_nothing(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """A start whose command never goes out, while a report lands on its way: nobody owns the charge after it (I4:
    today's code takes its two fields back, the core never gives the start an owner), with no mirror drift."""
    world = await pause_world(hass, timers)
    controller = world.controller

    async def refused(*_args: Any, **_kwargs: Any) -> bool:
        current = hass.states.get(SWITCH)
        hass.states.async_set(SWITCH, "off", {**(current.attributes if current else {}), "stamp": "report"})
        for _ in range(5):
            await asyncio.sleep(0)
        return False

    controller.adapter.async_start = refused  # type: ignore[method-assign]
    assert not await controller.async_start()
    await hass.async_block_till_done()
    assert _owner(world) == (None, False)
    shadow = controller.session_runner
    if drives:
        assert shadow.session.owner == "none" and not shadow.session.start_pending
    assert (await _stored(world))["charge_origin"] is None
    await world.shutdown()


async def test_a_dropped_plan_keeps_its_charge_the_plans(hass: HomeAssistant, timers: FakeScheduler, drives: bool) -> None:
    """A person's Start drops Auto's plan (`async_drop_plan`): with the option off today's code clears `plan_charge`
    and keeps `charge_origin`; the core keeps the owner `plan` (pinned in `test_core_ownership`), so driving reads
    `plan_charge` True with no plan. Nothing reads it to act without a plan: no stop, no claim, nothing saved wrong."""
    world = await pause_world(hass, timers, plan=open_window())
    assert _owner(world) == ("plan_window", True)
    await world.controller.async_drop_plan()
    await hass.async_block_till_done()
    assert world.controller.plan is None
    assert _owner(world) == ("plan_window", drives)
    for index in range(3):
        # Reports of the charge going on.
        hass.states.async_set(SWITCH, "on", {"report": index})
        await hass.async_block_till_done()
    assert world.stops == [] and hass.states.get(SWITCH).state == "on"
    stored = await _stored(world)
    assert (stored["charge_origin"], stored["plan_charge"]) == ("plan_window", drives)
    await world.shutdown()


async def test_an_owners_recording_replays_with_no_line_up(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool, tmp_path
) -> None:
    """Every change of the owner is fed: a recording of the plan's window start, the charger seen off, a person's Start,
    a start through the charger's entry and a person's Stop replays from its first session alone, with no difference
    beyond observation drift (the core's replay tool, `--no-line-up`, says the same)."""
    import json

    from custom_components.spotnav.core.replay import main, replay_bundle

    from .test_core_replay import _bundle

    world = await pause_world(hass, timers, plan=open_window())
    await world.switch("off")
    await world.executor.async_manual_start()
    await world.executor.async_manual_stop()
    await world.executor.async_resume()
    assert await world.controller.async_start(cause="solar")
    await world.executor.async_manual_stop()
    await hass.async_block_till_done()
    diagnostics = world.controller.session_runner.diagnostics()
    assert diagnostics["counts"]["failures"] == {}
    bundle = json.loads(json.dumps(_bundle(diagnostics["events"]), default=str))
    alone = replay_bundle(bundle, line_up=False)["charger_a"]
    assert alone.events == len(diagnostics["events"]) and alone.skipped == 0
    assert alone.real == (), alone.real
    assert replay_bundle(bundle)["charger_a"].mismatches == ()
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    assert main([str(path), "--no-line-up"]) == 0
    await world.shutdown()


async def test_a_feed_cancelled_in_another_task_leaves_a_start_on_its_way_its_owner(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """A start of ours on its way in one task, and a report in another whose control cannot be read (its feed is
    cancelled: nothing to decide): the owner still reads as the start makes it until the start's own feed is over."""
    world = await pause_world(hass, timers)
    controller = world.controller
    shadow = controller.session_runner
    sent = asyncio.Event()
    release = asyncio.Event()
    cancelled: list[int] = []
    real_cancel = shadow.cancel

    def counting_cancel(token: Any) -> None:
        cancelled.append(1)
        real_cancel(token)

    shadow.cancel = counting_cancel  # type: ignore[method-assign]

    async def slow_start(*_args: Any, **_kwargs: Any) -> bool:
        sent.set()
        await release.wait()
        current = hass.states.get(SWITCH)
        hass.states.async_set(SWITCH, "on", current.attributes if current else None)
        return True

    controller.adapter.async_start = slow_start  # type: ignore[method-assign]
    start = hass.async_create_task(controller.async_start())
    await sent.wait()
    assert controller.charge_origin == "other"
    # A report whose control cannot be read, landing while the start is on its way.
    hass.states.async_set(SWITCH, "unavailable", {"plug": True, "stamp": "unreadable"})
    for _ in range(5):
        await asyncio.sleep(0)
    assert cancelled, "the report's feed was cancelled"
    assert controller.charge_origin == "other", "still the start's owner while it is on its way"
    hass.states.async_set(SWITCH, "off", {"plug": True, "stamp": "readable"})
    release.set()
    assert await start
    await hass.async_block_till_done()
    assert controller.charge_origin == "other"
    if drives:
        assert shadow.session.owner == "charge_now"
    await world.shutdown()


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("owner", ["plan", "person", "charge_now"])
async def test_a_record_newer_than_the_keys_is_the_owner_at_a_restart_and_the_keys_follow(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """The core drives, and its record names another owner than today's keys beside it: a charge of SpotNav's in the
    record, nobody's in the keys (an earlier build saved the record after them, as its order marks say). The restart
    reads the owner from the record alone, and today's keys are written again to follow it, so a rollback reads the
    same owner."""
    from custom_components.spotnav.execution.controller import SESSION_ORDER_KEY

    world = await pause_world(hass, timers)
    await _start(world, owner)
    assert _owner(world) == ORIGINS[owner]
    real_start_again = World._start_again

    async def keys_behind(self: World) -> World:
        disk = await self.controller._store.async_load()  # noqa: SLF001
        assert disk[SESSION_STORE_KEY]["owner"] == owner
        await self.controller._store.async_save(  # noqa: SLF001
            {**disk, "charge_origin": None, "plan_charge": False, SESSION_ORDER_KEY: {"session": 7, "keys": 3}}
        )
        return await real_start_again(self)

    monkeypatch.setattr(World, "_start_again", keys_behind)
    restarted = await world.restart()
    await hass.async_block_till_done()
    assert restarted.controller.session_runner.session.owner == owner
    assert hass.states.get(SWITCH).state == "on", "the charge goes on"
    await _keys_follow(restarted, True, ORIGINS[owner])
    await restarted.shutdown()


class _Day:
    """The debug bundle's keeper of a charger's plug-ins (`debug_day.ChargerDay`), as far as the controller tells it."""

    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    def note_command(self, record: dict[str, Any]) -> None:
        self.commands.append(record)

    def note_ownership(self, record: dict[str, Any]) -> None:
        self.records.append(record)

    def note_phase_switch(self, *_args: Any, **_kwargs: Any) -> None:
        pass


async def test_the_debug_day_hears_every_owner_decision(hass: HomeAssistant, timers: FakeScheduler, drives: bool) -> None:
    """The debug bundle hears each event the core decided (`SessionRunner.on_record`) and each command sent, also
    when the core keeps the owner: a person's Start and the charger seen off are both in its records."""
    world = await pause_world(hass, timers)
    day = _Day()
    world.controller.set_debug_day(day)  # type: ignore[arg-type]
    before = len(world.controller.session_runner.events)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    await world.switch("off")
    await hass.async_block_till_done()
    kinds = [record.get("event", {}).get("kind") for record in day.records]
    assert kinds[0] == "person_start" and kinds[-1] == "charger_reported_off", kinds
    assert day.commands, "the start sent through the adapter is heard"
    assert len(day.records) == len(world.controller.session_runner.events) - before
    world.controller.set_debug_day(None)
    await world.shutdown()


async def test_a_plan_cleared_for_its_settings_while_its_start_is_on_its_way_is_fed_as_dropped(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """The settings changed and the answer for them is to wait while the plan's window start is on its way, not yet
    reported charging. The wait does not clear the plan then: its window is open, which counts as its charge running
    (`AutoExecutor._plan_charge_runs`), so the clear waits for the window's boundary. A clear that does go through
    (`_clear_superseded_locked`, here called directly while the start is on its way) is fed to the core as a dropped
    plan, as `async_drop_plan` feeds it: the charge goes on, its owner as after any dropped plan
    (`test_a_dropped_plan_keeps_its_charge_the_plans`), with no stop and no failure."""
    world = await pause_world(hass, timers)
    controller = world.controller
    shadow = controller.session_runner
    original = controller.adapter.async_start
    called = asyncio.Event()
    release = asyncio.Event()

    async def slow_start(*args: Any, **kwargs: Any) -> bool:
        called.set()
        await release.wait()
        return await original(*args, **kwargs)

    controller.adapter.async_start = slow_start  # type: ignore[method-assign]
    install = hass.async_create_task(install_schedule(controller, open_window()))
    await called.wait()
    assert not controller.charging
    assert world.executor._plan_charge_runs()  # noqa: SLF001 - what the wait's answer reads: it defers the clear
    clear = hass.async_create_task(world.executor._clear_superseded_locked())  # noqa: SLF001
    await asyncio.sleep(0)
    release.set()
    await install
    await clear
    await hass.async_block_till_done()

    assert controller.plan is None and controller.charging
    assert _owner(world) == ("plan_window", drives)
    kinds = [record["event"]["kind"] for record in shadow.events]
    assert "plan_dropped" in kinds, kinds
    if drives:
        assert shadow.session.owner == "plan" and not shadow.session.start_pending
    for index in range(3):
        hass.states.async_set(SWITCH, "on", {"report": index})
        await hass.async_block_till_done()
    assert world.stops == [] and hass.states.get(SWITCH).state == "on"
    assert shadow.counts["failures"] == {}
    stored = await _stored(world)
    assert (stored["charge_origin"], stored["plan_charge"]) == ("plan_window", drives)
    await world.shutdown()


def _balancing(world: World) -> tuple[str | None, bool, bool, str | None]:
    controller = world.controller
    return (
        controller.charge_origin,
        controller._is_plan_charge,  # noqa: SLF001
        controller.paused_by_balancing,
        controller.paused_charge_origin,
    )


async def test_a_persons_start_waiting_for_a_balancing_pauses_stop_owns_the_charge(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """The plan's charge runs; load balancing pauses it and, while that stop is on its way, the person presses Start
    (their feed begins and waits for the lock the stop holds). The stop goes out first, then the start: the charge
    that runs is the person's, and balancing holds nothing back, in both modes."""
    world = await pause_world(hass, timers)
    controller = world.controller
    await install_schedule(controller, open_window())
    await hass.async_block_till_done()
    assert _owner(world) == ("plan_window", True)
    real_stop = controller.adapter.async_stop
    started: list[asyncio.Task] = []

    async def stop_meeting_a_start(*args: Any, **kwargs: Any) -> Any:
        if not started:
            started.append(hass.async_create_task(world.executor.async_manual_start()))
            for _ in range(3):
                await asyncio.sleep(0)
        return await real_stop(*args, **kwargs)

    monkeypatch.setattr(controller.adapter, "async_stop", stop_meeting_a_start)
    await controller.async_apply_regulated_current(0, must_lower=True)
    await started[0]
    await hass.async_block_till_done()
    for index in range(2):
        hass.states.async_set(SWITCH, hass.states.get(SWITCH).state, {"report": index})
        await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == "on"
    assert _balancing(world) == ("manual", False, False, None)
    if drives:
        session = controller.session_runner.session
        assert (session.owner, session.balancing_paused, session.pending) == ("person", False, ())
    assert (await _stored(world))["charge_origin"] == "manual"
    await world.shutdown()


async def test_a_balancing_pause_landing_after_a_persons_start_went_out_holds_it_back(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """The person's Start went out (the controller's lock is free again) and load balancing pauses the charge while
    the boundary still stores the person's pause: the pause holds the person's charge back, and the regulator's resume
    gives it back as theirs, in both modes."""
    world = await pause_world(hass, timers)
    controller = world.controller
    real_update = world.store.async_update
    paused: list[Any] = []

    async def update_meeting_a_pause(*args: Any, **kwargs: Any) -> Any:
        if not paused:
            paused.append(await controller.async_apply_regulated_current(0, must_lower=True))
        return await real_update(*args, **kwargs)

    monkeypatch.setattr(world.store, "async_update", update_meeting_a_pause)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert paused and paused[0].outcome == "stopped"
    assert hass.states.get(SWITCH).state == "off"
    assert _balancing(world) == (None, False, True, "manual")
    if drives:
        session = controller.session_runner.session
        assert (session.owner, session.balancing_paused, session.paused_origin) == ("none", True, "person")

    assert await controller.async_battery_probe_start(10)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "on"
    assert _balancing(world) == ("manual", False, False, None)
    await world.shutdown()


@pytest.mark.core_failure_expected
@pytest.mark.parametrize("record", [True, False], ids=["record", "keys"])
async def test_a_restart_the_core_cannot_decide_keeps_the_owner(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, record: bool
) -> None:
    """The restart's own decision fails (a failure, told): the owner is not lost. The session is what was read back,
    the core's own record, or the one migrated from today's keys when there is none."""
    from custom_components.spotnav.core import events as ev
    from custom_components.spotnav.execution import session_runner

    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    assert _owner(world) == ("manual", False)
    if not record:
        stored = await _stored(world)
        stored.pop(SESSION_STORE_KEY, None)
        await world.controller._store.async_save(stored)  # noqa: SLF001
    real_decide = session_runner.decide

    def restart_fails(session: Any, event: Any, now: Any) -> Any:
        if isinstance(event, ev.Restart):
            raise RuntimeError("the restart cannot be decided")
        return real_decide(session, event, now)

    monkeypatch.setattr(session_runner, "decide", restart_fails)
    await world.controller.async_shutdown()
    await world.executor.async_shutdown()
    restarted = await world._start_again()  # noqa: SLF001 - the old controller is down already
    await hass.async_block_till_done()
    runner = restarted.controller.session_runner
    assert runner.counts["failures"].get("decide/restart") == 1
    assert runner.session.owner == "person"
    assert _owner(restarted) == ("manual", False)
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_a_record_newer_than_the_keys_keeps_the_owner_over_two_restarts(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person's charge whose keys on disk say nobody's and are older than the core's record: the first restart reads
    the person from the record and writes the keys again, so the second restart, in either kind, still reads them."""
    from custom_components.spotnav.execution.controller import SESSION_ORDER_KEY

    world = await pause_world(hass, timers)
    await world.executor.async_manual_start()
    await hass.async_block_till_done()
    real_start_again = World._start_again

    async def keys_behind(self: World) -> World:
        disk = await self.controller._store.async_load()  # noqa: SLF001
        await self.controller._store.async_save(  # noqa: SLF001
            {**disk, "charge_origin": None, "plan_charge": False, SESSION_ORDER_KEY: {"session": 7, "keys": 3}}
        )
        return await real_start_again(self)

    monkeypatch.setattr(World, "_start_again", keys_behind)
    restarted = await world.restart()
    await hass.async_block_till_done()
    monkeypatch.setattr(World, "_start_again", real_start_again)
    assert restarted.controller.session_runner.session.owner == "person"
    stored = await _stored(restarted)
    assert (stored["charge_origin"], stored["plan_charge"]) == ("manual", False)
    again = await restarted.restart()
    await hass.async_block_till_done()
    assert again.controller.session_runner.session.owner == "person"
    assert again.controller.charge_origin == "manual"
    await again.shutdown()


async def test_a_claim_inside_a_window_tells_readers_the_plans(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """The charger begins by itself inside a plan's open window: the charge is claimed as the plan's with no command,
    and readers are told so, with the keys saved, in both modes."""
    world = await pause_world(hass, timers)
    await install_schedule(world.controller, open_window())
    await hass.async_block_till_done()
    await world.switch("off")
    starts = len(world.starts)
    told: list[tuple[str | None, bool]] = []
    world.controller.add_listener(lambda: told.append(_owner(world)))
    hass.states.async_set(SWITCH, "on", {"by": "itself"})
    await hass.async_block_till_done()
    assert told and told[-1] == ("plan_window", True), told
    assert len(world.starts) == starts and world.stops == []
    assert _owner(world) == ("plan_window", True)
    stored = await _stored(world)
    assert (stored["charge_origin"], stored["plan_charge"]) == ("plan_window", True)
    await world.shutdown()
