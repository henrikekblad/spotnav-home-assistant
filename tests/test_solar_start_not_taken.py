"""A start the car never takes ends the sun's charge as the car's own end, and waits for the retry.

The field case (an Easee on `solar`): the sun armed, started and verified its start, but the Easee stayed
`awaiting_start` and the car drew nothing. The charge control of an Easee is on only while it charges, so the
watch never saw it on and never ended anything: solar stayed `on`, flapped between `on` and `disarming` for a
quarter of an hour writing a current every few seconds, and the card said the surplus was fading.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`); the wall clock
(the start's acknowledgement timeout) is moved with `freezer`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution.solar_execution import CAR_IDLE_S
from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.planning.status_compose import compose_status

from .test_solar_current_writer import _sun_for, _watty
from .world import controller_of, go_auto, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")


def _status_codes(hass: HomeAssistant, charger_entry_id: str) -> list[str]:
    """The status block's codes as the card and the app get them, prices taken as ready."""
    entry = hass.config_entries.async_get_entry(charger_entry_id)
    facts = replace(dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, entry)), price_state="ready")
    return [line["code"] for line in compose_status(facts)["lines"]]


async def _start_left_waiting(hass: HomeAssistant, freezer) -> tuple:
    """The sun starts the Easee at t=125; it stays `awaiting_start`, the car drawing nothing, and the start's
    acknowledgement times out."""
    charger, site, controller, coordinator, clock, easee = await _watty(hass)
    # Settings complete enough for a status of the strategy's own.
    await go_auto(hass, charger.entry_id, strategy=STRATEGY_SOLAR, amps=16)
    _sun_for(hass, car_a=0.0, available_a=9.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert "resume" in easee.commands
    assert coordinator.state.state == "on"
    freezer.tick(timedelta(seconds=60))
    clock.value = 185.0
    await tick_site(hass, site)
    assert not controller.start_pending
    return charger, site, controller, coordinator, clock, easee


async def test_a_start_the_car_never_takes_ends_as_car_stopped_after_the_grace(hass: HomeAssistant, freezer) -> None:
    charger, site, controller, coordinator, clock, easee = await _start_left_waiting(hass, freezer)

    # Within the grace the sun's charge runs on: the car may still wake up.
    clock.value = 185.0 + CAR_IDLE_S - 5.0
    await tick_site(hass, site)
    assert coordinator.state.state in ("on", "disarming")
    commands_before = len(easee.commands)

    clock.value = 185.0 + CAR_IDLE_S + 1.0
    await tick_site(hass, site)
    state = coordinator.state
    assert (state.state, state.reason) == ("off", "car_stopped")
    assert state.retry_at is not None
    assert len(easee.commands) > commands_before, "the stop was sent"
    assert _status_codes(hass, charger.entry_id)[0] == "solar_car_stopped"

    # No current written and no start sent again before the retry (30 minutes).
    limits = list(easee.limits)
    resumes = easee.commands.count("resume")
    for t in (300.0, 900.0, 1700.0):
        clock.value = 185.0 + CAR_IDLE_S + t
        await tick_site(hass, site)
        assert (coordinator.state.state, coordinator.state.reason) == ("off", "car_stopped")
    assert easee.limits == limits
    assert easee.commands.count("resume") == resumes


async def test_a_start_the_charger_itself_holds_is_not_ended(hass: HomeAssistant, freezer) -> None:
    """The Easee's own scheduler holds the car (`awaiting_scheduled_start`): the start was taken, nothing for the
    car to answer."""
    _charger, site, _controller, coordinator, clock, easee = await _start_left_waiting(hass, freezer)
    await easee.status("awaiting_scheduled_start")
    for t in (CAR_IDLE_S, 2 * CAR_IDLE_S, 3 * CAR_IDLE_S):
        clock.value = 185.0 + t
        await tick_site(hass, site)
        assert coordinator.state.reason != "car_stopped"
        assert coordinator.state.state in ("on", "disarming")


async def test_a_charger_with_a_switch_is_left_to_its_switch(hass: HomeAssistant, freezer) -> None:
    """A plain switch says whether a start was taken by itself: one left off is not read as the car's end."""
    charger, site_entry, _controller, coordinator, clock, turn_on_calls, _off = await solar_setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    set_site_power_w(hass, "solar_site", -4200.0 / 3)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert len(turn_on_calls) == 1 and coordinator.state.state == "on"
    freezer.tick(timedelta(seconds=60))
    for t in (185.0, 185.0 + CAR_IDLE_S + 1.0, 185.0 + 3 * CAR_IDLE_S):
        clock.value = t
        await tick_site(hass, site)
        assert coordinator.state.reason != "car_stopped"
