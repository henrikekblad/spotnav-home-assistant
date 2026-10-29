"""The hold's memory in the core's session (the core end state plan, step 5): while the core drives, whether this
plug-in session's charge that began by itself outside a window was held, and whether a person overrode the hold, are
the session's (`held`, `overridden`), read by the controller from it, and whether the charge is SpotNav's is the
session's owner (`WindowHold.owned`); today's `hold` key is still written, derived from the session, so an older
build reads it as before. With the option off, today's fields stay the truth. Every test here runs in both modes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.core.session import ChargeSession
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import SESSION_ORDER_KEY, SESSION_STORE_KEY

from .helpers import install_schedule
from .pause_world import later_window, pause_world, SWITCH, World
from .relay import FakeScheduler

HELD = {"held": True, "overridden": False}
OVERRIDDEN = {"held": True, "overridden": True}
NONE = {"held": False, "overridden": False}


@pytest.fixture(params=[True, False], ids=["core", "previous"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


async def _stored(world: World) -> dict[str, Any]:
    await world.hass.async_block_till_done()
    return await world.controller._store.async_load()  # noqa: SLF001


def _session(world: World) -> ChargeSession:
    return world.controller.ownership_shadow.session


def _hold(world: World) -> dict[str, bool]:
    state = world.controller._hold_state  # noqa: SLF001 - the hold's memory, through its accessor
    return {"held": state.held, "overridden": state.overridden}


def _agrees(world: World, drives: bool, expected: dict[str, bool]) -> None:
    assert _hold(world) == expected
    if drives:
        session = _session(world)
        assert {"held": session.held, "overridden": session.overridden} == expected


async def _charger_begins(world: World) -> None:
    """The charger begins a charge by itself (the car plugged in, the switch turned on by nobody here)."""
    await world.plug.set(True, control="on")


async def _held(world: World, drives: bool) -> None:
    """The charger began a charge by itself between the plan's windows: the hold stopped it once."""
    await _charger_begins(world)
    assert len(world.stops) == 1 and world.hass.states.get(SWITCH).state == "off"
    assert world.controller.hold_until is not None and not world.controller.hold_overridden
    _agrees(world, drives, HELD)


