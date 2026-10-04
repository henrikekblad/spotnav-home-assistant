"""Step 2 of the refactor, behind `CONF_CORE_OWNERSHIP` (off unless a charger's entry data says so): the charge-
ownership core decides, today's code acts on its verdicts and takes its owner back. Off, today's code decides as
ever. (`SPOTNAV_CORE_OWNERSHIP=1` runs the whole suite with it on.)"""

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


async def test_the_core_does_not_drive_unless_a_charger_says_so(hass: HomeAssistant, timers: FakeScheduler) -> None:
    default = ownership_shadow.CORE_OWNERSHIP_DEFAULT
    ownership_shadow.CORE_OWNERSHIP_DEFAULT = False
    try:
        controller, executor, _starts = await _charger(hass, drives=None)
        assert not controller.ownership_shadow.drives
        assert controller.ownership_shadow.diagnostics()["drives"] is False
        await executor.async_shutdown()
        await controller.async_shutdown()
        controller, executor, _starts = await _charger(hass, drives=True)
        assert controller.ownership_shadow.drives
    finally:
        ownership_shadow.CORE_OWNERSHIP_DEFAULT = default
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


async def test_the_cores_owner_is_taken_back_by_todays_two_fields(hass: HomeAssistant, timers: FakeScheduler) -> None:
    """Today's `charge_origin` and `plan_charge` follow the core's one owner (a charger seen off clears
    `plan_charge` at once and `charge_origin` only at the next notify pass; the core has one owner)."""
    controller, executor, _starts = await _charger(hass, drives=True)
    controller._charge_origin, controller._plan_charge = "plan_window", False  # noqa: SLF001 - today's drift
    assert controller._take_core_owner(ChargeSession(owner="plan"))  # noqa: SLF001
    assert (controller.charge_origin, controller._plan_charge) == ("plan_window", True)  # noqa: SLF001
    assert not controller._take_core_owner(ChargeSession(owner="top_off"))  # noqa: SLF001
    assert controller._take_core_owner(ChargeSession(owner="charger_self"))  # noqa: SLF001
    assert (controller.charge_origin, controller._plan_charge) == (None, False)  # noqa: SLF001
    assert controller._take_core_owner(ChargeSession(owner="person"))  # noqa: SLF001
    assert controller.charge_origin == "manual"
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
