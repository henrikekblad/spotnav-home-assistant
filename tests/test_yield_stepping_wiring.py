"""Wiring `execution/yield_stepping.py` into `SiteCapacityController._async_apply_active_control`.

Covers the four gates (`yield_stepping_enabled`, a decision that passed `applyability_failure`,
a real proposal, no pilot-floor probe in flight), how a verdict ("hold", "write", "passthrough")
becomes a charger command or none, and the site options and sensor attribute round trip.

No test sleeps: the stepper's clock (`controller._yield_now`) and the damper's
(`RegulatorDamper(now=...)`) are injected and moved by hand. `_active_control_setup`,
`set_derived_site_entities` and `_handmade_decision` are local adapted copies of those in
`test_site_capacity_controller.py`, so this file runs on its own.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_MEASURED_CURRENT_SOURCE,
    CONF_YIELD_CEILING_A,
    CONF_YIELD_STEPPING_ENABLED,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    DOMAIN,
    MEASUREMENT_MODE_DERIVED,
    default_yield_ceiling_a,
)
from custom_components.spotnav.site.regulator_damping import RegulatorDamper
from custom_components.spotnav.site.regulator import (
    PhaseDecisionBasis,
    RegulatorDecision,
)
from custom_components.spotnav.site.site_capacity import PHASES
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController

from .helpers import make_entry, make_site_entry, set_current_sensor
from .world import set_charger_delivered_a, set_derived_site_entities, set_site_current_a
from .world import separate_entities_source
from .world import controller_of

# A fresh net-import wattage: only its sign and freshness matter to `applyability_failure`. One constant so every handmade decision agrees with the derived-mode sensors set here.
_NET_IMPORT_W = 4646.0


async def _yield_setup(
    hass: HomeAssistant,
    *,
    entry_id: str,
    main_fuse_a: float = 20.0,
    safety_margin_a: float = 1.0,
    yield_stepping_enabled: bool = True,
    yield_ceiling_a: float | None = None,
    min_current_a: float = 6.0,
    battery_entity: str | None = None,
    solar_priority: str | None = None,
) -> tuple[object, object, list]:
    """A site opted into active control with one 3-phase charger wired for charge control and a measured (delivered) current source.

    Returns `(site_entry, charger_entry, configure_calls)`; the charger's own `async_start` write is cleared first.
    """
    prefix = f"{entry_id}_charger"
    hass.states.async_set(f"switch.{prefix}", "off")
    async_mock_service(hass, "switch", "turn_on")
    async_mock_service(hass, "ocpp", "get_configuration", response={"value": "1.6,2.10"})
    configure_calls = async_mock_service(hass, "ocpp", "configure")
    charger = make_entry(
        hass,
        entry_id=f"{entry_id}_charger",
        charge_control=f"switch.{prefix}",
        current_limit=f"number.{prefix}_connector_1_session_current_limit",
        webhook_id=f"webhook-{entry_id}",
        title="Yield stepping charger",
        current_control=CURRENT_CONTROL_CHANGE_CONFIGURATION,
        ocpp_target=(prefix, 1),
    )
    assert await hass.config_entries.async_setup(charger.entry_id)
    await hass.async_block_till_done()
    charger_controller = controller_of(hass, charger.entry_id)
    await charger_controller.async_start(amps=16)
    hass.states.async_set(f"switch.{prefix}", "on")
    configure_calls.clear()

    set_charger_delivered_a(hass, prefix, 5.9)
    derived_entities = set_derived_site_entities(hass, f"{entry_id}_site", _NET_IMPORT_W)

    site_entry = make_site_entry(
        hass,
        entry_id=f"{entry_id}_site",
        main_fuse_a=main_fuse_a,
        safety_margin_a=safety_margin_a,
        charger_entry_ids=[charger.entry_id],
        phase_wiring={
            charger.entry_id: {
                "phases": 3,
                "phase": None,
                "min_current_a": min_current_a,
                CONF_MEASURED_CURRENT_SOURCE: separate_entities_source(
                        f"sensor.{prefix}_l1", f"sensor.{prefix}_l2", f"sensor.{prefix}_l3"
                    ),
            }
        },
        measurement_mode=MEASUREMENT_MODE_DERIVED,
        derived_entities=derived_entities,
        active_control_enabled=True,
        yield_stepping_enabled=yield_stepping_enabled,
        yield_ceiling_a=yield_ceiling_a,
        battery_aggregate_power_entity=battery_entity,
        solar_priority=solar_priority,
    )
    assert await hass.config_entries.async_setup(site_entry.entry_id)
    await hass.async_block_till_done()
    configure_calls.clear()

    # These tests drive the apply path with explicit `_async_apply_active_control()` calls, and the settling/revert sequences
    # need the stepper to see exactly that sequence. Every sensor wired here is also a tracked entity, so each per-phase
    # `hass.states.async_set` would fire the controller's state-change listener, recompute a regulator decision from a partial
    # state and schedule an apply pass feeding the stepper a spurious observation. Cancelling that listener isolates the sequence.
    controller = controller_of(hass, site_entry.entry_id)
    if controller._state_listener_cancel is not None:
        controller._state_listener_cancel()
        controller._state_listener_cancel = None
    return site_entry, charger, configure_calls


def _decision(
    *, proposed_a: float, requested_a: float, reason: str = "no_change_requested"
) -> RegulatorDecision:
    """A handmade all-three-phases, net-import decision; the regulator's arithmetic is not under test.

    `direction`/`confirmed_direction` are `net_import`, matching the fresh derived-mode sensors
    that `applyability_failure` re-checks.
    """
    basis = {
        phase: PhaseDecisionBasis(
            signed_active_power_w=_NET_IMPORT_W,
            signed_active_power_age_s=1.0,
            measured_current_a=5.9,
            measured_current_age_s=1.0,
            requested_current_a=requested_a,
            direction="net_import",
            confirmed_direction="net_import",
        )
        for phase in PHASES
    }
    return RegulatorDecision(
        proposed_current_a=proposed_a, reason=reason, limiting_phase="L1", basis=basis
    )


def _seed_assigned_a(controller: SiteCapacityController, charger_entry_id: str, amps: float) -> None:
    """Give this charger's damper a real `last_written_a` of `amps` through the ordinary first-write path (a fresh damper writes any proposal immediately).

    Call `_dampers.clear()` (and install a clocked damper if needed) before this; never poke the damper's private state.
    """
    controller.regulator_decisions = {
        charger_entry_id: _decision(proposed_a=amps, requested_a=amps)
    }


class _SecondsClock:
    """A plain seconds clock advanced by hand, the source of `YieldObservation.now` (`controller._yield_now`), independent of the damper's `datetime` clock."""

    def __init__(self) -> None:
        self._now = 1_000_000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _DamperClock:
    """A plain `datetime` clock advanced by hand, for `RegulatorDamper`; a local copy of `test_site_capacity_controller.SecondsClock`."""

    def __init__(self) -> None:
        self._now = dt_util.utcnow()

    def __call__(self):
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