async def _overridden(world: World, drives: bool) -> None:
    """After the hold, the charge began again with nobody here starting it: a person's override, which goes on."""
    await _held(world, drives)
    await _charger_begins(world)
    assert len(world.stops) == 1, "a person's override is not stopped"
    assert world.controller.hold_overridden and world.controller.hold_until is None
    _agrees(world, drives, OVERRIDDEN)


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_in_a_held_plug_in_session_keeps_the_hold(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    world = await pause_world(hass, timers, plan=later_window())
    await _held(world, drives)
    stored = await _stored(world)
    assert stored["hold"] == HELD, "today's key, written in both modes"
    if drives:
        assert (stored[SESSION_STORE_KEY]["held"], stored[SESSION_STORE_KEY]["overridden"]) == (True, False)

    restarted = await world.restart()
    controller = restarted.controller
    assert controller.hold_until is not None and not controller.hold_overridden
    _agrees(restarted, drives, HELD)

    # The charger begins again in the same plug-in: the hold was used, so this is a person's override.
    await _charger_begins(restarted)
    assert len(restarted.stops) == 1
    assert controller.hold_overridden
    _agrees(restarted, drives, OVERRIDDEN)
    assert (await _stored(restarted))["hold"] == OVERRIDDEN
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_spares_a_persons_override_of_the_hold(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """The re-arm at the restart is outside the windows with a charge running, and stops it unless it is spared: an
    overridden hold (kept across the restart) is the person's, as after the hold."""
    world = await pause_world(hass, timers, plan=later_window())
    await _overridden(world, drives)

    restarted = await world.restart()
    await hass.async_block_till_done()
    assert len(restarted.stops) == 1 and hass.states.get(SWITCH).state == "on"
    assert restarted.controller.hold_overridden
    _agrees(restarted, drives, OVERRIDDEN)
    await restarted.shutdown()


async def test_the_rearm_spares_an_overridden_hold(hass: HomeAssistant, timers: FakeScheduler, drives: bool) -> None:
    world = await pause_world(hass, timers, plan=later_window())
    await _overridden(world, drives)

    await world.controller.async_follow_schedule()
    await hass.async_block_till_done()
    assert len(world.stops) == 1 and hass.states.get(SWITCH).state == "on"
    _agrees(world, drives, OVERRIDDEN)
    await world.shutdown()


async def test_the_rearms_stop_of_a_running_charge_holds_the_plug_in(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """A plan installed while the charger runs a charge it began by itself (no window ahead yet, so nothing held it)
    stops it at the re-arm: the rest of the plug-in session is held, so the next start by itself is a person's."""
    world = await pause_world(hass, timers)
    await _charger_begins(world)
    assert world.stops == []
    _agrees(world, drives, NONE)

    await install_schedule(world.controller, later_window())
    await hass.async_block_till_done()
    assert len(world.stops) == 1
    _agrees(world, drives, HELD)
    assert (await _stored(world))["hold"] == HELD

    await _charger_begins(world)
    assert len(world.stops) == 1
    _agrees(world, drives, OVERRIDDEN)
    await world.shutdown()


async def test_the_rearms_stop_across_an_unplug_and_a_new_plug_in_holds_nothing(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """The re-arm's stop with the car unplugged and plugged in again before it came back: the hold would be the
    earlier plug-in's, never the new one's, so a charge the charger then begins by itself is held, not a person's."""
    world = await pause_world(hass, timers)
    await _charger_begins(world)
    controller = world.controller
    real_stop = controller.adapter.async_stop

    async def stop_across_a_replug() -> bool:
        for connected, stamp in ((False, "unplug"), (True, "replug")):
            world.plug.connected = connected
            hass.states.async_set(SWITCH, "on", {"plug": connected, "stamp": stamp})
            for _ in range(5):
                await asyncio.sleep(0)
        return await real_stop()

    controller.adapter.async_stop = stop_across_a_replug  # type: ignore[method-assign]
    try:
        await install_schedule(controller, later_window())
        await hass.async_block_till_done()
    finally:
        controller.adapter.async_stop = real_stop  # type: ignore[method-assign]
    assert len(world.stops) == 1 and world.plug.connected is True
    _agrees(world, drives, NONE)
    assert (await _stored(world))["hold"] == NONE

    await _charger_begins(world)
    assert len(world.stops) == 2, "the new plug-in's own charge that began by itself is held"
    _agrees(world, drives, HELD)
    await world.shutdown()


async def test_the_next_window_ends_the_hold(hass: HomeAssistant, timers: FakeScheduler, drives: bool) -> None:
    world = await pause_world(hass, timers, plan=later_window())
    await _overridden(world, drives)

    await world.controller._async_start_window()  # noqa: SLF001 - the timer's own entry, the window's time aside
    await hass.async_block_till_done()
    _agrees(world, drives, NONE)
    await world.shutdown()


async def test_an_unplug_ends_the_hold(hass: HomeAssistant, timers: FakeScheduler, drives: bool) -> None:
    world = await pause_world(hass, timers, plan=later_window())
    await _overridden(world, drives)

    await world.plug.set(False, control="off")
    _agrees(world, drives, NONE)
    assert world.controller.hold_until is None
    assert (await _stored(world))["hold"] == NONE

    # A new plug-in: the charger beginning by itself is held once again.
    await world.plug.set(True)
    await _charger_begins(world)
    assert len(world.stops) == 2
    _agrees(world, drives, HELD)
    await world.shutdown()


async def test_a_start_of_ours_reported_before_it_returns_is_not_held(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """A Charge-now start between the windows (a webhook's, decided when it returns) that the charger reports on
    before the command returns is SpotNav's charge (`WindowHold.owned`): the hold leaves it alone."""
    world = await pause_world(hass, timers, plan=later_window())
    controller = world.controller
    real_start = controller.adapter.async_start

    async def reported_at_once(*args: Any, **kwargs: Any) -> bool:
        hass.states.async_set(SWITCH, "on", {"plug": True, "stamp": "on_first"})
        for _ in range(5):
            await asyncio.sleep(0)
        return await real_start(*args, **kwargs)

    controller.adapter.async_start = reported_at_once  # type: ignore[method-assign]
    try:
        assert await controller.async_start(10)
        await hass.async_block_till_done()
    finally:
        controller.adapter.async_start = real_start  # type: ignore[method-assign]
    assert world.stops == [] and hass.states.get(SWITCH).state == "on"
    assert controller.charge_origin == "other"
    _agrees(world, drives, NONE)
    if drives:
        assert controller._hold_state.owned and _session(world).owner == "charge_now"  # noqa: SLF001
    await world.shutdown()


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("record", ["missing", "older_than_the_keys"])
async def test_todays_hold_key_is_read_where_the_keys_are_the_truth(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, record: str
) -> None:
    """When the core drives, a restart takes the hold's memory from today's `hold` key where today's keys are the
    truth: an older build's record with no session of the core's (migrated once), or keys written after the session's
    last change."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", True)
    world = await pause_world(hass, timers, plan=later_window())
    await _overridden(world, True)
    world.controller._cancel_session_save()  # noqa: SLF001 - the record is changed by hand below
    saved = dict(await _stored(world))
    if record == "missing":
        saved.pop(SESSION_STORE_KEY, None)
        saved.pop(SESSION_ORDER_KEY, None)
    else:
        saved[SESSION_STORE_KEY] = {**saved[SESSION_STORE_KEY], "held": False, "overridden": False}
        saved[SESSION_ORDER_KEY] = {"session": 3, "keys": 4}
    await world.controller._store.async_save(saved)  # noqa: SLF001
    world.controller._session_saved = ChargeSession.from_store(  # noqa: SLF001 - the old controller saves no more
        saved.get(SESSION_STORE_KEY) or ChargeSession().to_store()
    )
    world.controller.ownership_shadow.session = world.controller._session_saved  # noqa: SLF001

    restarted = await world.restart()
    await hass.async_block_till_done()
    assert (_session(restarted).held, _session(restarted).overridden) == (True, True)
    assert restarted.controller.hold_overridden and len(restarted.stops) == 1
    await restarted.shutdown()


@pytest.mark.shadow_disagreement_expected
async def test_the_holds_memory_is_kept_as_the_core_decided_it_and_a_difference_is_mirror_drift(
    hass: HomeAssistant,
) -> None:
    """Owned by the core (`WINDOW_HOLD`): the line-up never takes it from today's state; a difference is counted and
    told as `mirror_drift`, and the core's value stays."""
    from custom_components.spotnav.core import events as ev

    today = {"session": ChargeSession(plugged=True)}
    heard: list[str] = []
    ownership_shadow.LISTENERS.append(lambda kind, record: heard.append(kind))
    try:
        shadow = ownership_shadow.OwnershipShadow(lambda: today["session"], owned_fields=ownership_shadow.WINDOW_HOLD)
        shadow.session = today["session"].with_changes(held=True, overridden=True)
        today["session"] = today["session"].with_changes(held=True, overridden=True)
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.counts["mirror_drift"] == 0
        today["session"] = today["session"].with_changes(held=False, overridden=False)
        shadow.feed(ev.ConnectionUnknown())
    finally:
        ownership_shadow.LISTENERS.pop()
    assert heard == ["mirror_drift"] and shadow.counts["mirror_drift"] == 1
    assert (shadow.session.held, shadow.session.overridden) == (True, True), "never taken"
    (record,) = [item for item in shadow.diagnostics()["drift"] if item.get("mirror")]
    assert set(record["fields"]) == {"held", "overridden"}


async def test_a_held_plug_in_sessions_recording_replays_with_no_line_up(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """Every change of the hold's memory is fed: a recording of the hold, the override, the re-arm sparing it and the
    unplug replays from its first session alone, with no difference beyond observation drift."""
    import json

    from custom_components.spotnav.core.replay import replay_bundle

    from .test_core_replay import _bundle

    world = await pause_world(hass, timers, plan=later_window())
    await _overridden(world, drives)
    await world.controller.async_follow_schedule()
    await world.plug.set(False, control="off")
    await hass.async_block_till_done()
    diagnostics = world.controller.ownership_shadow.diagnostics()
    bundle = json.loads(json.dumps(_bundle(diagnostics["events"]), default=str))
    alone = replay_bundle(bundle, line_up=False)["charger_a"]
    assert alone.events == len(diagnostics["events"]) and alone.skipped == 0
    assert alone.real == (), alone.real
    assert replay_bundle(bundle)["charger_a"].mismatches == ()
    await world.shutdown()
