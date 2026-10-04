"""Solar surplus execution through the site controller.

* Starts and stops reach the charger only through `AutoExecutor`'s per-charger lock and
  `ChargingController.async_start`/`async_stop`.
* Modulation (`set_current`) only records a requested current and never writes the charger.
* Pause and a manual Stop block solar exactly as they block price execution.
* A charger already charging under `solar` at restart is adopted as running, not stopped.
* A full replay over derived-mode sensor states: rising surplus starts, a cloud shorter than
  `stop_delay_s` does not stop, a lasting drop does.

No test sleeps: the coordinator's clock (`coordinator._now`) is a hand-advanced fake and each tick
calls `SiteCapacityController._recompute()` directly, as `tests/test_yield_stepping_wiring.py` does,
because exact observation sequences cannot come from a real timer.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.planning.auto_settings import STRATEGY_CHEAPEST, STRATEGY_SOLAR
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController
from custom_components.spotnav.execution.solar_execution import (
    solar_execution_state,
)

from .helpers import make_entry, make_site_entry
from .world import set_charger_delivered_a, set_site_power_w
from .world import arm_and_start, solar_setup, tick_site
from .world import controller_of
from custom_components.spotnav.runtime import domain_data
from custom_components.spotnav.runtime import executor_for

pytestmark = pytest.mark.usefixtures("offline_relay")


MEASUREMENT_MODE_DERIVED = "derived_phase_current"


async def test_surplus_rises_starts_a_short_cloud_survives_a_lasting_drop_stops(
    hass: HomeAssistant,
) -> None:
    """End-to-end replay: rising surplus starts the charger at its three-phase minimum (6 A, ~4.1 kW), a cloud shorter than `stop_delay_s` (300 s) does not stop it, and a lasting drop does once `stop_delay_s` and `min_on_s` (600 s) have elapsed."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    prefix = f"{charger.entry_id}"

    # t=0: strong export (1400 W/phase = 4200 W net), enough to arm, but `start_delay_s` (120 s) has not elapsed.
    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site_controller)
    assert coordinator.state is not None
    assert coordinator.state.state == "arming"
    assert not turn_on_calls

    # t=125: still exporting and `start_delay_s` has elapsed: starts.
    clock.value = 125.0
    await tick_site(hass, site_controller)
    assert coordinator.state.state == "on"
    assert coordinator.state.action == "start"
    assert len(turn_on_calls) == 1
    # The mocked `switch.turn_on` does not flip the entity's state; simulate the device confirming, as `_yield_setup` does.
    hass.states.async_set(f"switch.{prefix}", "on")

    # t=130: the car draws what it was started at and the export shrinks by that amount (energy-balance identity), so this must not look like the surplus vanished.
    clock.value = 130.0
    set_charger_delivered_a(hass, prefix, 6.0)
    set_site_power_w(hass, "solar_site", -20.0)
    await tick_site(hass, site_controller)
    assert coordinator.state.state == "on"
    assert coordinator.state.action in ("hold", "set_current")
    assert not turn_off_calls  # the car starting itself never stops the charge

    # t=200: a passing cloud drops surplus well below `stop_a` (5 A).
    clock.value = 200.0
    set_site_power_w(hass, "solar_site", 1000.0)  # 3000 W net import
    await tick_site(hass, site_controller)
    assert coordinator.state.state == "disarming"
    assert not turn_off_calls

    # t=260: the cloud passes 60 s later, inside `stop_delay_s` (300 s): recovers to `on` without stopping.
    clock.value = 260.0
    set_site_power_w(hass, "solar_site", -20.0)
    await tick_site(hass, site_controller)
    assert coordinator.state.state == "on"
    assert not turn_off_calls

    # t=900: a lasting drop; disarming begins fresh.
    clock.value = 900.0
    set_site_power_w(hass, "solar_site", 1000.0)
    await tick_site(hass, site_controller)
    assert coordinator.state.state == "disarming"
    assert not turn_off_calls

    # t=1150: `stop_delay_s` has not elapsed since t=900: still held.
    clock.value = 1150.0
    await tick_site(hass, site_controller)
    assert coordinator.state.state == "disarming"
    assert not turn_off_calls

    # t=1250: 350 s since the drop began (>= stop_delay_s) and 1125 s since the start (>= min_on_s): stops.
    clock.value = 1250.0
    await tick_site(hass, site_controller)
    assert coordinator.state.state == "off"
    assert coordinator.state.action == "stop"
    assert len(turn_off_calls) == 1