async def test_disabled_by_default_behaves_exactly_as_today(hass: HomeAssistant) -> None:
    """With the site option absent, an increase writes exactly what it would without the feature; the stepper is never consulted (`_yield_steppers` stays empty)."""
    site_entry, charger, configure_calls = await _yield_setup(
        hass, entry_id="disabled_default", yield_stepping_enabled=False
    )
    controller = controller_of(hass, site_entry.entry_id)
    assert CONF_YIELD_STEPPING_ENABLED not in site_entry.data  # the precondition

    controller._dampers.clear()
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=16.0, requested_a=16.0)
    }
    await controller._async_apply_active_control()

    assert len(configure_calls) == 1
    assert configure_calls[0].data["value"] == "1.16,2.10"
    assert controller._yield_steppers == {}


async def test_disabled_explicitly_matches_disabled_by_default(hass: HomeAssistant) -> None:
    """An explicit `False` behaves identically to the option being absent."""
    site_entry, charger, configure_calls = await _yield_setup(
        hass, entry_id="disabled_explicit", yield_stepping_enabled=False
    )
    controller = controller_of(hass, site_entry.entry_id)

    controller._dampers.clear()
    controller.regulator_decisions = {
        charger.entry_id: _decision(proposed_a=16.0, requested_a=16.0)
    }
    await controller._async_apply_active_control()

    assert len(configure_calls) == 1
    assert configure_calls[0].data["value"] == "1.16,2.10"
    assert controller._yield_steppers == {}


