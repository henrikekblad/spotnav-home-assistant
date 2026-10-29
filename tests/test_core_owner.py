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

from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import _CORE_OWNER_ORIGIN, SESSION_STORE_KEY
from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.runtime import executor_for

from .helpers import install_schedule
from .pause_world import ENTRY, open_window, pause_world, SWITCH, World
from .relay import FakeScheduler
from .world import solar_setup

pytestmark = pytest.mark.usefixtures("offline_relay")


@pytest.fixture(params=[True, False], ids=["core", "previous"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
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
        assert controller.ownership_shadow.session.owner == "charge_now"


@pytest.mark.shadow_disagreement_expected
async def test_the_owner_is_kept_as_the_core_decided_it_and_a_difference_is_mirror_drift(hass: HomeAssistant) -> None:
    """Owned by the core (`OWNER`): the line-up never takes it from today's state; a difference is counted and told as
    `mirror_drift` (not as drift), and the core's owner stays. Nobody's and the charger's own charge are one owner to
    it: which of them it is is seen now, and taken from today's state with no mirror drift."""
    from custom_components.spotnav.core import events as ev
    from custom_components.spotnav.core.session import ChargeSession

    today = {"session": ChargeSession(plugged=True, owner="person")}
    heard: list[str] = []
    ownership_shadow.LISTENERS.append(lambda kind, record: heard.append(kind))
    try:
        shadow = ownership_shadow.OwnershipShadow(lambda: today["session"], owned_fields=ownership_shadow.OWNER)
        shadow.session = today["session"]
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.counts["mirror_drift"] == 0
        today["session"] = today["session"].with_changes(owner="solar")
        shadow.feed(ev.ConnectionUnknown())
        assert heard.count("mirror_drift") == 1 and "drift" not in heard
        assert shadow.session.owner == "person", "never taken"
        shadow.session = shadow.session.with_changes(owner="none")
        today["session"] = today["session"].with_changes(owner="charger_self")
        heard.clear()
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.session.owner == "charger_self", "seen, not kept"
        today["session"] = today["session"].with_changes(owner="none")
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.session.owner == "none"
    finally:
        ownership_shadow.LISTENERS.pop()
    (record,) = [item for item in shadow.diagnostics()["drift"] if item.get("mirror")]
    assert record["fields"] == {"owner": {"core": "person", "today": "solar"}}


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
        session = world.controller.ownership_shadow.session
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
        assert restarted.controller.ownership_shadow.session.owner == "person"
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
    shadow = controller.ownership_shadow
    assert shadow.counts["mirror_drift"] == 0 and shadow.counts["disagreements"] == 0
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
