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
    """The charger begins a charge by itself (at plug-in): nothing here sent a Start. Its current is set
    first, as the site reads both together when the charger reports charging."""
    set_charger_delivered_a(hass, prefix, amps)
    hass.states.async_set(f"switch.{prefix}", "on")


def _decisions(coordinator: Any) -> list[tuple[str, str]]:
    """The actions in the solar decision log, with their reasons."""
    return [(entry["action"], entry["reason"]) for entry in coordinator.decision_log if entry["action"] != "hold"]


def _solar_line(hass: HomeAssistant, charger: Any) -> dict[str, Any]:
    """The solar headline the status composes from this charger's solar facts."""
    facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, charger))
    assert facts.solar is not None
    return compose_status(StatusFacts(now=facts.now, strategy=STRATEGY_SOLAR, solar=facts.solar))["lines"][0]


@pytest.mark.parametrize("strategy", [STRATEGY_HYBRID, STRATEGY_SOLAR])
async def test_a_charge_the_charger_began_by_itself_on_the_battery_is_stopped_on_the_first_reading(
    hass: HomeAssistant, strategy: str
) -> None:
    """The owner's facts: the car draws 15.7 A, the battery discharges 8 kW into it, the grid reads ~0. The
    charge never had a surplus: it is stopped on the first reading, with no stop delay to ride out and no
    verification as if it were a start of the sun's."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(
        hass, strategy
    )
    site = controller_of(hass, site_entry.entry_id)
    _battery(hass, -8000.0)
    set_site_power_w(hass, "solar_site", 0.0)
    _self_start(hass, charger.entry_id, FULL_A)

    await tick_site(hass, site)

    assert len(turn_off_calls) == 1
    assert not turn_on_calls
    assert coordinator.state is not None and coordinator.state.state == "off"
    assert coordinator.state.reason == "off_no_surplus"
    assert _decisions(coordinator) == [("take_over", "take_over"), ("stop", "off_no_surplus")]
    if strategy == STRATEGY_SOLAR:
        assert _solar_line(hass, charger)["code"] == "solar_waiting_for_sun"


async def test_an_easee_that_begins_a_charge_at_plug_in_with_no_sun_is_stopped_at_once(hass: HomeAssistant) -> None:
    """The field report (2026-10-04, an Easee, Solar, no surplus, no plan window): the charger starts at once
    at full current and the grid carries it and the house. Stopped on that first reading, not minutes later
    after "surplus fading"."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await solar_setup(
        hass, strategy=STRATEGY_SOLAR
    )
    site = controller_of(hass, site_entry.entry_id)
    clock.value = 3600.0
    set_site_power_w(hass, "solar_site", 16.0 * 230.0 + 150.0)
    _self_start(hass, charger.entry_id, 16.0)

    await tick_site(hass, site)

    assert len(turn_off_calls) == 1
    assert coordinator.state is not None and coordinator.state.state == "off"
    assert coordinator.state.reason == "off_no_surplus"
    assert not turn_on_calls


async def test_a_charge_taken_over_with_a_surplus_is_kept_at_the_minimum_then_verified(hass: HomeAssistant) -> None:
    """With the sun exporting enough for 10 A beside the car, the charge it began by itself is kept: asked
    down to the start minimum and verified there as a start of the sun's is, then regulated to the surplus."""
    charger, site_entry, controller, coordinator, clock, _on, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    _battery(hass, 0.0)
    # The car's 6 A plus 4 A exported: 10 A of sun.
    set_site_power_w(hass, "solar_site", -4.0 * 230.0)
    _self_start(hass, charger.entry_id, 6.0)

    await tick_site(hass, site)

    assert coordinator.state is not None and coordinator.state.state == "on"
    assert coordinator.state.reason == "start_verifying"
    assert controller.requested_current_a == 6

    clock.value = 60.0
    await tick_site(hass, site)
    assert coordinator.state.reason == "start_verifying" and controller.requested_current_a == 6

    clock.value = 125.0
    await tick_site(hass, site)
    assert coordinator.state.state == "on"
    assert controller.requested_current_a == 10
    assert not turn_off_calls