async def test_enabled_on_the_real_condition_a_probe_write_reaches_the_charger(
    hass: HomeAssistant,
) -> None:
    """Site pinned at ~20.2 A/phase on a 20 A fuse (1 A margin), charger at its 6 A floor delivering 5.9 A, raw a standing pause.

    With `assigned_a` at the floor, `held_at_floor_raw_pause` lets step 6 consider a probe. The stepper licenses
    an unverified probe to 8 A (`unknown` state, step_a 2.0), which reaches the charger's assign path once the
    damper's deadband/dwell allow it.
    """
    site_entry, charger, configure_calls = await _yield_setup(hass, entry_id="probe")
    controller = controller_of(hass, site_entry.entry_id)
    charger_controller = controller_of(hass, charger.entry_id)

    yield_clock = _SecondsClock()
    controller._yield_now = yield_clock
    damper_clock = _DamperClock()
    controller._dampers[charger.entry_id] = RegulatorDamper(now=damper_clock)
    controller._yield_steppers.clear()

    _seed_assigned_a(controller, charger.entry_id, 6.0)
    await controller._async_apply_active_control()
    assert configure_calls[-1].data["value"] == "1.6,2.10"
    configure_calls.clear()

    set_site_current_a(hass, "probe_site", 20.2)
    set_charger_delivered_a(hass, "probe_charger", 5.9)
    pause = _decision(
        proposed_a=0.0, requested_a=16.0, reason="paused_safe_current_below_charger_minimum"
    )
    controller.regulator_decisions = {charger.entry_id: pause}

    await controller._async_apply_active_control()
    verdict = controller._yield_stepping_last[charger.entry_id]
    assert verdict == {
        "state": "unknown",
        "y": None,
        "y_age_s": None,
        "reference_a": {},
        "action": "write",
        "reason": "probe_step",
        "battery_verified": False,
        "battery_credit_a": None,
    }
    # An ordinary (non-urgent) increase is damped like any proposal, so it does not reach the charger on this pass.
    assert configure_calls == []
    assert not charger_controller._probe.in_flight  # the guard this test is not exercising

    damper_clock.advance(61.0)  # past the default 60 s dwell
    await controller._async_apply_active_control()

    assert len(configure_calls) == 1
    assert configure_calls[0].data["value"] == "1.8,2.10"


async def test_stale_reading_means_the_stepper_is_not_consulted(hass: HomeAssistant) -> None:
    """A decision whose basis direction no longer matches the live reading fails `applyability_failure` before the stepper's gates; the stepper never sees it."""
    site_entry, charger, configure_calls = await _yield_setup(hass, entry_id="stale")
    controller = controller_of(hass, site_entry.entry_id)
    controller._dampers.clear()
    controller._yield_steppers.clear()

    _seed_assigned_a(controller, charger.entry_id, 6.0)
    await controller._async_apply_active_control()
    configure_calls.clear()
    controller._yield_steppers.clear()

    # The site flips to export between the decision and the write, as in `test_active_control_refuses_a_write_when_the_sign_changed`.
    set_site_current_a(hass, "stale_site", -20.2)
    pause = _decision(
        proposed_a=0.0, requested_a=16.0, reason="paused_safe_current_due_to_active_import_overload"
    )
    controller.regulator_decisions = {charger.entry_id: pause}

    await controller._async_apply_active_control()

    assert configure_calls == []
    assert controller._yield_steppers == {}


async def test_probe_in_flight_means_the_stepper_is_not_consulted(hass: HomeAssistant) -> None:
    """While the pilot-floor probe runs (`execution/controller.py` suppresses writes itself), the stepper is also never fed an observation, since its artificial steps would contaminate `y`."""
    site_entry, charger, configure_calls = await _yield_setup(hass, entry_id="probeflight")
    controller = controller_of(hass, site_entry.entry_id)
    charger_controller = controller_of(hass, charger.entry_id)
    controller._dampers.clear()
    controller._yield_steppers.clear()

    _seed_assigned_a(controller, charger.entry_id, 6.0)
    await controller._async_apply_active_control()
    configure_calls.clear()
    controller._yield_steppers.clear()

    charger_controller._probe._in_flight = True
    set_site_current_a(hass, "probeflight_site", 20.2)
    set_charger_delivered_a(hass, "probeflight_charger", 5.9)
    pause = _decision(
        proposed_a=0.0, requested_a=16.0, reason="paused_safe_current_below_charger_minimum"
    )
    controller.regulator_decisions = {charger.entry_id: pause}

    await controller._async_apply_active_control()

    assert configure_calls == []
    assert controller._yield_steppers == {}

    charger_controller._probe._in_flight = False


