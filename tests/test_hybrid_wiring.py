"""Hybrid arbitration and window-end handoff.

Arbitration: while an installed Auto plan has a window active now, the solar coordinator keeps
observing but issues no start, stop or `set_current`, recording `held_by_plan`. Handoff:
`ChargingController.end_window_guard`, when it returns `True` (coordinator state `on` or
`disarming`), skips the stop at window end and the charge continues; solar takes over on its next
tick because `plan_window_active_now` has gone `False`.

Built on `test_solar_execution.py`'s `solar_setup`/`tick_site`/`arm_and_start`. Auto-shaped
`ChargingPlan`s are installed directly (price planning is covered by `test_hybrid_replay.py`), and
window ends are fired with `async_fire_time_changed` against the real `ChargingController`
scheduling.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.spotnav.planning.auto_settings import STRATEGY_HYBRID
from custom_components.spotnav.execution.controller import ChargingController, ChargingPlan

from .world import arm_and_start, solar_setup, tick_site
from .world import controller_of
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import preview_for

pytestmark = pytest.mark.usefixtures("offline_relay")


async def _install_active_window(
    controller: ChargingController, *, minutes_left: float, amps: int = 10
) -> ChargingPlan:
    """An Auto-shaped plan with one window that started a minute ago and ends `minutes_left`
    from now -- installed directly through the real `ChargingController.async_install`, never
    through the price planner, so these tests isolate the arbitration/handoff mechanics from
    the pure planning core `tests/test_hybrid_plan.py` and the replay `tests/test_hybrid_replay.py`
    already cover.
    """
    now = dt_util.utcnow()
    plan = ChargingPlan(
        start=(now - timedelta(minutes=1)).isoformat(),
        end=(now + timedelta(minutes=minutes_left)).isoformat(),
        amps=amps,
        phases=3,
        auto_identity="hybrid-wiring-test",
    )
    await controller.async_install(plan)
    return plan


async def test_solar_coordinator_issues_nothing_during_an_active_plan_window(
    hass: HomeAssistant,
) -> None:
    """The design's own arbitration: a window active now means the plan owns the charger at
    full current, and solar's verdict -- computed, never applied -- is recorded as
    `held_by_plan`."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    await _install_active_window(controller, minutes_left=10)
    assert controller.plan_window_active_now is True
    turn_on_calls.clear()
    turn_off_calls.clear()

    # Drive solar's own surplus up -- under plain `solar` this would start (and keep
    # modulating) the charger; under `hybrid`, while the window is active, it must not.
    await arm_and_start(hass, site_controller, clock)

    assert coordinator.state is not None
    assert coordinator.state.state == "on"
    assert coordinator.state.held_by_plan is True
    assert turn_on_calls == [], "no start reached the charger while the plan owns it"
    assert turn_off_calls == [], "and no stop either"


async def test_solar_coordinator_acts_normally_once_no_window_is_active(
    hass: HomeAssistant,
) -> None:
    """The same charger, the same rising surplus, but no installed plan at all: `hybrid` with
    nothing to hold it back behaves exactly like `solar` -- the coordinator's verdict reaches
    the charger."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    assert controller.plan_window_active_now is False

    await arm_and_start(hass, site_controller, clock)

    assert coordinator.state is not None
    assert coordinator.state.state == "on"
    assert coordinator.state.held_by_plan is False
    assert len(turn_on_calls) == 1, "solar itself started the charger"


async def test_window_end_with_sun_continues_charging_without_a_gap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window-end handoff: the sun is available (the coordinator's own state is `on`) right
    as the window ends, so the scheduled stop is skipped entirely and the charge continues --
    solar then takes over, unheld, on its very next tick.

    This is the one test the task asks to be confirmed against the *unpatched* end callback:
    with `end_window_guard` removed (or always returning `False`), `_async_end_callback`/
    `_async_final_end_callback` call `async_stop` unconditionally, `turn_off_calls` would be
    non-empty, and this assertion would fail. Verified by hand during development by
    temporarily short-circuiting `ChargingController._async_end_callback`/
    `_async_final_end_callback` to always proceed to `async_stop` -- this test failed exactly
    as expected, then passed again once the guard was restored.
    """
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    plan = await _install_active_window(controller, minutes_left=1)
    hass.states.async_set(f"switch.{charger.entry_id}", "on")
    turn_on_calls.clear()
    turn_off_calls.clear()

    await arm_and_start(hass, site_controller, clock)
    assert coordinator.state is not None
    assert coordinator.state.state == "on" and coordinator.state.held_by_plan is True

    after_window = plan.end_time + timedelta(seconds=1)
    async_fire_time_changed(hass, after_window)
    await hass.async_block_till_done()

    assert turn_off_calls == [], "the charge continues without a gap"

    # `async_fire_time_changed` fires the scheduled callback without moving Home
    # Assistant's own clock -- `dt_util.utcnow()` is pinned here, exactly like
    # `test_auto_execution.py`'s own `frozen_real_clock` fixture, to make
    # `plan_window_active_now` (and the next tick's own `held_by_plan`) see the window as
    # genuinely past, the way a real clock reaching this instant would.
    monkeypatch.setattr(dt_util, "utcnow", lambda: after_window)
    assert controller.plan_window_active_now is False

    # Solar owns the charger again on its next tick.
    await tick_site(hass, site_controller)
    assert coordinator.state is not None and coordinator.state.held_by_plan is False


