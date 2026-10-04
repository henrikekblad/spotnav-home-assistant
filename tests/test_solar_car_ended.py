"""Solar follows the charger: a charge the car ended by itself is not shown, or counted, as running.

The field case (2026-10-04, Solar, an OCPP charger, an EV6 at 98 %): the sun started the car at 6 A; 27
minutes later the car stopped drawing by itself (0.6 A measured), a minute after that the connector went
`Finishing` and the charge control off. Forty minutes on, solar still said `on` / `on_steady`, counted the
measured current's leftover 0.6 A as the car's draw, never started again, and the card said solar was
charging.

Every tick is an explicit site recompute at a hand-set instant (`tests.test_solar_execution`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.api import dashboard as dashboard_api
from custom_components.spotnav.execution.solar_execution import CAR_IDLE_S

from custom_components.spotnav.planning.auto_settings import STRATEGY_SOLAR
from custom_components.spotnav.planning.status_compose import compose_status

from .world import controller_of, go_auto, set_charger_delivered_a, set_site_power_w, solar_setup, tick_site

pytestmark = pytest.mark.usefixtures("offline_relay")

CPID = "halo"
STATUS = f"sensor.{CPID}_connector_1_status_connector"
CURRENT = f"sensor.{CPID}_connector_1_current_import"
#: What the sun gives the house beyond its own load, in watts: enough for 6 A on three phases.
SUN_W = 4200.0
WATTS_PER_A = 3 * 230.0


def _connector(hass: HomeAssistant, status: str, amps: float) -> None:
    hass.states.async_set(STATUS, status)
    hass.states.async_set(CURRENT, str(amps), {"unit_of_measurement": "A"})


def _car_draws(hass: HomeAssistant, prefix: str, amps: float) -> None:
    """The car draws `amps`: the charger's own measured current, and the grid exports the rest of the sun."""
    set_charger_delivered_a(hass, prefix, amps)
    set_site_power_w(hass, "solar_site", -(SUN_W - amps * WATTS_PER_A) / 3)


async def _started(hass: HomeAssistant) -> tuple[Any, ...]:
    """Solar has started the car at 6 A at t=125 and the car draws it."""
    world = await solar_setup(hass, ocpp_target=(CPID, 1))
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = world
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    # Settings complete enough for a status of the strategy's own.
    await go_auto(hass, prefix, strategy=STRATEGY_SOLAR, phases=3)
    _connector(hass, "Preparing", 0.0)
    _car_draws(hass, prefix, 0.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.action == "start"
    assert len(turn_on_calls) == 1
    hass.states.async_set(f"switch.{prefix}", "on")
    _connector(hass, "Charging", 6.0)
    _car_draws(hass, prefix, 6.0)
    clock.value = 130.0
    await tick_site(hass, site)
    assert coordinator.state.state == "on"
    return (*world, site)


def _status_codes(hass: HomeAssistant, charger_entry_id: str) -> list[str]:
    """The status block's codes as the card and the app get them, prices taken as ready (the test world
    has none, which would block the whole block)."""
    entry = hass.config_entries.async_get_entry(charger_entry_id)
    facts = replace(dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, entry)), price_state="ready")
    return [line["code"] for line in compose_status(facts)["lines"]]


async def _car_ends_the_charge(hass: HomeAssistant, world: tuple[Any, ...]) -> None:
    """The replay's middle: the car tapers to 0.6 A at t=1700, and at t=1760 the connector goes `Finishing`
    and the charge control off, the measured current still reading 0.6 A."""
    charger, _site_entry, _controller, _coordinator, clock, *_rest, site = world
    prefix = charger.entry_id
    clock.value = 1700.0
    _connector(hass, "Charging", 0.6)
    _car_draws(hass, prefix, 0.6)
    await tick_site(hass, site)
    clock.value = 1760.0
    hass.states.async_set(f"switch.{prefix}", "off")
    _connector(hass, "Finishing", 0.6)
    # The car draws nothing: the grid exports the whole sun, while the charger's sensors keep 0.6 A.
    set_site_power_w(hass, "solar_site", -SUN_W / 3)
    await tick_site(hass, site)


async def test_the_field_case_leaves_on_when_the_car_ends_the_charge(hass: HomeAssistant) -> None:
    world = await _started(hass)
    charger, _site_entry, _controller, coordinator, clock, turn_on_calls, _turn_off_calls, site = world
    assert "solar_charging" in _status_codes(hass, charger.entry_id)

    await _car_ends_the_charge(hass, world)

    state = coordinator.state
    assert state is not None
    assert (state.state, state.reason) == ("off", "car_stopped")
    assert state.retry_at is not None
    # The measured current's leftover is no draw of the car's.
    assert state.car_w == 0.0
    snapshot = site.solar_surplus_snapshot[charger.entry_id]
    assert snapshot["state"] == "off" and snapshot["car_w"] == 0.0 and snapshot["retry_at"] is not None
    codes = _status_codes(hass, charger.entry_id)
    assert "solar_charging" not in codes
    assert codes[0] == "solar_car_stopped"

    # Forty minutes of sun later in the field: no start before the back-off (30 minutes) runs out.
    for t in (1900.0, 2500.0, 3500.0):
        clock.value = t
        await tick_site(hass, site)
        assert coordinator.state.state == "off" and coordinator.state.reason == "car_stopped"
    assert len(turn_on_calls) == 1


