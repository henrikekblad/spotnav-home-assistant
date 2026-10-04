"""Adversarial review of part B (spot fixes). Each test states the expected behaviour; a failure is a finding."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import CONF_CHARGE_CONTROL, CONF_CONTROL_PATH
from custom_components.spotnav.execution.controller import ChargingController

from .helpers import install_schedule
from .pause_world import later_window

pytestmark = pytest.mark.usefixtures("offline_relay")

BUTTONS = {
    CONF_CHARGE_CONTROL: "button.start",
    CONF_CONTROL_PATH: {"kind": "buttons", "start_entity_id": "button.start", "stop_entity_id": "button.stop"},
}


async def test_restart_with_unavailable_stop_button_does_not_fail_setup(hass: HomeAssistant) -> None:
    """A plan for tonight is stored; HA restarts in the afternoon and the charger's buttons are not loaded
    yet (or unavailable). Re-arming outside a window presses Stop; the press is not executed. Before 1.10.x
    the result was ignored; now `_stop_locked` raises `stop_not_executed` out of `async_initialize`, i.e.
    out of `async_setup_entry`."""
    hass.states.async_set("button.start", "unknown")
    hass.states.async_set("button.stop", "unknown")
    async_mock_service(hass, "button", "press")
    controller = ChargingController(hass, "entry_a", BUTTONS)
    await controller.async_initialize()
    await install_schedule(controller, later_window())
    assert controller.plan is not None
    await controller.async_shutdown()

    hass.states.async_remove("button.start")
    hass.states.async_remove("button.stop")  # the integration's entities are not there yet
    restarted = ChargingController(hass, "entry_a", BUTTONS)
    await restarted.async_initialize()  # must not raise: setup would fail and not be retried
    assert restarted.plan is not None
    await restarted.async_shutdown()


async def test_solar_start_held_by_balancing_then_resumed_by_regulator_is_still_stopped_by_solar(
    hass: HomeAssistant,
) -> None:
    """Bug 3's "solar back to off" also fires when load balancing holds the start back (`_start_locked`
    returns False after `_remember_paused_charge("solar")`). Solar goes `off`, but the controller keeps the
    balancing pause as solar's charge, and the regulator's resume later starts it with origin `solar`.
    Solar (off, `self_started_charge()` False because the origin is `solar`) never takes it back, so when the
    surplus is gone nobody stops a solar charge that now runs on grid power."""
    from .world import controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await solar_setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    controller.set_start_cap(lambda: 3.0)  # the site has 3 A of room: below the 6 A floor

    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert not turn_on_calls
    assert coordinator.state is not None and coordinator.state.state == "off"
    assert controller.paused_by_balancing  # the wish to charge, as solar's, is kept for the regulator

    # Headroom returns; the regulator resumes the paused charge (what `_async_maybe_resume_paused_charge` does).
    controller.set_start_cap(None)
    assert await controller.async_battery_probe_start(6, capped=True)
    hass.states.async_set(f"switch.{prefix}", "on")
    set_charger_delivered_a(hass, prefix, 6.0)
    assert controller.charge_origin == "solar"

    # The sun goes; the car imports 3 kW from the grid for a long time.
    for t in range(200, 3000, 50):
        clock.value = float(t)
        set_site_power_w(hass, "solar_site", 1000.0)
        await tick_site(hass, site)
    assert turn_off_calls, (
        f"a solar-origin charge runs on grid power and solar never stops it (solar state "
        f"{coordinator.state.state if coordinator.state else None})"
    )


async def test_easee_two_send_regulator_write_does_not_lift_a_pause_sent_between_the_sends(
    hass: HomeAssistant,
) -> None:
    """Bug 7: a regulator write with a suspect cache sends amps-1 then amps. `_stop_in_flight` and Easee's
    `_paused()` are checked once, before the first send, without the operation lock. A stop that lands
    between the two sends is lifted by the second (Easee resumes on a limit above 0)."""
    import asyncio

    from custom_components.spotnav.flows.charger_detection import detect_charger

    from .charger_helpers import detected_config
    from .charger_shapes import SHAPES, register_shape

    ids = register_shape(hass, SHAPES["easee"])
    config = detected_config(detect_charger(hass, ids["device_id"]))
    order: list[str] = []
    controller_box: list[ChargingController] = []
    stop_task: list[asyncio.Task] = []

    async def action(call: Any) -> None:
        order.append(call.data["action_command"])

    async def limit(call: Any) -> None:
        order.append(f"limit {call.data['current']}")
        if len(order) == 1:
            # While the first of two writes is on its way, a person presses Stop.
            stop_task.append(hass.async_create_task(controller_box[0].async_stop()))
            for _ in range(20):
                await asyncio.sleep(0)

    hass.services.async_register("easee", "action_command", action)
    hass.services.async_register("easee", "set_charger_dynamic_limit", limit)
    hass.states.async_set("sensor.easee_status", "charging")
    controller = ChargingController(hass, "entry_a", config)
    controller_box.append(controller)
    await controller.async_initialize()
    controller.adapter.current._suspect = True  # noqa: SLF001 - a cache the last read-back disagreed with

    await controller.async_apply_regulated_current(10, must_lower=False)
    await stop_task[0]
    assert "pause" in order
    assert order[-1] == "pause", f"a limit landed after the pause and resumes the charge: {order}"
    await controller.async_shutdown()


async def test_a_balancing_pause_does_not_outlive_the_plug_in_and_restart(hass: HomeAssistant) -> None:
    """Bug 9 now persists the balancing pause (with origin `manual`), but nothing ends it at the unplug. A
    person's Start is paused by load balancing, the car leaves (the manual pause ends with the unplug), HA
    restarts, and a car is plugged in again: the regulator's resume (`_charge_still_wanted` is True with no
    pause stored) starts a "manual" charge nobody asked for in this plug-in, outside any plan."""
    from .test_replug import Plug, _obedient_switch

    config = {CONF_CHARGE_CONTROL: "switch.a"}
    hass.states.async_set("switch.a", "off")
    _obedient_switch(hass)
    controller = ChargingController(hass, "entry_a", config)
    await controller.async_initialize()
    plug = Plug(hass, controller, "switch.a")
    await plug.set(True)
    await controller.async_start(10, manual=True)
    await controller._regulated_stop("pause")  # noqa: SLF001 - the regulator's pause below the floor
    assert controller.paused_by_balancing
    await plug.set(False)  # the car leaves: its plug-in session is over
    await hass.async_block_till_done()
    await controller.async_shutdown()

    restarted = ChargingController(hass, "entry_a", config)
    await restarted.async_initialize()
    plug = Plug(hass, restarted, "switch.a")
    await plug.set(False)
    await plug.set(True)  # a new plug-in
    assert not restarted.paused_by_balancing, (
        "a balancing pause from the previous plug-in is resumed on the new one as a person's charge"
    )
    await restarted.async_shutdown()


async def test_a_solar_stop_that_was_not_executed_is_retried(hass: HomeAssistant) -> None:
    """Bug 3: solar's stop verdict, the charge control unavailable for that one tick. `_stop_locked` now
    keeps the origin `solar` and raises `stop_not_executed`; the raise escapes `_apply_verdict` and
    `_async_evaluate_guarded` (an unhandled task error), after `SolarController.observe` already moved to
    `off`. Next ticks: solar is off and the charge is not self-started (origin `solar`), so nobody stops it
    when the control is back."""
    from .world import arm_and_start, controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await solar_setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    await arm_and_start(hass, site, clock)
    assert coordinator.state.state == "on"
    hass.states.async_set(f"switch.{prefix}", "on")
    set_charger_delivered_a(hass, prefix, 6.0)

    real_stop = controller.adapter.async_stop
    refused: list[int] = []

    async def not_executed() -> bool:
        refused.append(1)
        return False

    controller.adapter.async_stop = not_executed  # type: ignore[method-assign]
    t = 200
    while not refused and t < 5000:
        clock.value = float(t)
        set_site_power_w(hass, "solar_site", 1000.0)
        await tick_site(hass, site)
        t += 50
    assert refused, "solar never decided to stop"
    controller.adapter.async_stop = real_stop  # type: ignore[method-assign]  # the control is back
    from datetime import timedelta

    from freezegun import freeze_time
    from homeassistant.util import dt as dt_util

    from custom_components.spotnav.execution.controller import STOP_ACK_S

    with freeze_time(dt_util.utcnow() + timedelta(seconds=STOP_ACK_S + 5)):
        for _ in range(40):
            clock.value = float(t)
            set_site_power_w(hass, "solar_site", 1000.0)
            await tick_site(hass, site)
            t += 50
    assert turn_off_calls, (
        f"the charge runs on grid power and nobody retries the stop (solar {coordinator.state.state}, "
        f"origin {controller.charge_origin})"
    )


async def test_a_start_stopped_at_once_frees_its_reservation(hass: HomeAssistant) -> None:
    """Bug 6: a reservation is dropped only when the start did not go out, or after 120 s. A charge that is
    stopped right after its start (a person's Stop, a target already met, a window end, a failed probe)
    keeps the whole margin reserved: another charger's start in the next two minutes is refused."""
    from .test_start_reservation import _two_charger_site

    a, b, site, _, _ = await _two_charger_site(hass, site_a=4.0)  # 16 A of margin
    assert await a.async_start(16) is True
    await a.async_stop()
    hass.states.async_set("switch.ca", "off")
    await hass.async_block_till_done()
    assert site.start_allowance_a(b.entry_id) == pytest.approx(16.0), "nothing is on its way to A any more"


async def test_a_reservation_is_not_counted_twice_when_the_chargers_own_current_is_unreadable(
    hass: HomeAssistant,
) -> None:
    """Bug 6: what a starting charger has drawn is read from its own current sensors. When those read
    nothing (unavailable, a charger without them) the reservation stays whole for 120 s, while the site
    meter already shows the draw: the margin is taken twice and the other charger is starved."""
    from .test_start_reservation import _two_charger_site
    from .world import PHASES

    a, b, site, _, _ = await _two_charger_site(hass, site_a=0.0)  # 20 A of margin
    assert await a.async_start(10) is True
    for p in PHASES:
        hass.states.async_set(f"sensor.ca_{p.lower()}", "unavailable")
    set_site_current = __import__("tests.world", fromlist=["set_site_current_a"]).set_site_current_a
    set_site_current(hass, "pair_site", 10.0)  # A draws 10 A, seen by the site meter
    await hass.async_block_till_done()
    assert site.start_allowance_a(b.entry_id) == pytest.approx(10.0), "A's 10 A are counted once"


async def test_installing_a_plan_for_later_with_an_unavailable_stop_button(hass: HomeAssistant) -> None:
    """Same root as the restart case: arming a plan outside its windows presses Stop on a button/select
    charger (its state cannot say it is off). With the charger offline the press is not executed and the
    install raises `stop_not_executed` and is rolled back (with a previous plan in place, the rollback's own
    re-arm raises too: `rollback_failed`). Auto cannot install a plan while the charger is offline, though
    nothing is charging."""
    hass.states.async_set("button.start", "unavailable")
    hass.states.async_set("button.stop", "unavailable")
    async_mock_service(hass, "button", "press")
    controller = ChargingController(hass, "entry_a", BUTTONS)
    await controller.async_initialize()
    await install_schedule(controller, later_window())
    assert controller.plan is not None
    await controller.async_shutdown()
