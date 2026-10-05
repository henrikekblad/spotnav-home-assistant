"""A charge is nobody's as soon as the charger is seen off (a readable off report) with no start of ours on its way
(`_async_progress_state_changed`, and the core's `charger_reported_off`): a charge the charger later begins by itself
does not inherit the ended one's owner. Research: plans/research_integrations/q1_owner_after_off_2026-10-05.md."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.execution import ownership_shadow
from custom_components.spotnav.execution.controller import ChargingPlan

from .pause_world import pause_world, SWITCH, two_windows
from .relay import FakeScheduler
from .test_solar_takeover import _battery, _self_start, _setup, FULL_A
from .world import controller_of, set_site_power_w, tick_site


def _owner(controller: Any) -> tuple[Any, Any, str]:
    return controller.charge_origin, controller._plan_charge, controller.ownership_shadow.session.owner  # noqa: SLF001


@pytest.mark.parametrize("drives", [False, True])
async def test_a_plan_charge_that_pauses_and_resumes_in_its_window_is_the_plans_again_and_stops_at_its_end(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """Scenario A: the window's charge stops (the car pauses, or the charger's own app), then the charger begins
    again by itself inside the window. Seen off, the charge is nobody's; begun again, the window claims it as the
    plan's (no command, only who owns it), and the window's end stops it as before."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
    world = await pause_world(hass, timers, plan=two_windows())
    controller = world.controller
    assert controller.charging and _owner(controller) == ("plan_window", True, "plan")
    starts, stops = len(world.starts), len(world.stops)

    await world.switch("off")
    assert _owner(controller) == (None, False, "none"), "seen off: nobody's, today's fields and the core alike"

    await world.switch("on")
    assert _owner(controller) == ("plan_window", True, "plan"), "begun again in the window: the plan's"
    assert (len(world.starts), len(world.stops)) == (starts, stops), "a claim sends nothing"

    await world.fire("_async_end_callback")
    assert len(world.stops) == stops + 1 and not controller.charging, "the window's end stops it as ever"
    assert _owner(controller)[2] == "none"
    assert controller.ownership_shadow.counts["disagreements"] == 0
    await world.shutdown()


@pytest.mark.parametrize("drives", [False, True])
async def test_a_start_on_its_way_keeps_its_owner_through_an_off_report(
    hass: HomeAssistant, timers: FakeScheduler, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """An off report while a start of ours awaits the charger's answer does not end that start's ownership."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
    world = await pause_world(hass, timers)
    hass.services.async_remove("switch", "turn_on")

    async def unanswered(_call: Any) -> None:
        return

    hass.services.async_register("switch", "turn_on", unanswered)
    await world.executor.async_manual_start()
    assert world.controller.start_pending and world.controller.charge_origin == "manual"
    hass.states.async_set(SWITCH, "off", {"report": "still off"})
    await hass.async_block_till_done()
    assert world.controller.charge_origin == "manual"
    assert world.controller.ownership_shadow.session.owner == "person"
    await world.shutdown()


@pytest.mark.parametrize("drives", [False, True])
async def test_hybrid_a_charge_resumed_inside_a_window_is_not_taken_over_by_the_sun(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, drives: bool
) -> None:
    """Scenario F: Hybrid, a plan window open, nothing for the sun (the home battery feeds the house). The window's
    charge stops and the charger begins again by itself: it is the plan's again, and the sun's rules, which a charge
    nobody owns would face (I4: stopped at the first reading with no surplus), leave it alone through the window."""
    monkeypatch.setattr(ownership_shadow, "CORE_OWNERSHIP_DEFAULT", drives)
    charger, site_entry, controller, coordinator, clock, _turn_on_calls, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    now = dt_util.utcnow()
    await controller.async_install(
        ChargingPlan(
            start=(now - timedelta(minutes=1)).isoformat(),
            end=(now + timedelta(minutes=30)).isoformat(),
            amps=10,
            phases=3,
            auto_identity="q1-hybrid-window",
        )
    )
    await hass.async_block_till_done()
    assert controller.plan_window_active_now
    _battery(hass, -8000.0)
    set_site_power_w(hass, "solar_site", 0.0)
    _self_start(hass, charger.entry_id, FULL_A)
    await hass.async_block_till_done()
    clock.value = 0.0
    await tick_site(hass, site)
    stops = len(turn_off_calls)

    hass.states.async_set(f"switch.{charger.entry_id}", "off")
    await hass.async_block_till_done()
    assert (controller.charge_origin, controller.ownership_shadow.session.owner) == (None, "none")

    _self_start(hass, charger.entry_id, FULL_A)
    await hass.async_block_till_done()
    for t in (10.0, 130.0, 400.0, 1000.0):
        clock.value = t
        await tick_site(hass, site)

    assert len(turn_off_calls) == stops, "the sun takes nothing over while the plan's window owns the charger"
    assert controller.charging
    assert coordinator.state is not None and coordinator.state.held_by_plan is True
    assert _owner(controller) == ("plan_window", True, "plan"), "claimed by the window, not left as nobody's"
    assert controller.ownership_shadow.counts["disagreements"] == 0