async def test_solar_start_and_stop_reach_the_charger_only_through_the_executor_lock(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every `async_start`/`async_stop` this replay causes happens while `AutoExecutor`'s per-charger lock is held: there is no second path from the solar coordinator to the charger."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None

    lock_states: list[tuple[str, bool]] = []
    orig_start = ChargingController.async_start
    orig_stop = ChargingController.async_stop

    async def spy_start(self, *args, **kwargs):
        if self is controller:
            lock_states.append(("start", executor._lock.locked()))
        return await orig_start(self, *args, **kwargs)

    async def spy_stop(self, *args, **kwargs):
        if self is controller:
            lock_states.append(("stop", executor._lock.locked()))
        return await orig_stop(self, *args, **kwargs)

    monkeypatch.setattr(ChargingController, "async_start", spy_start)
    monkeypatch.setattr(ChargingController, "async_stop", spy_stop)

    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site_controller)
    clock.value = 125.0
    await tick_site(hass, site_controller)  # starts

    clock.value = 900.0
    set_site_power_w(hass, "solar_site", 1000.0)
    await tick_site(hass, site_controller)
    clock.value = 1250.0
    await tick_site(hass, site_controller)  # stops

    assert lock_states, "the spy never observed a start/stop -- the replay above is broken"
    assert all(locked for _action, locked in lock_states), (
        f"a charger call happened without the executor's lock held: {lock_states}"
    )
    assert {"start", "stop"} <= {action for action, _locked in lock_states}


