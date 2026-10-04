"""The sun's rules under a person's Start or Stop: solar neither starts nor stops a charger a person
owns for the plug-in session, and a strategy switch does not cost a charge solar runs.

Bug 4 (S-b): a solar start already computed when a person's Stop landed went through and the charge ran
owned by nobody. Bug 5 (S-c): a person's Start while solar ran was stopped by solar's next stop verdict.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.planning.auto_settings import (
    MANUAL_START,
    MANUAL_STOP,
    PAUSE_MANUAL,
    STRATEGY_HYBRID,
    STRATEGY_SOLAR,
    PauseIntent,
)
from custom_components.spotnav.runtime import domain_data, executor_for

from .world import controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _solar_on(hass: HomeAssistant, strategy: str = STRATEGY_SOLAR):
    charger, site_entry, controller, coordinator, clock, turn_on, turn_off = await solar_setup(hass, strategy=strategy)
    site = controller_of(hass, site_entry.entry_id)
    controller.adapter.vehicle_connected = lambda: True  # type: ignore[method-assign]
    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert len(turn_on) == 1
    hass.states.async_set(f"switch.{charger.entry_id}", "on")
    set_charger_delivered_a(hass, charger.entry_id, 6.0)
    set_site_power_w(hass, "solar_site", -20.0)
    clock.value = 160.0
    await tick_site(hass, site)
    return charger, site, controller, coordinator, clock, turn_on, turn_off


async def test_the_sun_does_not_start_what_a_person_stopped(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, turn_on, turn_off = await _solar_on(hass)
    executor = executor_for(hass, charger.entry_id)

    await executor.async_manual_stop()
    hass.states.async_set(f"switch.{charger.entry_id}", "off")
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    set_site_power_w(hass, "solar_site", -3000.0)
    for t in range(200, 2000, 30):
        clock.value = float(t)
        await tick_site(hass, site)

    assert executor.pause_intent.choice == PAUSE_MANUAL and executor.pause_intent.action == MANUAL_STOP
    assert len(turn_on) == 1, "no surplus starts a charge a person stopped"
    assert await executor.async_solar_start(10) is False, "nor a start computed before the Stop landed"


async def test_the_sun_does_not_stop_what_a_person_started(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, turn_on, turn_off = await _solar_on(hass)
    executor = executor_for(hass, charger.entry_id)

    await executor.async_manual_start()
    assert executor.pause_intent.action == MANUAL_START
    set_site_power_w(hass, "solar_site", 2000.0)  # importing: solar would stop its own charge
    for t in range(200, 3000, 30):
        clock.value = float(t)
        await tick_site(hass, site)

    assert turn_off == [], "solar never stops a charge a person started"
    await executor.async_solar_stop()
    assert turn_off == []


async def test_a_person_start_while_solar_is_off_is_not_stopped_either(hass: HomeAssistant) -> None:
    charger, site_entry, controller, coordinator, clock, turn_on, turn_off = await solar_setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    controller.adapter.vehicle_connected = lambda: True  # type: ignore[method-assign]
    executor = executor_for(hass, charger.entry_id)

    await executor.async_manual_start(10)
    hass.states.async_set(f"switch.{charger.entry_id}", "on")
    set_charger_delivered_a(hass, charger.entry_id, 10.0)
    set_site_power_w(hass, "solar_site", 3000.0)
    for t in range(0, 1500, 30):
        clock.value = float(t)
        await tick_site(hass, site)

    assert turn_off == []


async def test_resuming_auto_hands_the_charger_back_to_the_sun(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, turn_on, turn_off = await _solar_on(hass)
    executor = executor_for(hass, charger.entry_id)
    await executor.async_manual_stop()
    hass.states.async_set(f"switch.{charger.entry_id}", "off")
    set_charger_delivered_a(hass, charger.entry_id, 0.0)

    await executor.async_resume()
    assert executor.pause_intent == PauseIntent()
    set_site_power_w(hass, "solar_site", -1400.0)
    for t in range(400, 1500, 30):
        clock.value = float(t)
        await tick_site(hass, site)

    assert len(turn_on) == 2, "the sun starts the charger again once Auto is resumed"


async def test_a_switch_to_hybrid_while_solar_runs_the_charge_keeps_it(hass: HomeAssistant) -> None:
    charger, site, controller, coordinator, clock, turn_on, turn_off = await _solar_on(hass)
    store = domain_data(hass).auto_store

    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_HYBRID))
    for t in range(200, 400, 30):
        clock.value = float(t)
        await tick_site(hass, site)

    assert turn_off == [], "the same sun runs the charge under hybrid"
    assert coordinator.state is not None and coordinator.state.state in ("on", "disarming")