async def test_a_charge_kept_on_the_sun_rides_out_a_cloud_after_its_verification(hass: HomeAssistant) -> None:
    """Once kept, the charge is the sun's like any other: a fading surplus is held for the stop delay
    (`stop_delay_s`, 300 s) before it is stopped, with no `min_on_s` of a start the sun never made."""
    charger, site_entry, controller, coordinator, clock, _on, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    set_site_power_w(hass, "solar_site", -4.0 * 230.0)
    _self_start(hass, charger.entry_id, 6.0)
    await tick_site(hass, site)

    clock.value = 125.0
    set_site_power_w(hass, "solar_site", 2000.0)
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "disarming"
    assert not turn_off_calls

    clock.value = 426.0
    await tick_site(hass, site)
    assert coordinator.state.state == "off" and coordinator.state.reason == "stop_after_delay"
    assert len(turn_off_calls) == 1


async def test_a_stale_site_reading_stops_a_charge_the_charger_began_by_itself(hass: HomeAssistant) -> None:
    """No trustworthy reading of the grid: the safe side of a charge nobody asked for is not charging. The
    status says no reading stopped it."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(
        hass, STRATEGY_SOLAR
    )
    site = controller_of(hass, site_entry.entry_id)
    clock.value = 600.0
    hass.states.async_set("sensor.solar_site_power_l1", "unavailable")
    _self_start(hass, charger.entry_id, FULL_A)

    await tick_site(hass, site)

    assert len(turn_off_calls) == 1
    assert _decisions(coordinator) == [("take_over", "take_over"), ("stop", "no_basis_stopped")]
    # The tick after the stop: still no reading, and the status says so.
    assert coordinator.state is not None and coordinator.state.state == "off"
    assert _solar_line(hass, charger)["code"] == "solar_no_reading_waiting"


async def test_an_unreadable_charger_current_stops_a_charge_the_charger_began_by_itself(hass: HomeAssistant) -> None:
    """Blind (the charger's own current cannot be read): stopped as well, never kept at the minimum."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(
        hass, STRATEGY_SOLAR
    )
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    clock.value = 600.0
    set_site_power_w(hass, "solar_site", -4.0 * 230.0)
    for phase in ("l1", "l2", "l3"):
        hass.states.async_set(f"sensor.{prefix}_{phase}", "unavailable")
    hass.states.async_set(f"switch.{prefix}", "on")

    await tick_site(hass, site)

    assert len(turn_off_calls) == 1
    assert _decisions(coordinator) == [("take_over", "take_over"), ("stop", "no_basis_stopped")]