async def test_a_hold_results_in_no_call_to_the_assign_path(hass: HomeAssistant) -> None:
    """`held_at_floor_raw_pause` with nothing left to request (`requested_a == assigned_a`, both at the floor): a plain hold, no damper call, no write."""
    site_entry, charger, configure_calls = await _yield_setup(hass, entry_id="hold")
    controller = controller_of(hass, site_entry.entry_id)
    controller._dampers.clear()
    controller._yield_steppers.clear()

    _seed_assigned_a(controller, charger.entry_id, 6.0)
    await controller._async_apply_active_control()
    configure_calls.clear()
    controller._yield_steppers.clear()

    set_site_current_a(hass, "hold_site", 20.2)
    set_charger_delivered_a(hass, "hold_charger", 5.9)
    pause = _decision(
        proposed_a=0.0, requested_a=6.0, reason="paused_safe_current_below_charger_minimum"
    )
    controller.regulator_decisions = {charger.entry_id: pause}

    await controller._async_apply_active_control()

    assert configure_calls == []
    assert controller._yield_stepping_last[charger.entry_id]["action"] == "hold"
    assert controller._yield_stepping_last[charger.entry_id]["reason"] == "held_at_floor_raw_pause"


async def test_urgent_revert_reaches_the_charger_without_the_dwell(hass: HomeAssistant) -> None:
    """A step that is not absorbed is reverted urgently, in the very pass it is decided, never waiting out the damper's dwell.

    The site has generous margin (regulation band throughout) so only `urgent=True`, not the
    damper's "protection" rule, can explain an instant write.
    """
    site_entry, charger, configure_calls = await _yield_setup(
        hass, entry_id="revert", main_fuse_a=40.0, safety_margin_a=1.0
    )
    controller = controller_of(hass, site_entry.entry_id)
    controller._yield_steppers.clear()

    yield_clock = _SecondsClock()
    controller._yield_now = yield_clock
    damper_clock = _DamperClock()
    controller._dampers[charger.entry_id] = RegulatorDamper(now=damper_clock)

    _seed_assigned_a(controller, charger.entry_id, 6.0)
    await controller._async_apply_active_control()
    configure_calls.clear()

    # Pass 1: baseline. Delivered/site steady: the stepper records its pre-step reference.
    set_site_current_a(hass, "revert_site", 10.0)
    set_charger_delivered_a(hass, "revert_charger", 5.9)
    probe_decision = _decision(
        proposed_a=0.0, requested_a=16.0, reason="paused_safe_current_below_charger_minimum"
    )
    controller.regulator_decisions = {charger.entry_id: probe_decision}
    await controller._async_apply_active_control()
    assert controller._yield_stepping_last[charger.entry_id]["reason"] == "probe_step"
    assert configure_calls == []  # an ordinary increase: damped, dwell pending

    # Pass 2 (damper clock only, +61 s): the same probe is written for real; `assigned_a` becomes 8.0.
    damper_clock.advance(61.0)
    await controller._async_apply_active_control()
    assert len(configure_calls) == 1
    assert configure_calls[0].data["value"] == "1.8,2.10"
    configure_calls.clear()

    # Pass 3: the car's delivered current rises (>= min_delta_a) while the site rises almost in lockstep: nothing yielded. Detected from the delivered-current jump alone; enters `settling`, holds.
    set_site_current_a(hass, "revert_site", 11.0)
    set_charger_delivered_a(hass, "revert_charger", 7.6)
    controller.regulator_decisions = {charger.entry_id: probe_decision}
    await controller._async_apply_active_control()
    assert controller._yield_stepping_last[charger.entry_id]["state"] == "settling"
    assert configure_calls == []

    # Pass 4 (yield clock only, past settle_s): the step is measured and not absorbed (y well below confirm_y), so an urgent revert to the pre-step assigned value (6.0) reaches the charger in this pass, far from the protection band and without advancing the damper's dwell.
    yield_clock.advance(31.0)
    await controller._async_apply_active_control()

    verdict = controller._yield_stepping_last[charger.entry_id]
    assert verdict["action"] == "write"
    assert verdict["reason"] == "revert_not_absorbed"
    assert verdict["state"] == "backoff"
    assert len(configure_calls) == 1
    assert configure_calls[0].data["value"] == "1.6,2.10"


