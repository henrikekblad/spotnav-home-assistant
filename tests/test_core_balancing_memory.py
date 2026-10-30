"""Load balancing's memory in the core's session (the core end state plan, step 4): while the core drives, whether
load balancing holds a charge back and whose it was are the session's (`balancing_paused`, `paused_origin`), read by
the controller from it; today's `balancing_pause` key is still written, derived from the session, so an older build
reads it as before. With the option off, today's fields stay the truth. Every test here runs in both modes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.spotnav.core.session import ChargeSession
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import SESSION_ORDER_KEY, SESSION_STORE_KEY
from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED

from .pause_world import pause_world, World
from .relay import FakeScheduler

PERSONS = {"origin": "manual", "plan_charge": False}


@pytest.fixture(params=[True, False], ids=["core", "previous"])
def drives(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", request.param)
    return request.param


async def _stored(world: World) -> dict[str, Any]:
    await world.hass.async_block_till_done()
    return await world.controller._store.async_load()  # noqa: SLF001


async def _persons_charge_held_back(world: World) -> None:
    """A person's Start, then load balancing's pause of it (no room for the minimum current)."""
    await world.executor.async_manual_start(10)
    await world.controller._regulated_stop("pause")  # noqa: SLF001 - the regulator's own pause
    await world.hass.async_block_till_done()
    assert world.controller.paused_by_balancing and world.controller.paused_charge_origin == "manual"


def _session(world: World) -> ChargeSession:
    return world.controller.ownership_shadow.session


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_while_balancing_holds_a_persons_charge_gives_it_back(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    world = await pause_world(hass, timers)
    await _persons_charge_held_back(world)
    stored = await _stored(world)
    assert stored["balancing_pause"] == PERSONS, "today's key, written in both modes"
    if drives:
        assert (stored[SESSION_STORE_KEY]["balancing_paused"], stored[SESSION_STORE_KEY]["paused_origin"]) == (
            True,
            "person",
        )

    restarted = await world.restart()
    controller = restarted.controller
    assert controller.paused_by_balancing and controller.paused_charge_origin == "manual"
    assert controller.balancing_holds("manual")
    if drives:
        assert (_session(restarted).balancing_paused, _session(restarted).paused_origin) == (True, "person")

    # The regulator's resume gives the charge back as the person's, and nothing is held back any more.
    assert await controller.async_battery_probe_start(8)
    await hass.async_block_till_done()
    assert controller.charge_origin == "manual"
    assert not controller.paused_by_balancing and controller.paused_charge_origin is None
    assert (await _stored(restarted))["balancing_pause"] is None
    if drives:
        assert not _session(restarted).balancing_paused and _session(restarted).paused_origin is None
    await restarted.shutdown()


@pytest.mark.usefixtures("both_restarts")
@pytest.mark.parametrize("record", ["missing", "older_than_the_keys"])
async def test_todays_balancing_pause_key_is_read_where_the_keys_are_the_truth(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, record: str
) -> None:
    """When the core drives, a restart takes load balancing's memory from today's `balancing_pause` key where today's
    keys are the truth: an older build's record with no session of the core's (migrated once), or keys written
    after the session's last change."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", True)
    world = await pause_world(hass, timers)
    await _persons_charge_held_back(world)
    world.controller._cancel_session_save()  # noqa: SLF001 - the record is changed by hand below
    saved = dict(await _stored(world))
    if record == "missing":
        saved.pop(SESSION_STORE_KEY, None)
        saved.pop(SESSION_ORDER_KEY, None)
    else:
        saved[SESSION_STORE_KEY] = {**saved[SESSION_STORE_KEY], "balancing_paused": False, "paused_origin": None}
        saved[SESSION_ORDER_KEY] = {"session": 3, "keys": 4}
    await world.controller._store.async_save(saved)  # noqa: SLF001
    world.controller._session_saved = ChargeSession.from_store(  # noqa: SLF001 - the old controller saves no more
        saved.get(SESSION_STORE_KEY) or ChargeSession().to_store()
    )
    world.controller.ownership_shadow.session = world.controller._session_saved  # noqa: SLF001

    restarted = await world.restart()
    assert (_session(restarted).balancing_paused, _session(restarted).paused_origin) == (True, "person")
    assert restarted.controller.paused_by_balancing and restarted.controller.paused_charge_origin == "manual"
    await restarted.shutdown()