async def test_a_plan_window_charge_is_never_taken_over(hass: HomeAssistant) -> None:
    """Hybrid with a plan window open: the plan's charge is left alone whatever the sun does."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.spotnav.execution.controller import ChargingPlan

    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    now = dt_util.utcnow()
    await controller.async_install(
        ChargingPlan(
            start=(now - timedelta(minutes=1)).isoformat(),
            end=(now + timedelta(minutes=30)).isoformat(),
            amps=10,
            phases=3,
            auto_identity="takeover-window-test",
        )
    )
    assert controller.plan_window_active_now is True
    _battery(hass, -8000.0)
    set_site_power_w(hass, "solar_site", 0.0)
    _self_start(hass, charger.entry_id, FULL_A)

    for t in (0.0, 130.0, 400.0, 1000.0):
        clock.value = t
        await tick_site(hass, site)

    assert not turn_off_calls
    assert coordinator.state is not None and coordinator.state.held_by_plan is True


async def test_a_later_surplus_starts_the_charge_by_the_usual_rules(hass: HomeAssistant) -> None:
    """After the stop at plug-in the sun comes out: the charge starts as any start of the sun's, after the
    start delay and no sooner than `min_off_s` (300 s) after that stop, at the start minimum."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(
        hass, STRATEGY_SOLAR
    )
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    # The grid carries the car and the house.
    set_site_power_w(hass, "solar_site", FULL_A * 230.0 + 200.0)
    _self_start(hass, prefix, FULL_A)
    await tick_site(hass, site)
    assert len(turn_off_calls) == 1
    # The charger obeys.
    set_charger_delivered_a(hass, prefix, 0.0)
    hass.states.async_set(f"switch.{prefix}", "off")

    set_site_power_w(hass, "solar_site", -3000.0)
    for t in range(10, 300, 30):
        clock.value = float(t)
        await tick_site(hass, site)
    assert not turn_on_calls, "no start inside min_off_s of the stop"

    for t in range(300, 460, 30):
        clock.value = float(t)
        await tick_site(hass, site)
    assert len(turn_on_calls) == 1
    assert controller.charge_origin == "solar"


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
    set_site_power_w(hass, "solar_site", -4.0 * 230.0)
    _self_start(hass, charger.entry_id, 6.0)
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
    assert coordinator.state is not None and coordinator.state.reason == "paused"
    facts = dashboard_api.status_facts(dashboard_api.capture_dashboard(hass, charger))
    assert (facts.paused, facts.pause_choice, facts.pause_action) == (True, "manual", "stop")
    # The pause takes the headline, worded by the clients from the same facts.
    lines = compose_status(
        StatusFacts(now=facts.now, strategy=STRATEGY_HYBRID, hybrid=HybridFacts(), paused=True,
                    pause_choice="manual", pause_action="stop", pause_scope="plug_in")
    )["lines"]
    assert lines[0] == {
        "code": "paused", "params": {"until": None, "choice": "manual", "action": "stop", "ends": "unplug"}
    }

    # The person starts the charge: their Start is theirs, and stays so (Auto is still paused for them).
    await executor.async_manual_start()
    assert executor.pause_intent.action == "start"


async def test_a_persons_stop_ends_with_the_plug_in(hass: HomeAssistant) -> None:
    charger, site_entry, controller, coordinator, clock, turn_on_calls, _off = await _setup(hass)
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    connected: list[bool | None] = [True]
    controller.adapter.vehicle_connected = lambda: connected[0]  # type: ignore[method-assign]
    controller._observe_connection()  # noqa: SLF001 - the observation point under test
    await executor.async_manual_stop()
    assert executor.pause_intent.choice == "manual"

    connected[0] = False
    controller._observe_connection()  # noqa: SLF001
    await hass.async_block_till_done()

    assert not executor.pause_intent.admitted


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


async def _restart(hass: HomeAssistant, charger: Any) -> Any:
    """Reload the charger entry, as a restart does: the controller and the solar coordinator are rebuilt
    from what was saved."""
    assert await hass.config_entries.async_reload(charger.entry_id)
    await hass.async_block_till_done()
    return controller_of(hass, charger.entry_id)


async def test_a_persons_stop_survives_a_restart(hass: HomeAssistant) -> None:
    charger, site_entry, controller, coordinator, clock, turn_on_calls, _off = await _setup(hass)
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    await executor.async_manual_stop()
    assert executor.pause_intent.choice == "manual"

    controller = await _restart(hass, charger)
    assert executor_for(hass, charger.entry_id).pause_intent.choice == "manual"
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    coordinator._now = clock.now  # noqa: SLF001 - the fake clock, as `solar_setup` installs it
    coordinator.async_start()
    site = controller_of(hass, site_entry.entry_id)
    _battery(hass, 8000.0)
    set_site_power_w(hass, "solar_site", 0.0)
    turn_on_calls.clear()
    for t in range(0, 1200, 30):
        clock.value = float(t)
        await tick_site(hass, site)
    assert not turn_on_calls
    assert coordinator.state is not None and coordinator.state.reason == "paused"