async def test_set_current_never_calls_the_chargers_write_path_directly(
    hass: HomeAssistant,
) -> None:
    """`async_set_requested_current` only records the request; unlike `async_start` it never calls the charge-control switch or the OCPP `ChangeConfiguration` write."""
    hass.states.async_set("switch.set_current_charger", "on")
    turn_on_calls = async_mock_service(hass, "switch", "turn_on")
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    entry = make_entry(
        hass,
        entry_id="set_current_charger",
        charge_control="switch.set_current_charger",
        current_limit=None,
        webhook_id="webhook-set-current",
        title="Set current charger",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    controller: ChargingController = controller_of(hass, entry.entry_id)

    await controller.async_set_requested_current(11)

    assert controller.requested_current_a == 11
    assert not turn_on_calls
    assert not turn_off_calls
    assert not configure_calls


async def test_pause_blocks_solar_from_starting(hass: HomeAssistant) -> None:
    """Pause is the persisted gate every application checks (`pause_blocks_execution`): admitted before surplus arrives, it keeps the charger off."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    await executor.async_pause()

    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site_controller)
    clock.value = 125.0
    await tick_site(hass, site_controller)

    # The pure controller's verdict still says "start" (pause is an execution-layer gate), but nothing reached the charger.
    assert coordinator.state is not None
    assert coordinator.state.action == "start"
    assert not turn_on_calls
    assert controller.charging is False


async def test_a_manual_stop_is_respected_like_a_price_executions_own(
    hass: HomeAssistant,
) -> None:
    """A person's Stop goes through `AutoExecutor.async_manual_stop` -> `_immediate_stop_locked`: invalidate the attempt, stop the charger, publish, with no pause and no authority change.

    Solar's start/stop share that lock, so they never interleave with a manual stop. The pure
    `SolarController` re-issues `"start"` only on an `off`/`arming` -> `on` transition, so a manual
    stop holds until surplus falls and rearms, as with price execution.
    """
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    prefix = charger.entry_id

    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site_controller)
    clock.value = 125.0
    await tick_site(hass, site_controller)
    assert len(turn_on_calls) == 1
    hass.states.async_set(f"switch.{prefix}", "on")

    await executor.async_manual_stop()
    assert len(turn_off_calls) == 1
    hass.states.async_set(f"switch.{prefix}", "off")

    # Surplus stays ample: if solar overrode the manual stop, this would start again.
    clock.value = 200.0
    set_charger_delivered_a(hass, prefix, 6.0)
    set_site_power_w(hass, "solar_site", -20.0)
    await tick_site(hass, site_controller)

    assert len(turn_on_calls) == 1, "the manual stop was overridden -- it must not be"
    assert controller.charging is False


async def test_restart_adoption_does_not_cycle_the_contactor_and_still_stops_normally(
    hass: HomeAssistant,
) -> None:
    """A charger found already charging under `solar` at start-up (its saved origin says solar started it) is adopted as `on` rather than left to rediscover that over `start_delay_s`.

    Proves no immediate stop (the contactor is not cycled) despite no surplus at all, and that the
    state machine does stop it once `stop_delay_s`/`min_on_s` are satisfied.
    """
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass, charging_at_setup=True, charge_origin_at_setup="solar")
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    set_charger_delivered_a(hass, prefix, 6.0)

    # No surplus at all: strong import, nothing exported.
    set_site_power_w(hass, "solar_site", 2000.0)
    await tick_site(hass, site_controller)

    assert coordinator.state is not None
    assert coordinator.state.state in ("disarming", "on")
    assert not turn_off_calls, "adoption must not cycle the contactor immediately"
    assert not turn_on_calls, "adoption must never issue a redundant start either"

    # Advance well past both `stop_delay_s` (300 s) and `min_on_s` (600 s) from adoption.
    clock.value = 700.0
    await tick_site(hass, site_controller)

    assert coordinator.state.state == "off"
    assert coordinator.state.action == "stop"
    assert len(turn_off_calls) == 1


async def test_solar_execution_unavailable_no_longer_appears_when_solar_can_run(
    hass: HomeAssistant,
) -> None:
    """Once a charger's site can run solar (derived-measurement mode), `AutoPlannerController`'s snapshot no longer reports `solar_execution_unavailable`; that reason is for a charger whose site structurally cannot run solar."""
    from .world import go_auto, setup_charger

    entry = await setup_charger(hass, entry_id="solar_avail_charger", charge_control="switch.solar_avail")
    site_entry = make_site_entry(
        hass,
        entry_id="solar_avail_site",
        charger_entry_ids=[entry.entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={},
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()

    snapshot = await go_auto(hass, entry.entry_id, strategy=STRATEGY_SOLAR)

    assert snapshot.state == "planning_unavailable"
    assert snapshot.reason == "solar_running"
    assert snapshot.reason != "solar_execution_unavailable"


async def test_solar_execution_unavailable_still_appears_with_no_site(hass: HomeAssistant) -> None:
    """A solar-strategy charger with no site at all can never run solar and keeps the `solar_execution_unavailable` reason."""
    from .world import go_auto, setup_charger

    entry = await setup_charger(hass, entry_id="solar_no_site_charger", charge_control="switch.solar_no_site")

    snapshot = await go_auto(hass, entry.entry_id, strategy=STRATEGY_SOLAR)

    assert snapshot.reason == "solar_execution_unavailable"


async def test_solar_execution_state_reports_the_verdicts_breakdown(hass: HomeAssistant) -> None:
    """`solar_execution_state`, what a status reader shows, carries the solar controller's state, last action/reason and surplus breakdown, live."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)

    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site_controller)

    state = solar_execution_state(hass, charger.entry_id)
    assert state is not None
    assert state.state == "arming"
    assert state.reason == "arming_delay"
    assert state.available_a is not None and state.available_a >= 6.0
    assert state.net_grid_w == pytest.approx(-4200.0)
    assert state.active_control_active is False


def test_liveness_gated_accepts_fresh_and_confirmed_unchanged_rejects_the_rest() -> None:
    """`_liveness_gated` accepts exactly `fresh` and `confirmed_unchanged` and rejects `unconfirmed_stale`/`no_recent_report`/unknown (`None`), per phase, as `phase_liveness` does."""
    from custom_components.spotnav.execution.solar_execution import _liveness_gated

    liveness = {
        "L1": "fresh",
        "L2": "confirmed_unchanged",
        "L3": "unconfirmed_stale",
    }
    diagnostic = {"L1": 100.0, "L2": 200.0, "L3": 300.0}

    assert _liveness_gated(liveness, diagnostic, "L1") == 100.0
    assert _liveness_gated(liveness, diagnostic, "L2") == 200.0
    assert _liveness_gated(liveness, diagnostic, "L3") is None

    no_recent = {"L1": "no_recent_report"}
    assert _liveness_gated(no_recent, {"L1": 1.0}, "L1") is None
    unknown = {"L1": None}
    assert _liveness_gated(unknown, {"L1": 1.0}, "L1") is None


