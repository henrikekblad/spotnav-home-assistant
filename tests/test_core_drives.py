"""The charge-ownership core drives (`CONF_CORE_OWNERSHIP`, on unless a charger's entry data says false): it decides,
today's code acts on its verdicts and takes its owner back, and is still computed beside it and compared. A charger
set to the previous logic decides with today's code as ever, the core only in shadow. (`SPOTNAV_CORE_OWNERSHIP=0`
runs the whole suite with the previous logic.)"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CORE_OWNERSHIP
from custom_components.spotnav.core.ownership import Start
from custom_components.spotnav.core.session import ChargeSession
from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.auto_execution import AutoExecutor
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.planning.auto_settings import async_setup_auto_settings

from .helpers import install_schedule
from .pause_world import open_window, SWITCH
from .relay import FakeScheduler
from .test_replug import _obedient_switch, Plug


async def _charger(hass: HomeAssistant, *, drives: bool | None) -> tuple[ChargingController, AutoExecutor, list]:
    hass.states.async_set(SWITCH, "off")
    starts, _stops = _obedient_switch(hass)
    store = await async_setup_auto_settings(hass)
    config = {CONF_CHARGE_CONTROL: SWITCH}
    if drives is not None:
        config[CONF_CORE_OWNERSHIP] = drives
    controller = ChargingController(hass, "entry_a", config)
    executor = AutoExecutor(hass, controller, store)
    await executor.async_start()
    await controller.async_initialize()
    await executor.async_after_restore()
    plug = Plug(hass, controller, SWITCH)
    await plug.set(False)
    await plug.set(True)
    await hass.async_block_till_done()
    return controller, executor, starts


@pytest.mark.parametrize(("default", "configured", "drives"), [
    (True, None, True),
    (True, False, False),
    (True, True, True),
    (False, None, False),
    (False, True, True),
])
async def test_the_core_drives_unless_a_charger_says_otherwise(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, default: bool, configured: bool | None, drives: bool
) -> None:
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", default)
    controller, executor, _starts = await _charger(hass, drives=configured)
    assert controller.ownership_shadow.drives is drives
    assert controller.ownership_shadow.diagnostics()["drives"] is drives
    await executor.async_shutdown()
    await controller.async_shutdown()


def test_the_default_is_that_the_core_drives() -> None:
    import os

    if os.environ.get("SPOTNAV_CORE_OWNERSHIP") == "0":
        pytest.skip("this run sets the previous logic as the default")
    assert ownership_shadow.CORE_OWNERSHIP_DEFAULT is True


@pytest.mark.parametrize("drives", [True, False])
async def test_the_previous_logic_is_still_computed_and_compared_while_the_core_drives(
    hass: HomeAssistant, timers: FakeScheduler, drives: bool
) -> None:
    """Either way the other side runs beside the one that decides: a plan window's start is compared, and the
    diagnostics say which one drives."""
    controller, executor, starts = await _charger(hass, drives=drives)
    before = len(starts)
    compared = controller.ownership_shadow.counts["compared"]
    await install_schedule(controller, open_window())
    await hass.async_block_till_done()
    assert len(starts) > before and controller.charging
    shadow = controller.ownership_shadow.diagnostics()
    assert shadow["drives"] is drives
    assert shadow["counts"]["compared"] > compared
    assert shadow["counts"]["disagreements"] == 0
    assert shadow["events"], "the events the comparison saw are kept for the diagnostics"
    await executor.async_shutdown()
    await controller.async_shutdown()


@pytest.mark.shadow_disagreement_expected
@pytest.mark.parametrize(("drives", "started"), [(True, False), (False, True)])
async def test_todays_code_acts_on_the_cores_verdict_when_it_drives(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, drives: bool, started: bool
) -> None:
    """A core that would not start a window: driving, no start goes out; shadowing, today's code starts it (and the
    shadow reports the disagreement)."""
    real = ownership_shadow.decide

    def no_window_starts(session, event, now):
        session, commands = real(session, event, now)
        if event.kind == "window_start":
            commands = tuple(command for command in commands if not isinstance(command, Start))
        return session, commands

    monkeypatch.setattr(ownership_shadow, "decide", no_window_starts)
    controller, executor, starts = await _charger(hass, drives=drives)
    before = len(starts)
    await install_schedule(controller, open_window())
    await hass.async_block_till_done()
    assert (len(starts) > before) is started
    assert controller.charging is started
    assert (controller.ownership_shadow.counts["disagreements"] > 0) is (not drives)
    await executor.async_shutdown()
    await controller.async_shutdown()


async def test_the_cores_owner_is_taken_back_by_todays_two_fields_one_way(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    """A charge the core says nobody here owns clears today's `charge_origin` and `plan_charge`; an owner the core
    names is never written back over them (today's code sets them where it starts or claims a charge, and forgets
    `plan_charge` by itself when it sees the charger off)."""
    controller, executor, _starts = await _charger(hass, drives=True)
    controller._charge_origin, controller._plan_charge = "plan_window", False  # noqa: SLF001 - today's own forget
    assert not controller._take_core_owner(ChargeSession(owner="plan"))  # noqa: SLF001
    assert (controller.charge_origin, controller._plan_charge) == ("plan_window", False)  # noqa: SLF001
    assert not controller._take_core_owner(ChargeSession(owner="top_off"))  # noqa: SLF001
    assert controller._take_core_owner(ChargeSession(owner="charger_self"))  # noqa: SLF001
    assert (controller.charge_origin, controller._plan_charge) == (None, False)  # noqa: SLF001
    assert not controller._take_core_owner(ChargeSession(owner="none"))  # noqa: SLF001
    assert not controller._take_core_owner(ChargeSession(owner="person"))  # noqa: SLF001
    assert controller.charge_origin is None
    await executor.async_shutdown()
    await controller.async_shutdown()


async def test_a_persons_stop_and_start_follow_the_core_when_it_drives(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    controller, executor, _starts = await _charger(hass, drives=True)
    await executor.async_manual_stop()
    pause = executor.pause_intent
    assert (pause.choice, pause.action, pause.scope) == ("manual", "stop", "plug_in")
    assert controller.ownership_shadow.session.manual is not None
    await executor.async_manual_start()
    pause = executor.pause_intent
    assert (pause.action, pause.scope) == ("start", "plug_in")
    assert controller.charge_origin == "manual"
    assert controller.ownership_shadow.counts["disagreements"] == 0
    await executor.async_shutdown()
    await controller.async_shutdown()


async def test_shadow_token_equality_confuses_nested_tokens() -> None:
    """Closing the inner of nested feeds of one task takes that very token off the stack, not an outer one that
    looks the same, so `verdict` then reads the feed still open."""
    from datetime import datetime, timezone

    import asyncio

    from custom_components.spotnav.core import events as ev
    from custom_components.spotnav.execution.ownership_shadow import OwnershipShadow

    at = datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc)
    shadow = OwnershipShadow(lambda: ChargeSession(), now=lambda: at, drives=True)
    outer = shadow.begin()
    mid = shadow.begin()
    inner = shadow.begin()
    shadow.end(inner, ev.ConnectionUnknown())
    stack = shadow._stack[asyncio.current_task()]  # noqa: SLF001
    assert stack[-1] is mid, stack
    assert stack == [outer, mid] and stack[0] is outer
    shadow.end(mid, ev.ConnectionUnknown())
    shadow.end(outer, ev.ConnectionUnknown())
    assert not shadow._stack  # noqa: SLF001


@pytest.mark.parametrize("drives", [False, True])
async def test_a_charge_seen_off_is_not_handed_back_to_the_plan_at_every_report(hass, timers, drives):
    """Step 2: a pause whose stop failed, then the charge stops some other way. Today's code forgets the plan charge
    (`_async_forget_plan_charge`, plan_charge False, origin kept); the core still says `plan`. Driving, its write-back
    is one-way (it may clear today's owner fields, never set them back to an owner), so later reports neither hand
    the charge back to the plan nor save again."""
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.spotnav.execution import ownership_shadow
    from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED
    from .pause_world import pause_world, two_windows, SWITCH

    default = ownership_shadow.CORE_OWNERSHIP_DEFAULT
    ownership_shadow.CORE_OWNERSHIP_DEFAULT = drives
    try:
        world = await pause_world(hass, timers, plan=two_windows())

        async def failing() -> bool:
            raise HomeAssistantError("the charge control is unavailable")

        world.controller.adapter.async_stop = failing
        await world.executor.async_pause(PAUSE_UNTIL_RESUMED)
        await world.switch("off")
        saves = []
        real = world.controller._async_save

        async def counting(*a, **k):
            saves.append(1)
            return await real(*a, **k)

        world.controller._async_save = counting
        flips = []
        for index in range(6):
            hass.states.async_set(SWITCH, "off", {"report": index})
            await hass.async_block_till_done()
            flips.append(world.controller._plan_charge)
        written = world.controller.ownership_shadow.counts["written_back"]
        await world.shutdown()
        assert written <= 1 and len(saves) <= 1, (drives, written, len(saves), flips)
    finally:
        ownership_shadow.CORE_OWNERSHIP_DEFAULT = default
