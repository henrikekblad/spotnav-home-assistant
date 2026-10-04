"""The sun's rules own the charger when no plan window does: a charge the charger began by itself is taken
over, a person's Stop sticks, and a start credited by a charging battery is checked.

The field case (2026-10-04, an OCPP charger, Hybrid with no plan: `nothing_to_charge` / `solar_covers_need`,
a Sigenergy battery, `car_first`): the charger began a charge by itself at plug-in, at full current, and the
home battery discharged ~8 kW into the car while nothing took the charge over. The person stopped it; two
minutes later the sun's rules started it again at 16 A, counting the battery's charge as surplus, and the
battery turned to feed the car.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`).
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.planning.status_compose import HybridFacts, StatusFacts, compose_status
from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID, STRATEGY_SOLAR
from custom_components.spotnav.runtime import executor_for

from .world import controller_of, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")

BATTERY = "sensor.home_battery_power"
#: 15.7 A on three phases at 230 V.
FULL_A = 15.7


def _battery(hass: HomeAssistant, watts: float) -> None:
    """The home battery's power, positive while it charges."""
    hass.states.async_set(BATTERY, str(watts), {"unit_of_measurement": "W"})


async def _setup(hass: HomeAssistant, strategy: str = STRATEGY_HYBRID) -> tuple[Any, ...]:
    _battery(hass, 0.0)
    return await solar_setup(hass, strategy=strategy, battery_entity=BATTERY)


def _self_start(hass: HomeAssistant, prefix: str, amps: float) -> None:
    """The charger begins a charge by itself (at plug-in): nothing here sent a Start."""
    hass.states.async_set(f"switch.{prefix}", "on")
    set_charger_delivered_a(hass, prefix, amps)


@pytest.mark.parametrize("strategy", [STRATEGY_HYBRID, STRATEGY_SOLAR])
async def test_a_charge_the_charger_began_by_itself_on_the_battery_is_taken_over_and_stopped(
    hass: HomeAssistant, strategy: str
) -> None:
    """The owner's facts: the car draws 15.7 A, the battery discharges 8 kW into it, the grid reads ~0. The
    charge is taken over at once, asked down to the minimum, and stopped after `stop_delay_s` (300 s) with
    no `min_on_s` of a start the sun never made."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(
        hass, strategy
    )
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    _self_start(hass, prefix, FULL_A)
    _battery(hass, -8000.0)
    set_site_power_w(hass, "solar_site", 0.0)

    await tick_site(hass, site)

    assert coordinator.state is not None and coordinator.state.state == "disarming"
    assert controller.requested_current_a == 6, "the charge is asked down to the minimum at once"
    assert not turn_off_calls and not turn_on_calls

    clock.value = 301.0
    await tick_site(hass, site)
    assert coordinator.state.state == "off" and coordinator.state.action == "stop"
    assert len(turn_off_calls) == 1
    assert not turn_on_calls


async def test_a_charge_taken_over_is_regulated_to_the_real_surplus(hass: HomeAssistant) -> None:
    """With the sun exporting enough for 10 A beside the car, the charge it began by itself at 15.7 A is
    asked for 10 A, and goes on."""
    charger, site_entry, controller, coordinator, clock, _on, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    _self_start(hass, charger.entry_id, 6.0)
    _battery(hass, 0.0)
    # The car's 6 A plus 4 A exported: 10 A of sun.
    set_site_power_w(hass, "solar_site", -4.0 * 230.0)

    await tick_site(hass, site)

    assert coordinator.state is not None and coordinator.state.state == "on"
    assert controller.requested_current_a == 10
    assert not turn_off_calls


async def test_a_persons_charge_now_is_never_taken_over(hass: HomeAssistant) -> None:
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    await executor.async_manual_start()
    _self_start(hass, charger.entry_id, FULL_A)
    _battery(hass, -8000.0)
    set_site_power_w(hass, "solar_site", 0.0)

    for t in (0.0, 400.0, 1200.0):
        clock.value = t
        await tick_site(hass, site)

    assert controller.charge_origin == "manual"
    assert coordinator.state is not None and coordinator.state.state == "off"
    assert not turn_off_calls


async def test_leaving_the_strategy_does_not_stop_a_charge_it_took_over(hass: HomeAssistant) -> None:
    """Solar never stopped a charge it did not start when the strategy changed; one it took over is no
    different."""
    from dataclasses import replace

    from custom_components.spotnav.planning.auto_settings import STRATEGY_CHEAPEST
    from custom_components.spotnav.runtime import domain_data

    charger, site_entry, controller, coordinator, clock, _on, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    _self_start(hass, charger.entry_id, 6.0)
    set_site_power_w(hass, "solar_site", -4.0 * 230.0)
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on"

    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_CHEAPEST))
    await tick_site(hass, site)

    assert coordinator.state is None
    assert not turn_off_calls


async def test_a_persons_stop_sticks_until_they_start_again(hass: HomeAssistant) -> None:
    """The field replay: the person stops the charge; the battery then charges 8 kW from the sun with the
    grid at zero, which the sun's rules would count as surplus under `car_first`. Nothing starts the charger
    again, for an hour; the status says why. A person's Start ends it."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    _self_start(hass, prefix, FULL_A)
    _battery(hass, -8000.0)
    await tick_site(hass, site)

    await executor.async_manual_stop()
    hass.states.async_set(f"switch.{prefix}", "off")
    set_charger_delivered_a(hass, prefix, 0.0)
    _battery(hass, 8000.0)
    set_site_power_w(hass, "solar_site", 0.0)
    turn_on_calls.clear()

    for t in range(0, 3600, 30):
        clock.value = float(t)
        await tick_site(hass, site)

    assert not turn_on_calls, "a person's Stop must stick"
    assert coordinator.state is not None and coordinator.state.reason == "person_stopped"
    facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, charger))
    assert facts.person_stopped is True
    # Under the hybrid headline, worded by the clients (this charger has no price area, so its own status
    # is the setup's; the line is composed from the same fact).
    lines = compose_status(
        StatusFacts(now=facts.now, strategy=STRATEGY_HYBRID, hybrid=HybridFacts(), person_stopped=True)
    )["lines"]
    assert [line["code"] for line in lines][:2] == ["hybrid_unknown", "stopped_by_person"]

    # The person starts the charge: the Stop is over (their Start is theirs, and stays so).
    await executor.async_manual_start()
    assert controller.person_stopped is False