async def test_options_round_trip_the_yield_stepping_fields(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="yield_opts", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.yield_opts_l1", 0)
    set_current_sensor(hass, "sensor.yield_opts_l2", 0)
    set_current_sensor(hass, "sensor.yield_opts_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "site_init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Yield Opts",
            "main_fuse_a": 25,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": False,
            "change_measurement": True,
            "max_age_s": 120,
            CONF_YIELD_STEPPING_ENABLED: True,
            CONF_YIELD_CEILING_A: 30.0,
        },
    )
    assert result["step_id"] == "site_current_suggestions"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "manual"}
    )
    assert result["step_id"] == "site_details"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "direct_L1": "sensor.yield_opts_l1",
            "direct_L2": "sensor.yield_opts_l2",
            "direct_L3": "sensor.yield_opts_l3",
        },
    )
    assert result["type"].value == "create_entry"

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated.data[CONF_YIELD_STEPPING_ENABLED] is True
    assert updated.data[CONF_YIELD_CEILING_A] == 30.0


async def test_options_flow_rejects_a_ceiling_at_or_below_the_fuse(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="yield_reject", charger_entry_ids=[])
    set_current_sensor(hass, "sensor.yield_reject_l1", 0)
    set_current_sensor(hass, "sensor.yield_reject_l2", 0)
    set_current_sensor(hass, "sensor.yield_reject_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Yield Reject",
            "main_fuse_a": 20,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": False,
            "change_measurement": True,
            "max_age_s": 120,
            CONF_YIELD_STEPPING_ENABLED: True,
            CONF_YIELD_CEILING_A: 20.0,  # equal to the fuse: rejected
        },
    )

    assert result["step_id"] == "site_init"
    assert result["errors"] == {CONF_YIELD_CEILING_A: "yield_ceiling_below_fuse"}

    unchanged = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_YIELD_CEILING_A not in unchanged.data


async def test_defaults_for_an_old_entry_that_predates_these_options(hass: HomeAssistant) -> None:
    """An entry with neither key (built directly, not via `make_site_entry`) reads the defaults: disabled, and a ceiling of 1.15x its main fuse."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.spotnav.const import (
        CONF_CHARGER_ENTRY_IDS,
        CONF_DERIVED_ENTITIES,
        CONF_DIRECT_ENTITIES,
        CONF_ENTRY_TYPE,
        CONF_MAIN_FUSE_A,
        CONF_MAX_AGE_S,
        CONF_MEASUREMENT_MODE,
        CONF_PHASE_WIRING,
        CONF_SAFETY_MARGIN_A,
        CONF_SITE_ENABLED,
        DEFAULT_MAX_AGE_S,
        ENTRY_TYPE_SITE,
        MEASUREMENT_MODE_DIRECT,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="old_entry",
        title="Old Site",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SITE,
            CONF_SITE_ENABLED: True,
            CONF_MAIN_FUSE_A: 20.0,
            CONF_SAFETY_MARGIN_A: 1.0,
            CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT,
            CONF_CHARGER_ENTRY_IDS: [],
            CONF_PHASE_WIRING: {},
            CONF_DIRECT_ENTITIES: {
                "L1": "sensor.old_entry_l1",
                "L2": "sensor.old_entry_l2",
                "L3": "sensor.old_entry_l3",
            },
            CONF_DERIVED_ENTITIES: {},
            CONF_MAX_AGE_S: DEFAULT_MAX_AGE_S,
            # Deliberately no CONF_YIELD_STEPPING_ENABLED / CONF_YIELD_CEILING_A.
        },
    )
    entry.add_to_hass(hass)
    set_current_sensor(hass, "sensor.old_entry_l1", 0)
    set_current_sensor(hass, "sensor.old_entry_l2", 0)
    set_current_sensor(hass, "sensor.old_entry_l3", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    assert bool(controller.config.get(CONF_YIELD_STEPPING_ENABLED, False)) is False
    stepper = controller._yield_stepper_for("some_charger")
    assert stepper._config.ceiling_a == default_yield_ceiling_a(20.0)


async def test_a_stored_ceiling_is_clamped_to_the_fuse_actually_configured(
    hass: HomeAssistant,
) -> None:
    """A ceiling chosen for a larger fuse must not survive that fuse being replaced by a smaller one.

    28.75 A was 1.15 x a 25 A fuse; on a 20 A fuse it would be 1.44 x, past the 1.25 x a gG fuse
    carries for an hour. The run-time clamp makes that impossible whatever is stored.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.spotnav.const import (
        CONF_CHARGER_ENTRY_IDS,
        CONF_DERIVED_ENTITIES,
        CONF_DIRECT_ENTITIES,
        CONF_ENTRY_TYPE,
        CONF_MAIN_FUSE_A,
        CONF_MAX_AGE_S,
        CONF_MEASUREMENT_MODE,
        CONF_PHASE_WIRING,
        CONF_SAFETY_MARGIN_A,
        CONF_SITE_ENABLED,
        DEFAULT_MAX_AGE_S,
        ENTRY_TYPE_SITE,
        MEASUREMENT_MODE_DIRECT,
        max_yield_ceiling_a,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="shrunk_fuse",
        title="Shrunk Fuse",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_SITE,
            CONF_SITE_ENABLED: True,
            CONF_MAIN_FUSE_A: 20.0,
            CONF_SAFETY_MARGIN_A: 1.0,
            CONF_MEASUREMENT_MODE: MEASUREMENT_MODE_DIRECT,
            CONF_CHARGER_ENTRY_IDS: [],
            CONF_PHASE_WIRING: {},
            CONF_DIRECT_ENTITIES: {
                "L1": "sensor.shrunk_fuse_l1",
                "L2": "sensor.shrunk_fuse_l2",
                "L3": "sensor.shrunk_fuse_l3",
            },
            CONF_DERIVED_ENTITIES: {},
            CONF_MAX_AGE_S: DEFAULT_MAX_AGE_S,
            CONF_YIELD_STEPPING_ENABLED: True,
            CONF_YIELD_CEILING_A: 28.75,
        },
    )
    entry.add_to_hass(hass)
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.shrunk_fuse_{phase}", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = controller_of(hass, entry.entry_id)
    stepper = controller._yield_stepper_for("some_charger")
    assert stepper._config.ceiling_a == max_yield_ceiling_a(20.0)
    assert stepper._config.ceiling_a < 28.75