async def test_window_end_without_sun_stops_as_today(hass: HomeAssistant) -> None:
    """No sun (the coordinator's own state stays `off`) at the window's end: the guard reads
    `False`, and the window ends exactly as it always has."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    plan = await _install_active_window(controller, minutes_left=1)
    hass.states.async_set(f"switch.{charger.entry_id}", "on")
    turn_off_calls.clear()

    assert coordinator.state is not None and coordinator.state.state == "off"

    async_fire_time_changed(hass, plan.end_time + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert len(turn_off_calls) == 1, "the window ends exactly as it does without hybrid"


async def test_manual_stop_during_a_hybrid_solar_charge_is_respected(
    hass: HomeAssistant,
) -> None:
    """A manual stop never goes through window end, so it is never subject to the guard --
    the design's own "must stay exactly as it is"."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    await _install_active_window(controller, minutes_left=30)
    hass.states.async_set(f"switch.{charger.entry_id}", "on")
    await arm_and_start(hass, site_controller, clock)
    assert coordinator.state is not None and coordinator.state.held_by_plan is True
    turn_off_calls.clear()

    await controller.async_stop()

    assert len(turn_off_calls) == 1, "a person's own Stop is always respected"


# ------------------------------------------------------- hybrid must stop at target


async def test_a_satisfied_hybrid_never_starts_on_surplus(hass: HomeAssistant) -> None:
    """Once the remaining need is `<= 0`
    (`plan_hybrid`'s own `satisfied` state), the coordinator must not start a charge for
    surplus that arrives afterwards -- continuing is what plain `solar` is for; here it would
    take `car_first` sun away from the house battery for a car that needs nothing more.
    """
    from custom_components.spotnav.planning.auto_settings import EnergyBaseline
    from custom_components.spotnav.execution.hybrid_execution import hybrid_state
    from .world import go_auto
    from .world import set_charger_delivered_a, set_derived_site_entities

    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    site_controller = controller_of(hass, site_entry.entry_id)

    # The register already shows the full request delivered, *and* the baseline is pre-seeded
    # at zero for the `no_deadline` epoch `departure_enabled=False` uses -- so the very first
    # compute already sees the target met, and no grid plan is ever installed to begin with
    # (this test is about solar's own behaviour once satisfied, not about the grid plan).
    register = "sensor.solar_charger_energy_register"
    hass.states.async_set(
        register, "5.0",
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )
    controller.energy_register_entity_id = register
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        charger.entry_id,
        energy_baseline=EnergyBaseline(register_kwh=0.0, departure_key="no_deadline"),
    )

    snapshot = await go_auto(
        hass, charger.entry_id, strategy=STRATEGY_HYBRID, area_id="SE4", amps=10, phases=3,
        requested_kwh=5.0, departure_enabled=False,
    )
    assert snapshot.state == "nothing_to_charge" and snapshot.reason == "already_at_target"

    state = hybrid_state(hass, charger.entry_id)
    assert state is not None and state.state == "satisfied"
    assert turn_on_calls == [], "no grid plan either -- the target was already met"

    # Surplus arrives -- under an unsatisfied hybrid charger this would start the charger.
    set_derived_site_entities(hass, "solar_site", -1400.0)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    await tick_site(hass, site_controller)
    clock.value = 125.0
    await tick_site(hass, site_controller)

    assert turn_on_calls == [], "a satisfied hybrid charger never starts on surplus"
    assert coordinator.state is not None and coordinator.state.satisfied is True