async def test_a_stopped_car_is_tried_again_after_the_back_off_and_waits_longer_next_time(
    hass: HomeAssistant,
) -> None:
    world = await _started(hass)
    charger, _site_entry, _controller, coordinator, clock, turn_on_calls, turn_off_calls, site = world
    prefix = charger.entry_id
    await _car_ends_the_charge(hass, world)
    retry_at = coordinator.state.retry_at

    # 30 minutes after the end: the back-off is over, the surplus arms and starts after the start delay.
    clock.value = 1760.0 + 1800.0
    await tick_site(hass, site)
    assert coordinator.state.state == "arming"
    clock.value = 1760.0 + 1800.0 + 125.0
    await tick_site(hass, site)
    assert coordinator.state.action == "start"
    assert len(turn_on_calls) == 2

    # The car takes nothing: the connector says SuspendedEV at 0 A with the control on.
    hass.states.async_set(f"switch.{prefix}", "on")
    _connector(hass, "SuspendedEV", 0.0)
    _car_draws(hass, prefix, 0.0)
    started = clock.value
    clock.value = started + 10.0
    await tick_site(hass, site)
    assert coordinator.state.state == "on"
    clock.value = started + 10.0 + CAR_IDLE_S
    await tick_site(hass, site)
    assert (coordinator.state.state, coordinator.state.reason) == ("off", "car_stopped")
    assert len(turn_off_calls) == 1, "solar stops the charge the car does not take"
    # The second wait is twice the first.
    assert coordinator.state.retry_at is not None and retry_at is not None
    hass.states.async_set(f"switch.{prefix}", "off")
    ended_at = clock.value
    clock.value = ended_at + 1800.0 + 200.0
    await tick_site(hass, site)
    clock.value = ended_at + 1800.0 + 400.0
    await tick_site(hass, site)
    assert coordinator.state.reason == "car_stopped" and len(turn_on_calls) == 2


async def test_a_re_plug_ends_the_wait_at_once(hass: HomeAssistant) -> None:
    world = await _started(hass)
    charger, _site_entry, _controller, coordinator, clock, turn_on_calls, _turn_off_calls, site = world
    await _car_ends_the_charge(hass, world)

    clock.value = 2000.0
    _connector(hass, "Available", 0.0)
    await tick_site(hass, site)
    assert coordinator.state.reason != "car_stopped"

    clock.value = 2100.0
    _connector(hass, "Preparing", 0.0)
    await tick_site(hass, site)
    assert coordinator.state.state == "arming"
    clock.value = 2230.0
    await tick_site(hass, site)
    assert coordinator.state.action == "start"
    assert len(turn_on_calls) == 2


async def test_a_car_at_its_own_limit_is_full_and_gets_no_retry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _started(hass)
    charger, _site_entry, controller, coordinator, clock, turn_on_calls, _turn_off_calls, site = world
    plugged = controller.plugged_in_at
    # The car reads 100 % against its own limit of 100 %.
    monkeypatch.setattr(coordinator, "_vehicle_context", lambda: (plugged, 100.0, 100.0, None))
    await _car_ends_the_charge(hass, world)

    assert (coordinator.state.state, coordinator.state.reason) == ("off", "vehicle_full")
    assert coordinator.state.retry_at is None
    assert _status_codes(hass, charger.entry_id)[0] == "solar_vehicle_full"
    for t in (4000.0, 9000.0, 20000.0, 40000.0):
        clock.value = t
        await tick_site(hass, site)
        assert coordinator.state.reason == "vehicle_full"
    assert len(turn_on_calls) == 1

    # The state of charge falls (the car was driven off and back, say, or used its battery): it may charge.
    monkeypatch.setattr(coordinator, "_vehicle_context", lambda: (plugged, 95.0, 100.0, None))
    clock.value = 40100.0
    await tick_site(hass, site)
    assert coordinator.state.state == "arming"


async def test_a_charge_something_else_ended_is_an_ordinary_off(hass: HomeAssistant) -> None:
    """Another integration turns the charge control off while the car draws: no car-ended wait, the
    sun's ordinary rules start it again after `min_off_s`."""
    world = await _started(hass)
    charger, _site_entry, _controller, coordinator, clock, turn_on_calls, _turn_off_calls, site = world
    prefix = charger.entry_id
    clock.value = 1000.0
    hass.states.async_set(f"switch.{prefix}", "off")
    _connector(hass, "SuspendedEVSE", 6.0)
    set_site_power_w(hass, "solar_site", -SUN_W / 3)
    await tick_site(hass, site)
    assert coordinator.state.state in ("off", "arming")
    assert coordinator.state.reason not in ("car_stopped", "vehicle_full")
    assert coordinator.state.car_w == 0.0

    clock.value = 1400.0
    await tick_site(hass, site)
    assert coordinator.state.action == "start"
    assert len(turn_on_calls) == 2


async def test_a_start_the_charger_has_not_taken_yet_ended_nothing(hass: HomeAssistant) -> None:
    """A charge control never seen on after solar's start is not a charge that ended."""
    world = await solar_setup(hass, ocpp_target=(CPID, 1))
    charger, site_entry, _controller, coordinator, clock, _on, _off = world
    site = controller_of(hass, site_entry.entry_id)
    _connector(hass, "Preparing", 0.0)
    _car_draws(hass, charger.entry_id, 0.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert coordinator.state.action == "start"
    clock.value = 200.0
    await tick_site(hass, site)
    assert coordinator.state.state == "on"