async def test_options_flow_rejects_a_ceiling_at_or_above_the_limit(hass: HomeAssistant) -> None:
    entry = make_site_entry(hass, entry_id="yield_high", charger_entry_ids=[])
    for phase in ("l1", "l2", "l3"):
        set_current_sensor(hass, f"sensor.yield_high_{phase}", 0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Yield High",
            "main_fuse_a": 20,
            "safety_margin_a": 1,
            "measurement_mode": "direct_phase_current",
            "charger_entry_ids": [],
            "site_enabled": False,
            "change_measurement": True,
            "max_age_s": 120,
            CONF_YIELD_STEPPING_ENABLED: True,
            CONF_YIELD_CEILING_A: 25.0,  # exactly 1.25 x the fuse: rejected
        },
    )

    assert result["step_id"] == "site_init"
    assert result["errors"] == {CONF_YIELD_CEILING_A: "yield_ceiling_above_limit"}


async def test_yield_stepping_attribute_present_when_disabled(hass: HomeAssistant) -> None:
    site_entry, charger, _configure_calls = await _yield_setup(
        hass, entry_id="attr_disabled", yield_stepping_enabled=False
    )
    controller = controller_of(hass, site_entry.entry_id)

    state = hass.states.get("sensor.site_capacity_state")
    snapshot = state.attributes["yield_stepping"]
    assert snapshot[charger.entry_id] == {
        "enabled": False,
        "state": "unknown",
        "y": None,
        "y_age_s": None,
        "reference_a": {},
        "action": None,
        "reason": None,
        "battery_verified": False,
        "battery_credit_a": None,
    }
    assert controller.yield_stepping_snapshot == snapshot


async def test_yield_stepping_attribute_reflects_the_latest_verdict_when_enabled(
    hass: HomeAssistant,
) -> None:
    site_entry, charger, configure_calls = await _yield_setup(hass, entry_id="attr_enabled")
    controller = controller_of(hass, site_entry.entry_id)
    controller._dampers.clear()
    controller._yield_steppers.clear()

    _seed_assigned_a(controller, charger.entry_id, 6.0)
    await controller._async_apply_active_control()
    configure_calls.clear()
    controller._yield_steppers.clear()

    set_site_current_a(hass, "attr_enabled_site", 20.2)
    set_charger_delivered_a(hass, "attr_enabled_charger", 5.9)
    pause = _decision(
        proposed_a=0.0, requested_a=16.0, reason="paused_safe_current_below_charger_minimum"
    )
    controller.regulator_decisions = {charger.entry_id: pause}
    await controller._async_apply_active_control()
    controller._notify()
    await hass.async_block_till_done()

    state = hass.states.get("sensor.site_capacity_state")
    snapshot = state.attributes["yield_stepping"][charger.entry_id]
    assert snapshot["enabled"] is True
    assert snapshot["action"] == "write"
    assert snapshot["reason"] == "probe_step"
    assert snapshot["state"] == "unknown"