async def test_a_satisfied_hybrid_stops_a_charge_it_started(hass: HomeAssistant) -> None:
    """The other half: a charge solar already started, before the register caught up with
    the target, is stopped once it does -- never left running with the target already met.
    """
    from custom_components.spotnav.planning.auto_settings import EnergyBaseline
    from .world import go_auto
    from .world import arm_and_start

    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    site_controller = controller_of(hass, site_entry.entry_id)

    # Pre-seed the baseline at zero for the same `no_deadline` epoch, but the register itself
    # still reads zero too -- the target is *not* met yet, so the first compute installs
    # nothing and solar is free to start on surplus, exactly like an ordinary hybrid charger.
    register = "sensor.solar_charger_energy_register"
    hass.states.async_set(
        register, "0.0",
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )
    controller.energy_register_entity_id = register
    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(
        charger.entry_id,
        energy_baseline=EnergyBaseline(register_kwh=0.0, departure_key="no_deadline"),
    )

    await go_auto(
        hass, charger.entry_id, strategy=STRATEGY_HYBRID, area_id="SE4", amps=10, phases=3,
        requested_kwh=5.0, departure_enabled=False,
    )
    # Whatever grid plan the (still partly unmet) need produced is beside this test's own
    # point -- cleared explicitly so `held_by_plan` cannot suppress solar's own start below,
    # which is what this test is actually about.
    await controller.async_stop(clear_schedule=True)
    turn_on_calls.clear()
    turn_off_calls.clear()
    assert controller.plan_window_active_now is False
    await arm_and_start(hass, site_controller, clock)
    assert coordinator.state is not None and coordinator.state.state == "on"
    assert len(turn_on_calls) == 1
    hass.states.async_set(f"switch.{charger.entry_id}", "on")

    # The register now shows the full request delivered -- the target is met while solar is
    # still running the charge it started.
    hass.states.async_set(
        register, "5.0",
        {"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    )
    # `hybrid_state` is one tick behind by design: this first tick is the one whose own
    # periodic preview recompute *records* satisfaction; only the next tick reads that
    # freshly-recorded state at its own start and acts on it by stopping the charge it started.
    await tick_site(hass, site_controller)
    await tick_site(hass, site_controller)

    assert len(turn_off_calls) == 1, "solar stops the charge it started once the target is met"
    assert coordinator.state is not None and coordinator.state.satisfied is True


async def test_a_satisfied_target_soc_hybrid_never_starts_on_surplus(hass: HomeAssistant) -> None:
    """The same rule for `target_soc`: `_energy_for`'s own "already at target" answer records
    the identical `satisfied` result, through live state of charge rather than a register."""
    from dataclasses import replace

    from custom_components.spotnav.planning.auto_controller import LiveVehicleFacts
    from custom_components.spotnav.planning.auto_settings import DRIVER_TARGET_SOC, TargetSocIntent
    from custom_components.spotnav.execution.hybrid_execution import hybrid_state
    from .world import set_charger_delivered_a, set_derived_site_entities

    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, strategy=STRATEGY_HYBRID)
    )
    site_controller = controller_of(hass, site_entry.entry_id)

    preview = preview_for(hass, charger.entry_id)
    assert preview is not None
    preview._vehicle_reader = lambda _vehicle_id: LiveVehicleFacts(
        vehicle_id="car-1", soc_percent=80.0, reported_capacity_kwh=60.0, max_percent=100.0
    )

    snapshot = await preview.async_apply_settings(
        mutate=lambda s: replace(
            s, area_id="SE4", amps=10, phases=3, departure_enabled=False,
            driver=DRIVER_TARGET_SOC,
            target=TargetSocIntent(vehicle_id="car-1", target_percent=80.0),
        )
    )
    assert snapshot.state == "nothing_to_charge" and snapshot.reason == "already_at_target"

    state = hybrid_state(hass, charger.entry_id)
    assert state is not None and state.state == "satisfied"

    set_derived_site_entities(hass, "solar_site", -1400.0)
    set_charger_delivered_a(hass, charger.entry_id, 0.0)
    await tick_site(hass, site_controller)
    clock.value = 125.0
    await tick_site(hass, site_controller)

    assert turn_on_calls == [], "a satisfied target_soc hybrid charger never starts on surplus"