async def test_the_regulator_dropping_a_charge_no_longer_wanted_is_the_cores_decision(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """The site's `paused_charge_origin` path: under a span pause only a person's charge is still wanted. The plan's
    one is dropped (`BalancingDropped`), which the core decides; a person's is kept."""
    from custom_components.spotnav.site import site_capacity_controller as scc

    world = await pause_world(hass, timers)
    await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
    controller = world.controller
    monkeypatch.setattr(scc, "domain_data", lambda _hass: SimpleNamespace(auto_store=world.store))
    site = SimpleNamespace(hass=hass)

    # The plan's charge, held back by load balancing below the floor (no room on the site now).
    controller.set_start_cap(lambda: 0.0)
    assert await controller.async_start(16, cause="plan_window") is False
    assert controller.paused_by_balancing and controller.paused_charge_origin == "plan_window"
    if drives:
        assert _session(world).paused_origin == "plan"

    assert not scc.SiteCapacityController._charge_still_wanted(site, "entry_a", controller)  # noqa: SLF001
    assert not controller.paused_by_balancing and controller.paused_charge_origin is None
    assert not _session(world).balancing_paused and _session(world).paused_origin is None
    kinds = [record["event"]["kind"] for record in controller.ownership_shadow.diagnostics()["events"]]
    assert kinds[-1] == "balancing_dropped"
    assert (await _stored(world))["balancing_pause"] is None

    # A person's charge held back the same way is still wanted under their span pause (C5).
    assert await controller.async_start(16, manual=True) is False
    assert controller.paused_charge_origin == "manual"
    assert scc.SiteCapacityController._charge_still_wanted(site, "entry_a", controller)  # noqa: SLF001
    assert controller.paused_by_balancing
    await world.shutdown()


async def test_a_start_held_back_below_the_floor_is_told_by_its_own_outcome(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """A person's Start load balancing holds back is still their charge, waiting for the regulator, in both modes:
    the boundary tells it by the start's own outcome (`start_held_by_balancing`), not by the memory the core keeps."""
    world = await pause_world(hass, timers)
    controller = world.controller
    controller.set_start_cap(lambda: 0.0)
    with pytest.raises(HomeAssistantError, match="held back by load balancing"):
        await world.executor.async_manual_start(10)
    assert controller.start_held_by_balancing
    assert controller.paused_by_balancing and controller.paused_charge_origin == "manual"
    assert world.pause.manual and world.pause.action == "start"
    if drives:
        assert (_session(world).balancing_paused, _session(world).paused_origin) == (True, "person")
    controller.set_start_cap(None)
    await world.executor.async_manual_start(10)
    assert not controller.start_held_by_balancing and not controller.paused_by_balancing
    await world.shutdown()


@pytest.mark.shadow_disagreement_expected
async def test_load_balancings_memory_is_kept_as_the_core_decided_it_and_a_difference_is_mirror_drift(
    hass: HomeAssistant,
) -> None:
    """Owned by the core (`BALANCING`): the line-up never takes it from today's state; a difference is counted and
    told as `mirror_drift`, and the core's value stays."""
    from custom_components.spotnav.core import events as ev

    today = {"session": ChargeSession(plugged=True)}
    heard: list[str] = []
    ownership_shadow.LISTENERS.append(lambda kind, record: heard.append(kind))
    try:
        shadow = ownership_shadow.OwnershipShadow(
            lambda: today["session"], owned_fields=(*ownership_shadow.SAFETY_AND_C7, *ownership_shadow.BALANCING)
        )
        shadow.session = today["session"].with_changes(balancing_paused=True, paused_origin="person")
        today["session"] = today["session"].with_changes(balancing_paused=True, paused_origin="person")
        shadow.feed(ev.ConnectionUnknown())
        assert heard == [] and shadow.counts["mirror_drift"] == 0
        today["session"] = today["session"].with_changes(balancing_paused=False, paused_origin=None)
        shadow.feed(ev.ConnectionUnknown())
    finally:
        ownership_shadow.LISTENERS.pop()
    assert heard == ["mirror_drift"] and shadow.counts["mirror_drift"] == 1
    assert (shadow.session.balancing_paused, shadow.session.paused_origin) == (True, "person"), "never taken"
    (record,) = [item for item in shadow.diagnostics()["drift"] if item.get("mirror")]
    assert set(record["fields"]) == {"balancing_paused", "paused_origin"}


async def _replugged(world: World, control: str) -> None:
    """The car is unplugged and plugged in again while a command is on its way (no wait for the loop to settle: the
    command's task holds the operation lock)."""
    import asyncio

    from .pause_world import SWITCH

    for connected, stamp in ((False, "unplug"), (True, "replug")):
        world.plug.connected = connected
        world.hass.states.async_set(SWITCH, control, {"plug": connected, "stamp": stamp})
        for _ in range(5):
            await asyncio.sleep(0)


def _nothing_held_back(world: World, drives: bool) -> None:
    controller = world.controller
    assert not controller._balancing.paused and controller.paused_charge_origin is None  # noqa: SLF001
    if drives:
        assert not _session(world).balancing_paused and _session(world).paused_origin is None


@pytest.mark.parametrize("what", ["pause", "held_start", "failed_resume"])
async def test_a_command_across_an_unplug_and_a_new_plug_in_holds_nothing_back(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool, what: str
) -> None:
    """Load balancing's pause, a start it holds back below the floor, or its resume that did not take, with the car
    unplugged and plugged in again before the command came back: the charge held back would be the earlier plug-in's
    (a person's Start, say), never one to give back on the new plug-in."""
    world = await pause_world(hass, timers)
    controller = world.controller
    await world.executor.async_manual_start(10)
    if what == "pause":
        real_stop = controller.adapter.async_stop

        async def stop_across_a_replug() -> bool:
            await _replugged(world, "on")
            return await real_stop()

        controller.adapter.async_stop = stop_across_a_replug  # type: ignore[method-assign]
        await controller._regulated_stop("pause")  # noqa: SLF001
    elif what == "held_start":
        await world.executor.async_manual_stop()
        real_save = controller._async_save  # noqa: SLF001

        async def save_across_a_replug() -> None:
            await _replugged(world, "off")
            await real_save()

        controller.set_start_cap(lambda: 0.0)
        controller._async_save = save_across_a_replug  # type: ignore[method-assign]  # noqa: SLF001
        try:
            assert await controller.async_start(16, cause="solar") is False
        finally:
            controller._async_save = real_save  # type: ignore[method-assign]  # noqa: SLF001
    else:
        await controller._regulated_stop("pause")  # noqa: SLF001
        assert controller.paused_by_balancing and controller.paused_charge_origin == "manual"

        async def start_across_a_replug(_amps: int | None = None) -> bool:
            await _replugged(world, "off")
            return False

        controller.adapter.async_start = start_across_a_replug  # type: ignore[method-assign]
        assert not await controller.async_battery_probe_start(8)
    await hass.async_block_till_done()
    assert world.plug.connected is True
    _nothing_held_back(world, drives)
    assert (await _stored(world))["balancing_pause"] is None
    await world.shutdown()


async def _plans_charge_held_back(world: World) -> None:
    """The plan's charge in its open window, then load balancing's pause of it (no room for the minimum current)."""
    from .pause_world import SWITCH

    controller = world.controller
    assert controller.charge_origin == "plan_window" and world.hass.states.get(SWITCH).state == "on"
    await controller._regulated_stop("pause")  # noqa: SLF001 - the regulator's own pause
    await world.hass.async_block_till_done()
    assert controller.paused_by_balancing and controller.paused_charge_origin == "plan_window"


@pytest.mark.usefixtures("both_restarts")
async def test_a_restart_in_an_open_window_leaves_the_plans_charge_load_balancing_holds_back(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """The restart's re-arm finds the plan's window open and the plan's charge held back by load balancing: it starts
    nothing and keeps the memory, so the regulator gives the charge back by its own rules, as without a restart."""
    from .pause_world import open_window, SWITCH

    world = await pause_world(hass, timers, plan=open_window())
    await _plans_charge_held_back(world)
    starts = len(world.starts)

    restarted = await world.restart()
    controller = restarted.controller
    assert len(restarted.starts) == starts and hass.states.get(SWITCH).state == "off"
    assert controller.paused_by_balancing and controller.paused_charge_origin == "plan_window"
    assert (await _stored(restarted))["balancing_pause"] == {"origin": "plan_window", "plan_charge": True}
    if drives:
        assert (_session(restarted).balancing_paused, _session(restarted).paused_origin) == (True, "plan")

    # Room again: the regulator's resume gives the charge back as the plan's.
    assert await controller.async_battery_probe_start(8)
    await hass.async_block_till_done()
    assert len(restarted.starts) == starts + 1 and hass.states.get(SWITCH).state == "on"
    assert controller.charge_origin == "plan_window" and not controller.paused_by_balancing
    assert (await _stored(restarted))["balancing_pause"] is None
    if drives:
        assert not _session(restarted).balancing_paused and _session(restarted).paused_origin is None
    await restarted.shutdown()