def test_build_observation_uses_confirmed_unchanged_grid_power_the_strict_field_would_drop(
    hass: HomeAssistant,
) -> None:
    """L1/L3 `confirmed_unchanged` at 240 s while the protection layer's strict field reads `None`: this adapter still uses the reading, and `SiteCapacityResult.phase_signed_active_power_w` is unchanged for the same input."""
    from custom_components.spotnav.site.site_capacity import (
        DerivedPhaseInput,
        DerivedPhaseMeasurement,
        PhaseValue,
        SiteCalculationConfig,
        calculate_site_capacity,
    )
    from custom_components.spotnav.execution.solar_execution import _build_observation

    max_age_s = 120.0

    def stale_but_confirmed(watts: float) -> DerivedPhaseInput:
        # Raw age well past `max_age_s` but a fresh report: `confirmed_unchanged`.
        return DerivedPhaseInput(
            active_power_w=PhaseValue(watts, max_age_s + 60.0, report_age_s=5.0),
            reactive_power_var=PhaseValue(0.0, max_age_s + 60.0, report_age_s=5.0),
            voltage_v=PhaseValue(230.0, max_age_s + 60.0, report_age_s=5.0),
        )

    derived = DerivedPhaseMeasurement(
        l1=stale_but_confirmed(-1400.0),
        l2=stale_but_confirmed(-1400.0),
        l3=stale_but_confirmed(-1400.0),
    )
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=25.0,
        safety_margin_a=1.0,
        measurement_mode="derived_phase_current",
        max_age_s=max_age_s,
        derived=derived,
    )
    result = calculate_site_capacity(config, [])
    assert all(liveness == "confirmed_unchanged" for liveness in result.phase_liveness.values())
    # The protection-layer field is still strictly age-gated, still `None` here.
    assert result.phase_signed_active_power_w == {"L1": None, "L2": None, "L3": None}

    class _FakeSite:
        def __init__(self) -> None:
            self.result = result
            self.config: dict = {"phase_wiring": {}}

        def charger_measured_current(self, _charger_entry_id: str):
            return None

        def battery_aggregate_power(self):
            return None

    observation = _build_observation(_FakeSite(), "some_charger", now=0.0)

    # The adapter used the diagnostic field, gated by liveness, never the strict (here `None`) field.
    assert observation.signed_grid_w == {"L1": -1400.0, "L2": -1400.0, "L3": -1400.0}
    assert observation.voltage_v["L1"] == 230.0