async def test_a_battery_credit_back_off_survives_a_restart(hass: HomeAssistant) -> None:
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await _setup(hass)
    site = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    _battery(hass, 8000.0)
    set_site_power_w(hass, "solar_site", 0.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    hass.states.async_set(f"switch.{prefix}", "on")
    clock.value = 160.0
    set_charger_delivered_a(hass, prefix, 6.0)
    _battery(hass, -6.0 * 3 * 230.0)
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.reason == "battery_credit_false"
    hass.states.async_set(f"switch.{prefix}", "off")
    set_charger_delivered_a(hass, prefix, 0.0)
    until, next_s = controller.solar_credit_backoff
    assert until is not None and next_s == 1200.0

    controller = await _restart(hass, charger)
    assert controller.solar_credit_backoff[0] == until
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    clock.value = 0.0
    coordinator._now = clock.now  # noqa: SLF001
    coordinator.async_start()
    _battery(hass, 8000.0)
    turn_on_calls.clear()
    # Well inside the ten minutes left of it, a charging battery is still not counted: nothing starts.
    for t in range(0, 400, 30):
        clock.value = float(t)
        await tick_site(hass, site)
    assert not turn_on_calls
    assert coordinator.state is not None and coordinator.state.priority_effective == "battery_first"


async def test_a_restart_finding_a_charge_the_charger_began_by_itself_decides_it_on_the_first_reading(
    hass: HomeAssistant,
) -> None:
    """HA restarts while a charge the charger began by itself runs (nothing saved says anyone started it) and
    the sun gives nothing: it is not adopted as a charge of the sun's (which would hold it for `min_on_s`
    and the stop delay) but stopped on the first reading."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = await solar_setup(
        hass, charging_at_setup=True
    )

    assert _decisions(coordinator) == [("take_over", "take_over"), ("stop", "off_no_surplus")]
    assert coordinator.state is not None and coordinator.state.state == "off"


async def test_a_kept_charge_is_decided_again_after_a_restart_and_kept_while_the_sun_covers_it(
    hass: HomeAssistant,
) -> None:
    charger, site_entry, controller, coordinator, clock, _on, turn_off_calls = await _setup(hass, STRATEGY_SOLAR)
    site = controller_of(hass, site_entry.entry_id)
    set_site_power_w(hass, "solar_site", -4.0 * 230.0)
    _self_start(hass, charger.entry_id, 6.0)
    await tick_site(hass, site)
    clock.value = 125.0
    await tick_site(hass, site)
    assert coordinator.state is not None and coordinator.state.state == "on"

    controller = await _restart(hass, charger)
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    coordinator._now = clock.now  # noqa: SLF001 - the fake clock, as `solar_setup` installs it
    coordinator.async_start()
    await hass.async_block_till_done()
    for t in (130.0, 200.0, 400.0):
        clock.value = t
        await tick_site(hass, site)

    assert not turn_off_calls, "a charge the sun covers is not cycled by a restart"
    assert coordinator.state is not None and coordinator.state.state == "on"


async def test_after_a_restart_a_missing_reading_is_waited_for_briefly_then_the_charge_is_stopped(
    hass: HomeAssistant,
) -> None:
    """Right after a restart the readings may not be back yet: a charge the charger began by itself waits for
    the first usable one for `MEASUREMENT_WARNING_GRACE_S` (120 s), unchanged, and is stopped once that
    passes with none."""
    charger, site_entry, controller, coordinator, clock, _on, turn_off_calls = await _setup(hass, STRATEGY_SOLAR)
    site = controller_of(hass, site_entry.entry_id)
    hass.states.async_set("sensor.solar_site_power_l1", "unavailable")
    _self_start(hass, charger.entry_id, FULL_A)
    controller = await _restart(hass, charger)
    turn_off_calls.clear()
    coordinator = hass.config_entries.async_get_entry(charger.entry_id).runtime_data.solar
    clock.value = 1000.0
    coordinator._now = clock.now  # noqa: SLF001 - the fake clock, as `solar_setup` installs it
    coordinator._first_evaluated_at = None  # noqa: SLF001 - the restart's first evaluation is on this clock
    coordinator.async_start()
    await hass.async_block_till_done()

    for t in (1000.0, 1060.0, 1110.0):
        clock.value = t
        await tick_site(hass, site)
    assert not turn_off_calls
    assert coordinator.state is not None and coordinator.state.reason == "no_basis_off"

    clock.value = 1125.0
    await tick_site(hass, site)
    assert len(turn_off_calls) == 1
    assert _decisions(coordinator)[-1] == ("stop", "no_basis_stopped")
