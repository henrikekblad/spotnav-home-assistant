"""The site recomputes once per turn of the event loop, and calculates once per reading.

A meter that writes its phases one entity at a time (power, reactive power and voltage on each of three phases) used
to recompute the site, and queue an apply pass, on every partial state. Now the state changes of one turn of the loop
make one recompute at the next turn; while an import overload stands a change is still recomputed at once. The site
calculation is reused while no reading changed (`CALCULATION_REUSE_S`).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.spotnav.site import site_capacity_controller as scc

from .test_yield_stepping_wiring import _yield_setup
from .world import controller_of, set_charger_delivered_a, set_site_current_a

_OVERLOAD = "reducing_current_due_to_active_import_overload"


async def _listening_site(hass: HomeAssistant, entry_id: str):
    """`_yield_setup`'s site and charger (20 A fuse, 1 A margin, the car asking 16 A), with the site's state listener
    on, as in a running Home Assistant, and every recompute counted."""
    site_entry, charger, configure_calls = await _yield_setup(
        hass, entry_id=entry_id, yield_stepping_enabled=False
    )
    controller = controller_of(hass, site_entry.entry_id)
    controller._state_listener_cancel = async_track_state_change_event(
        hass, controller._tracked_entity_ids(), controller._async_state_changed
    )
    recomputes: list[int] = []
    real = controller._recompute

    def counted(**kwargs: Any) -> None:
        recomputes.append(1)
        real(**kwargs)

    controller._recompute = counted  # type: ignore[method-assign]
    return controller, charger, configure_calls, recomputes


async def _one_turn() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_many_state_changes_in_one_turn_make_one_recompute(hass: HomeAssistant) -> None:
    controller, charger, _calls, recomputes = await _listening_site(hass, "coalesce")
    set_charger_delivered_a(hass, "coalesce_charger", 10.0)
    set_site_current_a(hass, "coalesce_site", 11.0)
    await hass.async_block_till_done()
    assert not controller._import_overload_now()
    recomputes.clear()

    # Nine entities (power, reactive power, voltage on each phase), each written on its own.
    for amps in (12.0, 13.0, 14.0):
        set_site_current_a(hass, "coalesce_site", amps)
    assert recomputes == [], "nothing recomputed on a partial state"
    await _one_turn()
    assert len(recomputes) == 1
    await hass.async_block_till_done()
    assert len(recomputes) == 1
    assert controller.result.measured_phase_current_a["L1"] == pytest.approx(14.0)


async def test_an_overload_is_acted_on_in_the_recompute_that_sees_it(hass: HomeAssistant) -> None:
    controller, charger, configure_calls, recomputes = await _listening_site(hass, "coalesceover")
    set_charger_delivered_a(hass, "coalesceover_charger", 10.0)
    set_site_current_a(hass, "coalesceover_site", 15.0)
    await hass.async_block_till_done()
    configure_calls.clear()
    recomputes.clear()

    # The site goes 1.5 A over its 19 A limit, written phase by phase: one recompute, and its apply pass lowers the
    # car from that very recompute.
    set_site_current_a(hass, "coalesceover_site", 20.5)
    await _one_turn()
    assert len(recomputes) == 1
    assert controller.regulator_decisions[charger.entry_id].reason == _OVERLOAD
    await hass.async_block_till_done()
    assert len(recomputes) == 1
    assert [call.data["value"] for call in configure_calls] == ["1.8,2.10"]


async def test_while_an_overload_stands_a_change_is_recomputed_at_once(hass: HomeAssistant) -> None:
    controller, charger, _calls, recomputes = await _listening_site(hass, "coalescenow")
    set_charger_delivered_a(hass, "coalescenow_charger", 10.0)
    set_site_current_a(hass, "coalescenow_site", 20.5)
    await hass.async_block_till_done()
    assert controller._import_overload_now()
    recomputes.clear()

    hass.states.async_set("sensor.coalescenow_site_power_l1", str(21.0 * 230.0), {"unit_of_measurement": "W"})
    assert len(recomputes) == 1, "no wait for the rest of the loop's turn"


async def test_one_reading_is_calculated_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory
) -> None:
    controller, charger, _calls, _recomputes = await _listening_site(hass, "calconce")
    calculations: list[int] = []
    real = scc.calculate_site_capacity

    def counted(*args: Any, **kwargs: Any):
        calculations.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(scc, "calculate_site_capacity", counted)
    controller._calculation = None

    # A recompute, the apply pass it queues, a start's allowance and its reservation: one calculation.
    controller._recompute()
    await hass.async_block_till_done()
    controller.start_allowance_a(charger.entry_id)
    controller.reserve_start(charger.entry_id, 6.0)
    assert len(calculations) == 1

    # A new reading is calculated again.
    set_site_current_a(hass, "calconce_site", 12.0)
    await hass.async_block_till_done()
    assert len(calculations) == 2

    # The same readings, but older than the reuse allows: their ages have moved on.
    freezer.tick(timedelta(seconds=scc.CALCULATION_REUSE_S + 0.5))
    controller.start_allowance_a(charger.entry_id)
    assert len(calculations) == 3