async def test_a_persons_stop_ends_with_the_plug_in(hass: HomeAssistant) -> None:
    charger, site_entry, controller, coordinator, clock, turn_on_calls, _off = await _setup(hass)
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    await executor.async_manual_stop()
    assert controller.person_stopped is True

    connected: list[bool | None] = [True]
    controller.adapter.vehicle_connected = lambda: connected[0]  # type: ignore[method-assign]
    controller._observe_connection()  # noqa: SLF001 - the observation point under test
    connected[0] = False
    controller._observe_connection()  # noqa: SLF001

    assert controller.person_stopped is False


async def test_a_start_on_a_battery_that_turns_to_feed_the_car_is_stopped_and_backs_off(
    hass: HomeAssistant,
) -> None:
    """The battery charges 8 kW with the grid at zero: under `car_first` that counts. The start is at the
    minimum (never 16 A); once the car draws, the battery turns to discharge into it and the grid stays at
    zero: the credit was false, the charge is stopped at once, and the battery's charge is not counted again
    for the back-off."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    _battery(hass, 8000.0)
    set_site_power_w(hass, "solar_site", 0.0)

    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert len(turn_on_calls) == 1
    assert controller.requested_current_a == 6, "a start is at the minimum current"
    hass.states.async_set(f"switch.{prefix}", "on")

    # The car draws 6 A; the battery now discharges into it, and the grid still reads zero.
    clock.value = 160.0
    set_charger_delivered_a(hass, prefix, 6.0)
    _battery(hass, -6.0 * 3 * 230.0)
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.reason == "battery_credit_false"
    assert len(turn_off_calls) == 1
    hass.states.async_set(f"switch.{prefix}", "off")
    set_charger_delivered_a(hass, prefix, 0.0)

    # The battery charges again: inside the back-off it is not counted, so nothing arms or starts.
    _battery(hass, 8000.0)
    turn_on_calls.clear()
    for t in range(190, 700, 30):
        clock.value = float(t)
        await tick_site(hass, site)
    assert not turn_on_calls
    assert coordinator.state.state == "off"