async def test_solar_surplus_attribute_present_and_disabled_for_a_cheapest_charger(
    hass: HomeAssistant,
) -> None:
    """The `solar_surplus` attribute exists for every associated charger even when not on the `solar` strategy, with `enabled: False` and "nothing observed yet" defaults, never a missing key."""
    from .world import setup_charger

    entry = await setup_charger(
        hass, entry_id="attr_disabled_charger", charge_control="switch.attr_disabled"
    )
    site_entry = make_site_entry(
        hass,
        entry_id="attr_disabled_site",
        charger_entry_ids=[entry.entry_id],
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities={},
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    site_controller: SiteCapacityController = controller_of(hass, site_entry.entry_id)

    state = hass.states.get("sensor.site_capacity_state")
    assert state is not None
    snapshot = state.attributes["solar_surplus"]
    assert snapshot[entry.entry_id] == {
        "enabled": False,
        "state": "unknown",
        "action": None,
        "reason": None,
        "requested_a": None,
        "available_w": None,
        "available_a": None,
        "net_grid_w": None,
        "car_w": None,
        "battery_w": None,
        "export_w": None,
        "priority_effective": None,
        "basis_problem": None,
        "basis_entity": None,
        "charger_current": None,
        "charger_current_entity": None,
        "site_incomplete_phases": [],
        "retry_at": None,
    }
    assert site_controller.solar_surplus_snapshot == snapshot


async def test_solar_surplus_attribute_reflects_the_live_verdict_when_enabled(
    hass: HomeAssistant,
) -> None:
    """The enabled, live half of the same attribute: why a charger is or is not solar-charging."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)

    set_site_power_w(hass, "solar_site", -1400.0)
    await tick_site(hass, site_controller)
    # The sensor platform only republishes on the site controller's `_notify()`, which `tick_site`'s `_recompute()` triggers, but a state write from the entity still needs one more loop turn.
    await hass.async_block_till_done()

    assert coordinator.state is not None
    state = hass.states.get("sensor.site_capacity_state")
    assert state is not None
    snapshot = state.attributes["solar_surplus"][charger.entry_id]
    assert snapshot["enabled"] is True
    assert snapshot["state"] == coordinator.state.state == "arming"
    assert snapshot["action"] == coordinator.state.action == "hold"
    assert snapshot["reason"] == coordinator.state.reason == "arming_delay"
    assert snapshot["available_a"] == pytest.approx(coordinator.state.available_a)
    assert snapshot["net_grid_w"] == pytest.approx(-4200.0)
    assert snapshot["priority_effective"] == "battery_first"


async def test_leaving_solar_stops_a_solar_started_charge_when_cheapest_cannot_plan(
    hass: HomeAssistant,
) -> None:
    """A `cheapest` reconcile that cannot plan (no area/amps/phases set) never stops a charge it did not start, so a charge solar started would run with no owner.

    Solar must release what it started when the strategy moves away from it.
    """
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    await arm_and_start(hass, site_controller, clock)
    assert coordinator.state is not None and coordinator.state.state == "on"
    assert len(turn_on_calls) == 1
    hass.states.async_set(f"switch.{prefix}", "on")
    turn_off_calls.clear()

    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_CHEAPEST))

    await tick_site(hass, site_controller)

    assert coordinator.state is None
    assert len(turn_off_calls) == 1


@freeze_time("2026-09-22 06:00:00")
async def test_leaving_solar_for_a_cheapest_that_can_plan_ends_in_cheapests_own_plan(
    hass: HomeAssistant,
) -> None:
    """`cheapest` can plan here (full settings, live prices), so the end state is what its reconcile decides; solar's release must not race that, only ensure nothing runs ownerless in between."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id
    await arm_and_start(hass, site_controller, clock)
    assert coordinator.state is not None and coordinator.state.state == "on"
    hass.states.async_set(f"switch.{prefix}", "on")

    from .world import go_auto

    snapshot = await go_auto(hass, charger.entry_id, strategy=STRATEGY_CHEAPEST)
    await tick_site(hass, site_controller)

    executor = executor_for(hass, charger.entry_id)
    assert executor is not None
    assert snapshot.state in ("proposal_ready", "proposal_unpriced")
    assert executor.applied is not None
    assert controller.plan is not None
    assert coordinator.state is None


async def test_a_hand_started_charge_is_left_alone_by_the_solar_to_cheapest_switch(
    hass: HomeAssistant,
) -> None:
    """Solar never considered itself responsible (its state stayed `off`: no surplus ever arrived), so leaving `solar` must not stop the charge."""
    charger, site_entry, controller, coordinator, clock, turn_on_calls, turn_off_calls = (
        await solar_setup(hass)
    )
    site_controller = controller_of(hass, site_entry.entry_id)
    prefix = charger.entry_id

    # No surplus at all: solar's state stays `off`.
    await tick_site(hass, site_controller)
    assert coordinator.state is not None and coordinator.state.state == "off"

    # A person starts the charger by hand, nothing to do with solar.
    hass.states.async_set(f"switch.{prefix}", "on")
    turn_off_calls.clear()

    store = domain_data(hass).auto_store
    assert store is not None
    await store.async_update(charger.entry_id, mutate=lambda s: replace(s, strategy=STRATEGY_CHEAPEST))

    await tick_site(hass, site_controller)

    assert coordinator.state is None
    assert not turn_off_calls, "a hand-started charge must not be stopped by leaving solar"
