"""The charge-ownership core drives for every charger: it decides, today's code acts on its verdicts and reads its
owner from the session, and is still computed beside it and compared."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CORE_OWNERSHIP
from custom_components.spotnav.core.ownership import Start
from custom_components.spotnav.execution import session_runner
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


@pytest.mark.parametrize("configured", [None, False, True])
async def test_the_core_drives_whatever_an_earlier_release_stored(
    hass: HomeAssistant, timers: FakeScheduler, configured: bool | None
) -> None:
    """The choice of the logic before the core is gone: a charger whose data still says `core_ownership` (a controller
    built from it before setup drops the key) is driven by the core all the same."""
    controller, executor, _starts = await _charger(hass, drives=configured)
    assert controller.session_runner.drives is True
    assert controller.session_runner.diagnostics()["drives"] is True
    await executor.async_shutdown()
    await controller.async_shutdown()


@pytest.mark.parametrize(("drives", "started"), [(True, False)])
async def test_todays_code_acts_on_the_cores_verdict_when_it_drives(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch, drives: bool, started: bool
) -> None:
    """A core that would not start a window: no start goes out."""
    real = session_runner.decide

    def no_window_starts(session, event, now):
        session, commands = real(session, event, now)
        if event.kind == "window_start":
            commands = tuple(command for command in commands if not isinstance(command, Start))
        return session, commands

    monkeypatch.setattr(session_runner, "decide", no_window_starts)
    controller, executor, starts = await _charger(hass, drives=drives)
    before = len(starts)
    await install_schedule(controller, open_window())
    await hass.async_block_till_done()
    assert (len(starts) > before) is started
    assert controller.charging is started
    await executor.async_shutdown()
    await controller.async_shutdown()


@pytest.mark.parametrize(
    ("owner", "fields"),
    [
        ("plan", ("plan_window", True)),
        ("top_off", ("plan_window", True)),
        ("person", ("manual", False)),
        ("solar", ("solar", False)),
        ("charge_now", ("other", False)),
        ("min_soc", ("min_soc", False)),
        ("charger_self", (None, False)),
        ("none", (None, False)),
    ],
)
async def test_the_cores_owner_is_todays_two_fields_both_ways(
    hass: HomeAssistant, timers: FakeScheduler, owner: str, fields: tuple
) -> None:
    """`charge_origin` and `plan_charge` are the session's owner: every owner the core names is read as them, nobody's
    and the charger's own as none, and nothing else can write them."""
    controller, executor, _starts = await _charger(hass, drives=True)
    shadow = controller.session_runner
    shadow.session = shadow.session.with_changes(owner=owner)
    assert (controller.charge_origin, controller._is_plan_charge) == fields  # noqa: SLF001
    with pytest.raises(AttributeError):
        controller._owner_origin = "manual"  # noqa: SLF001 - a view of the session
    assert (controller.charge_origin, controller._is_plan_charge) == fields  # noqa: SLF001
    await executor.async_shutdown()
    await controller.async_shutdown()


async def test_a_persons_stop_and_start_follow_the_core_when_it_drives(
    hass: HomeAssistant, timers: FakeScheduler
) -> None:
    controller, executor, _starts = await _charger(hass, drives=True)
    await executor.async_manual_stop()
    pause = executor.pause_intent
    assert (pause.choice, pause.action, pause.scope) == ("manual", "stop", "plug_in")
    assert controller.session_runner.session.manual is not None
    await executor.async_manual_start()
    pause = executor.pause_intent
    assert (pause.action, pause.scope) == ("start", "plug_in")
    assert controller.charge_origin == "manual"
    await executor.async_shutdown()
    await controller.async_shutdown()


@pytest.mark.parametrize("drives", [True])
async def test_a_charge_seen_off_is_not_handed_back_to_the_plan_at_every_report(hass, timers, drives):
    """Step 2: a pause whose stop failed, then the charge stops some other way. Today's code forgets the plan charge
    (`_async_forget_plan_charge`, plan_charge False, origin kept). Driving, the owner is the core's (step 7): the
    report of the charger off made it nobody's, and later reports neither hand the charge back to the plan nor save
    again."""
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.spotnav.planning.auto_settings import PAUSE_UNTIL_RESUMED
    from .pause_world import pause_world, two_windows, SWITCH

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
        flips.append(world.controller._is_plan_charge)
    await world.shutdown()
    assert not any(flips) and len(saves) <= 1, (len(saves), flips)
