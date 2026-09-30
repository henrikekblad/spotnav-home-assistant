"""Tests for the load-balancing regulator (`site/regulator.py`) on top of `site/site_capacity.py`."""

from __future__ import annotations

from dataclasses import replace

from custom_components.spotnav.site.regulator import (
    DEFAULT_ZERO_MARGIN_W,
    RegulatorDecision,
    allocate_regulator_decisions,
    applyability_failure,
    classify_direction,
    confirmed_direction,
    decide_charging_current,
    decision_still_applyable,
)
from custom_components.spotnav.site.site_capacity import (
    DEFAULT_MIN_CURRENT_A,
    PHASES,
    ChargerRequest,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PhaseValue,
    SiteCalculationConfig,
    calculate_site_capacity,
)

HEALTHY_AGE = 5.0
MAX_AGE = 120.0
VOLTAGE = 230.0
MAIN_FUSE_A = 20.0


def _derived_input(p: float, q: float = 0.0, v: float = VOLTAGE, age: float = HEALTHY_AGE) -> DerivedPhaseInput:
    return DerivedPhaseInput(
        active_power_w=PhaseValue(p, age),
        reactive_power_var=PhaseValue(q, age),
        voltage_v=PhaseValue(v, age),
    )


def _site_result(
    p_l1: float, p_l2: float | None = None, p_l3: float | None = None, *, max_age_s: float = MAX_AGE,
    ages: tuple[float, float, float] = (HEALTHY_AGE, HEALTHY_AGE, HEALTHY_AGE),
):
    """A real `SiteCapacityResult` (no chargers), all phases at the same power unless overridden."""
    p2 = p_l1 if p_l2 is None else p_l2
    p3 = p_l1 if p_l3 is None else p_l3
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(p_l1, age=ages[0]),
        l2=_derived_input(p2, age=ages[1]),
        l3=_derived_input(p3, age=ages[2]),
    )
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=MAIN_FUSE_A,
        safety_margin_a=1.0,
        measurement_mode="derived_phase_current",  # type: ignore[arg-type]
        max_age_s=max_age_s,
        derived=derived,
    )
    return calculate_site_capacity(config, [])


def _voltage_by_phase(v: float = VOLTAGE) -> dict[str, float | None]:
    return {phase: v for phase in PHASES}


def _confirmed(direction: str | None) -> dict[str, str | None]:
    return {phase: direction for phase in PHASES}


def _charger(
    requested_current_a: float | None,
    measured_current_a: float | None,
    *,
    phases: int = 3,
    measured_age: float = HEALTHY_AGE,
    min_current_a: float = DEFAULT_MIN_CURRENT_A,
) -> ChargerRequest:
    if measured_current_a is None:
        measured = None
    else:
        measured = DirectPhaseMeasurement(
            l1=PhaseValue(measured_current_a, measured_age),
            l2=PhaseValue(measured_current_a, measured_age),
            l3=PhaseValue(measured_current_a, measured_age),
        )
    return ChargerRequest(
        charger_entry_id="charger_a",
        requested_current_a=requested_current_a,
        phases=phases,  # type: ignore[arg-type]
        min_current_a=min_current_a,
        measured_current_a=measured,
    )


def _decide(site_result, request, *, confirmed: str | None = "net_import") -> RegulatorDecision:
    return decide_charging_current(
        site_result=site_result,
        request=request,
        voltage_by_phase=_voltage_by_phase(),
        confirmed_direction_by_phase=_confirmed(confirmed),
        max_age_s=MAX_AGE,
    )


def test_the_decision_uses_the_uncredited_margin_not_the_diagnostic_numbers() -> None:
    """The decision reads `measured_margin_a` only, never the diagnostic headroom fields.

    Replacing both diagnostic fields with values that justify a different current
    must leave the decision identical.
    """
    site = _site_result(6.0 * VOLTAGE)
    request = _charger(requested_current_a=16.0, measured_current_a=0.57)
    decision = _decide(site, request)

    assert decision.proposed_current_a is not None  # the guard, not an assumption
    shifted = replace(
        site,
        calculated_headroom_after_ev_credit_a={phase: 100.0 for phase in PHASES},
        estimated_headroom_if_battery_yields_a={phase: 100.0 for phase in PHASES},
    )

    assert _decide(shifted, request) == decision


def test_the_proposal_adds_two_measurements_so_a_stale_site_reading_lands_whole() -> None:
    """Why the proposed amplitude is whole amps rather than one step.

    The ceiling is `measured_current + measured_margin`, two readings with their own
    ages. If the site total contains the charger's own current, it cancels and the
    proposal is the phase's true allowance. If it does not (the site reading has not
    caught up, or never covers this charger), the whole draw is added on top.
    """
    # 20 A fuse, 1 A safety margin, 9 A on the charger's own phase measurement.
    other_load_a = 6.0
    charger_current_a = 9.0

    # The site's total contains the charger's own 9 A: the allowance for it is
    # 20 - 1 - 6 = 13 A, and the proposal is exactly that.
    consistent = _site_result((other_load_a + charger_current_a) * VOLTAGE)
    proposed = _decide(
        consistent, _charger(requested_current_a=16.0, measured_current_a=charger_current_a)
    ).proposed_current_a
    assert proposed == 13.0

    # The site's total does not (yet) contain it: the same allowance plus the
    # whole 9 A, capped by the request rather than by the margin.
    stale = _site_result(other_load_a * VOLTAGE)
    assert (
        _decide(
            stale, _charger(requested_current_a=16.0, measured_current_a=charger_current_a)
        ).proposed_current_a
        == 16.0
    )


def test_classify_direction_within_margin_is_near_zero() -> None:
    assert classify_direction(100.0, DEFAULT_ZERO_MARGIN_W) == "near_zero"
    assert classify_direction(-100.0, DEFAULT_ZERO_MARGIN_W) == "near_zero"
    assert classify_direction(None, DEFAULT_ZERO_MARGIN_W) is None


def test_classify_direction_beyond_margin_has_a_sign() -> None:
    assert classify_direction(1000.0, DEFAULT_ZERO_MARGIN_W) == "net_import"
    assert classify_direction(-1000.0, DEFAULT_ZERO_MARGIN_W) == "net_export"


def test_confirmed_direction_requires_enough_history() -> None:
    assert confirmed_direction(["net_import", "net_import"], 3) is None


def test_confirmed_direction_one_inconsistent_reading_withholds_confirmation() -> None:
    """One inconsistent reading in the recent window refuses confirmation, even if the latest matches the one before."""
    history = ["net_import", "net_import", "net_export", "net_import", "net_import"]
    assert confirmed_direction(history, 3) is None


def test_confirmed_direction_a_consistent_run_confirms() -> None:
    history = ["net_export", "net_import", "net_import", "net_import"]
    assert confirmed_direction(history, 3) == "net_import"


def test_confirmed_direction_any_unknown_entry_withholds_confirmation() -> None:
    assert confirmed_direction(["net_import", None, "net_import"], 3) is None


def test_normal_import_increase_is_capped_by_the_safe_uncredited_margin() -> None:
    # Household total ~3.5 kW/phase (~15.2 A, including this charger's 6 A); charger asks to go from 6 A to 16 A.
    site_result = _site_result(3500.0)
    request = _charger(requested_current_a=16.0, measured_current_a=6.0)

    decision = _decide(site_result, request)

    assert decision.reason == "increase_within_safe_uncredited_margin"
    # measured_margin_a = 20 - 1 - 3500/230 ~= 19 - 15.22 ~= 3.78A of
    # *additional* headroom on top of the 6A already flowing -> capped
    # at ~9.78A, floored to 9.0A -- well short of the requested 16A.
    assert decision.proposed_current_a is not None
    assert 6.0 < decision.proposed_current_a < 16.0


def test_heat_pump_starting_reduces_the_available_increase() -> None:
    baseline = _site_result(2000.0)
    with_heat_pump = _site_result(4200.0)  # a sudden, large jump in other load
    request = _charger(requested_current_a=16.0, measured_current_a=6.0)

    before = _decide(baseline, request)
    after = _decide(with_heat_pump, request)

    assert before.proposed_current_a is not None
    assert after.proposed_current_a is not None
    assert after.proposed_current_a < before.proposed_current_a
    assert after.reason == "increase_within_safe_uncredited_margin"


def test_battery_charging_is_net_import_and_a_small_decrease_is_confirmed_safe() -> None:
    # Battery charging adds import; charger at 16A asks to drop to 10A.
    site_result = _site_result(3000.0)  # comfortably net import
    request = _charger(requested_current_a=10.0, measured_current_a=16.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.reason == "decrease_confirmed_safe"
    assert decision.proposed_current_a == 10.0


def test_battery_export_near_the_fuse_limit_refuses_to_assume_a_decrease_helps() -> None:
    """Battery export near the fuse: a reduction request must be refused, not assumed to reduce load."""
    # ~-4100 W/phase, battery export net of a charging EV.
    site_result = _site_result(-4100.0)
    request = _charger(requested_current_a=6.0, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_export")

    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"
    assert decision.proposed_current_a == 10.0  # unchanged, NOT the requested 6.0


def test_mixed_directions_between_phases_the_exporting_phase_governs() -> None:
    """L1 exports near the fuse while L2/L3 import: the whole charger stays at its current draw because of L1."""
    site_result = _site_result(-4100.0, 3000.0, 3000.0)
    request = _charger(requested_current_a=6.0, measured_current_a=10.0)

    decision = decide_charging_current(
        site_result=site_result,
        request=request,
        voltage_by_phase=_voltage_by_phase(),
        confirmed_direction_by_phase={"L1": "net_export", "L2": "net_import", "L3": "net_import"},
        max_age_s=MAX_AGE,
    )

    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"
    assert decision.proposed_current_a == 10.0
    assert decision.limiting_phase == "L1"


def test_stale_phase_measurement_is_undetermined_not_guessed() -> None:
    """One stale phase marks the whole site stale, so an increase is refused; which phase is named is incidental."""
    site_result = _site_result(2000.0, ages=(HEALTHY_AGE, MAX_AGE + 30.0, HEALTHY_AGE))
    request = _charger(requested_current_a=16.0, measured_current_a=6.0)

    decision = _decide(site_result, request)

    assert decision.proposed_current_a is None
    assert decision.reason == "phase_measurement_unusable"
    assert decision.limiting_phase in PHASES


def test_charger_not_following_its_last_requested_current_is_evaluated_against_reality() -> None:
    """A charger told to run at 10 A but still drawing 16 A is judged on the measured 16 A, not on the earlier request."""
    site_result = _site_result(-4100.0)
    # Still measured at 16 A even though 10 A was requested before.
    request = _charger(requested_current_a=6.0, measured_current_a=16.0)

    decision = _decide(site_result, request, confirmed="net_export")

    # Grounded in the real 16 A: still refuses to reduce while the site nets to export.
    assert decision.proposed_current_a == 16.0
    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"


def test_charger_measurement_missing_entirely_is_undetermined() -> None:
    """No measured-current source: no ground truth, so no decision."""
    site_result = _site_result(2000.0)
    request = _charger(requested_current_a=16.0, measured_current_a=None)

    decision = _decide(site_result, request)

    assert decision.proposed_current_a is None
    assert decision.reason == "charger_measurement_unusable"


def test_charger_measurement_itself_stale_is_undetermined() -> None:
    site_result = _site_result(2000.0)
    request = _charger(requested_current_a=16.0, measured_current_a=6.0, measured_age=MAX_AGE + 30.0)

    decision = _decide(site_result, request)

    assert decision.proposed_current_a is None
    assert decision.reason == "charger_measurement_unusable"


def test_boundary_the_worked_counterexample_within_the_conservative_margin() -> None:
    """P_rest = -1 kW, P_EV = +2 kW gives +1 kW; a 0.5 kW reduction is within the 0.5 margin factor and confirmed safe."""
    site_result = _site_result(1000.0)
    delta_a = 500.0 / VOLTAGE
    request = _charger(requested_current_a=10.0 - delta_a, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.reason == "decrease_confirmed_safe"
    assert decision.proposed_current_a == request.requested_current_a


def test_boundary_just_outside_the_conservative_margin_is_kept_unchanged() -> None:
    """A 0.9 kW reduction at the same total is inside `delta <= 2 * total` but outside the conservative 0.5 margin, so not confirmed safe."""
    site_result = _site_result(1000.0)
    delta_a = 900.0 / VOLTAGE
    request = _charger(requested_current_a=10.0 - delta_a, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.reason == "decrease_margin_exceeded_kept_unchanged"
    assert decision.proposed_current_a == 10.0


def test_boundary_exactly_at_the_theoretical_limit_is_kept_unchanged_not_help() -> None:
    """`delta == 2 * total` is the neutral point, never a genuine improvement."""
    site_result = _site_result(1000.0)
    delta_a = 2000.0 / VOLTAGE
    request = _charger(requested_current_a=10.0 - delta_a, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.reason == "decrease_margin_exceeded_kept_unchanged"
    assert decision.proposed_current_a == 10.0


def test_boundary_beyond_the_theoretical_limit_is_would_increase_net_current() -> None:
    """`delta > 2 * total` overshoots the mirrored export-side point: a forced worsening."""
    site_result = _site_result(1000.0)
    delta_a = 2100.0 / VOLTAGE
    request = _charger(requested_current_a=10.0 - delta_a, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"
    assert decision.proposed_current_a == 10.0


def test_boundary_any_reduction_during_export_is_refused_regardless_of_size() -> None:
    site_result = _site_result(-1000.0)
    request = _charger(requested_current_a=9.9, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_export")

    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"
    assert decision.proposed_current_a == 10.0


def test_boundary_near_zero_crossing_is_kept_unchanged_regardless_of_delta() -> None:
    site_result = _site_result(200.0)  # within DEFAULT_ZERO_MARGIN_W of zero
    request = _charger(requested_current_a=8.0, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.reason == "near_zero_crossing_kept_unchanged"
    assert decision.proposed_current_a == 10.0


def test_boundary_an_otherwise_confirmed_safe_decrease_still_needs_fresh_measurement() -> None:
    """Freshness gates the classification: a stale `signed_active_power_w` is not confirmed safe."""
    site_result = _site_result(1000.0, ages=(MAX_AGE + 10.0, MAX_AGE + 10.0, MAX_AGE + 10.0))
    delta_a = 500.0 / VOLTAGE
    request = _charger(requested_current_a=10.0 - delta_a, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.proposed_current_a is None
    assert decision.reason == "phase_measurement_unusable"


def test_boundary_without_consecutive_confirmation_a_decrease_is_not_confirmed_safe() -> None:
    """An unconfirmed direction is not treated as confirmed safe even when the math would allow it."""
    site_result = _site_result(1000.0)
    delta_a = 500.0 / VOLTAGE
    request = _charger(requested_current_a=10.0 - delta_a, measured_current_a=10.0)

    decision = _decide(site_result, request, confirmed=None)

    assert decision.reason == "insufficient_consecutive_confirmation_kept_unchanged"
    assert decision.proposed_current_a == 10.0


def test_a_new_household_load_pushing_the_phase_over_the_fuse_is_not_no_change_requested() -> None:
    """Requested and measured current both 16 A, but a new household load pushes the phase over the fuse: never "no_change_requested"."""
    # Total ~21 A against a 20 A fuse with 1 A margin: margin = -2 A, an active overload.
    site_result = _site_result(21.0 * VOLTAGE)
    request = _charger(requested_current_a=16.0, measured_current_a=16.0)

    decision = _decide(site_result, request)

    assert decision.reason != "no_change_requested"
    assert decision.reason == "reducing_current_due_to_active_import_overload"
    # A genuinely lower regulator current, never a confident "keep it at 16 A".
    assert decision.proposed_current_a is not None
    assert decision.proposed_current_a < 16.0
    assert decision.proposed_current_a == 14.0  # 16 + (19 - 21) = 14


def test_import_overload_proposes_a_lower_current_even_if_that_is_all_that_was_asked() -> None:
    """A request already below the emergency ceiling is granted as asked."""
    site_result = _site_result(21.0 * VOLTAGE)
    request = _charger(requested_current_a=10.0, measured_current_a=16.0)

    decision = _decide(site_result, request)

    assert decision.reason == "reducing_current_due_to_active_import_overload"
    assert decision.proposed_current_a == 10.0


def test_export_overload_never_proposes_keeping_the_current_as_safe() -> None:
    """Active overload on the export side: reducing import would worsen it, so the answer is `None`."""
    # Total ~-20 A against a 19 A effective fuse: a 1 A overload on the export side.
    site_result = _site_result(-20.0 * VOLTAGE)
    request = _charger(requested_current_a=16.0, measured_current_a=16.0)

    decision = _decide(site_result, request, confirmed="net_export")

    assert decision.proposed_current_a is None
    assert decision.reason == "active_export_overload_no_reliable_action"
    # Not any phrasing that reads as "16 A is safe right now".
    assert "kept_unchanged" not in decision.reason


def test_overload_with_unclear_direction_is_no_reliable_action_not_a_guess() -> None:
    """Overload with a near-zero-crossing direction must resolve honestly rather than guess a direction."""
    # Large magnitude with P near zero (a reactive load); built directly because Q must carry the magnitude.
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(p=100.0, q=21.0 * VOLTAGE, v=VOLTAGE),
        l2=_derived_input(p=100.0, q=21.0 * VOLTAGE, v=VOLTAGE),
        l3=_derived_input(p=100.0, q=21.0 * VOLTAGE, v=VOLTAGE),
    )
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=MAIN_FUSE_A,
        safety_margin_a=1.0,
        measurement_mode="derived_phase_current",  # type: ignore[arg-type]
        max_age_s=MAX_AGE,
        derived=derived,
    )
    site_result = calculate_site_capacity(config, [])
    request = _charger(requested_current_a=16.0, measured_current_a=16.0)

    decision = _decide(site_result, request)

    assert decision.proposed_current_a is None
    assert decision.reason == "active_overload_direction_unclear_no_reliable_action"


def test_no_change_requested_is_reported_directly() -> None:
    site_result = _site_result(2000.0)
    request = _charger(requested_current_a=10.0, measured_current_a=10.0)

    decision = _decide(site_result, request)

    assert decision.reason == "no_change_requested"
    assert decision.proposed_current_a == 10.0


def test_direct_mode_is_not_supported() -> None:
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=MAIN_FUSE_A,
        safety_margin_a=1.0,
        measurement_mode="direct_phase_current",  # type: ignore[arg-type]
        max_age_s=MAX_AGE,
        direct=DirectPhaseMeasurement(
            l1=PhaseValue(5.0, HEALTHY_AGE),
            l2=PhaseValue(5.0, HEALTHY_AGE),
            l3=PhaseValue(5.0, HEALTHY_AGE),
        ),
    )
    site_result = calculate_site_capacity(config, [])
    request = _charger(requested_current_a=10.0, measured_current_a=6.0)

    decision = _decide(site_result, request)

    assert decision.proposed_current_a is None
    assert decision.reason == "derived_mode_required"


def test_requested_current_unknown_is_undetermined() -> None:
    site_result = _site_result(2000.0)
    request = _charger(requested_current_a=None, measured_current_a=6.0)

    decision = _decide(site_result, request)

    assert decision.proposed_current_a is None
    assert decision.reason == "requested_current_unknown"


def test_unknown_single_phase_wiring_is_undetermined() -> None:
    site_result = _site_result(2000.0)
    request = ChargerRequest(
        charger_entry_id="charger_single",
        requested_current_a=10.0,
        phases=1,
        phase=None,
        measured_current_a=DirectPhaseMeasurement(
            l1=PhaseValue(6.0, HEALTHY_AGE),
            l2=PhaseValue(None, None, problem="missing"),
            l3=PhaseValue(None, None, problem="missing"),
        ),
    )

    decision = _decide(site_result, request)

    assert decision.proposed_current_a is None
    assert decision.reason == "charger_phase_wiring_unknown"


def test_decision_basis_carries_the_measurements_it_was_built_on() -> None:
    site_result = _site_result(2000.0)
    request = _charger(requested_current_a=16.0, measured_current_a=6.0)

    decision = _decide(site_result, request)

    for phase in PHASES:
        entry = decision.basis[phase]
        assert entry.measured_current_a == 6.0
        assert entry.requested_current_a == 16.0
        assert entry.signed_active_power_w == 2000.0
        assert entry.direction == "net_import"


def test_increase_cap_landing_below_the_minimum_current_is_an_explicit_pause() -> None:
    """Increase-cap branch: 3 A of headroom is below the 6 A minimum, so the answer is an explicit pause (0.0 A), not a raw 3.0 A."""
    site_result = _site_result(16.0 * VOLTAGE)  # measured_margin_a = 19 - 16 = 3A
    request = _charger(requested_current_a=10.0, measured_current_a=0.0, min_current_a=6.0)

    decision = _decide(site_result, request)

    assert decision.reason == "paused_safe_current_below_charger_minimum"
    assert decision.proposed_current_a == 0.0


def test_active_import_overload_reduction_landing_below_the_minimum_is_an_explicit_pause() -> None:
    """Active-import-overload reduction branch: a ceiling between 0 and the minimum is also an explicit pause."""
    site_result = _site_result(24.0 * VOLTAGE)  # measured_margin_a = 19 - 24 = -5A
    request = _charger(requested_current_a=8.0, measured_current_a=8.0, min_current_a=6.0)

    decision = _decide(site_result, request)

    assert decision.reason == "paused_safe_current_below_charger_minimum"
    assert decision.proposed_current_a == 0.0


def test_confirmed_safe_decrease_landing_below_the_minimum_is_an_explicit_pause() -> None:
    """Confirmed-safe decrease branch: a requested value below the minimum is not a legal setpoint either."""
    # P=4000 W/phase keeps the margin positive (not overloaded) yet within the help margin for a 7 A reduction.
    site_result = _site_result(4000.0)
    request = _charger(requested_current_a=3.0, measured_current_a=10.0, min_current_a=6.0)

    decision = _decide(site_result, request, confirmed="net_import")

    assert decision.reason == "paused_safe_current_below_charger_minimum"
    assert decision.proposed_current_a == 0.0


def test_combining_already_legal_per_phase_values_cannot_reintroduce_a_sub_minimum_value() -> None:
    """The phase-combination step never invents a sub-minimum value: `min()`/`max()` only pick already-legalized per-phase values."""
    site_result = _site_result(16.0 * VOLTAGE, 2000.0, 2000.0)
    measured = DirectPhaseMeasurement(
        l1=PhaseValue(0.0, HEALTHY_AGE),
        l2=PhaseValue(0.0, HEALTHY_AGE),
        l3=PhaseValue(0.0, HEALTHY_AGE),
    )
    request = ChargerRequest(
        charger_entry_id="charger_combo",
        requested_current_a=10.0,
        phases=3,
        min_current_a=6.0,
        measured_current_a=measured,
    )

    decision = _decide(site_result, request)

    assert decision.limiting_phase == "L1"
    assert decision.proposed_current_a == 0.0
    assert decision.reason == "paused_safe_current_below_charger_minimum"


def test_a_legal_setpoint_at_or_above_the_minimum_is_never_paused() -> None:
    """A ceiling exactly at or above the minimum is untouched and never reclassified as a pause."""
    # Ceiling exactly at the minimum (6 A): 6 A of headroom on a 0 A draw.
    site_result_at_min = _site_result(13.0 * VOLTAGE)  # measured_margin_a = 19 - 13 = 6A
    request_at_min = _charger(requested_current_a=10.0, measured_current_a=0.0, min_current_a=6.0)
    decision_at_min = _decide(site_result_at_min, request_at_min)
    assert decision_at_min.reason == "increase_within_safe_uncredited_margin"
    assert decision_at_min.proposed_current_a == 6.0

    # Comfortably above the minimum.
    site_result_above = _site_result(3500.0)
    request_above = _charger(requested_current_a=16.0, measured_current_a=6.0, min_current_a=6.0)
    decision_above = _decide(site_result_above, request_above)
    assert decision_above.reason == "increase_within_safe_uncredited_margin"
    assert decision_above.proposed_current_a == 9.0


# Two chargers sharing a phase must never each be offered the same headroom:
# `decide_charging_current` sees a site-wide margin that already reflects
# every charger, so `allocate_regulator_decisions` processes chargers in
# `charger_entry_id` order against a shared remaining-headroom dict, as
# `site_capacity.allocate_chargers` does.


def _named_charger(
    entry_id: str,
    requested_current_a: float | None,
    measured_current_a: float | None,
    *,
    phases: int = 3,
    phase: str | None = None,
    measured_age: float = HEALTHY_AGE,
    min_current_a: float = 6.0,
) -> ChargerRequest:
    """A `ChargerRequest` with an explicit entry id and, for single-phase chargers, an explicit phase."""
    if measured_current_a is None:
        measured = None
    else:
        measured = DirectPhaseMeasurement(
            l1=PhaseValue(measured_current_a, measured_age),
            l2=PhaseValue(measured_current_a, measured_age),
            l3=PhaseValue(measured_current_a, measured_age),
        )
    return ChargerRequest(
        charger_entry_id=entry_id,
        requested_current_a=requested_current_a,
        phases=phases,  # type: ignore[arg-type]
        phase=phase,  # type: ignore[arg-type]
        min_current_a=min_current_a,
        measured_current_a=measured,
    )


def _allocate(
    site_result,
    requests,
    *,
    confirmed: str | None = "net_import",
) -> dict[str, RegulatorDecision]:
    return allocate_regulator_decisions(
        site_result=site_result,
        requests=requests,
        voltage_by_phase=_voltage_by_phase(),
        confirmed_direction_by_phase=_confirmed(confirmed),
        max_age_s=MAX_AGE,
    )


def test_two_chargers_sharing_a_phase_do_not_each_claim_the_same_headroom() -> None:
    """Two off chargers both requesting 16 A against 11 A/phase of headroom: the first takes 11 A, the second sees 0 A and pauses."""
    site_result = _site_result(8.0 * VOLTAGE)  # measured_margin_a = 19 - 8 = 11A

    # The naive per-charger result, computed to pin the before/after difference.
    naive_a = _decide(site_result, _named_charger("charger_a", 16.0, 0.0))
    naive_b = _decide(site_result, _named_charger("charger_b", 16.0, 0.0))
    assert naive_a.proposed_current_a == 11.0
    assert naive_b.proposed_current_a == 11.0  # the same 11A, twice over

    decisions = _allocate(
        site_result,
        [_named_charger("charger_b", 16.0, 0.0), _named_charger("charger_a", 16.0, 0.0)],
    )

    assert decisions["charger_a"].proposed_current_a == 11.0
    # The second charger must NOT also receive 11A.
    assert decisions["charger_b"].proposed_current_a != 11.0
    # 0 A is left after A's claim, so B's ceiling is exactly 0.0, which keeps its natural reason; only a value strictly between 0 and the minimum is relabeled.
    assert decisions["charger_b"].proposed_current_a == 0.0


def test_a_decrease_on_one_charger_frees_headroom_for_a_phase_sharing_sibling() -> None:
    """Charger A's sub-minimum confirmed-safe decrease becomes a pause and frees its 10 A; B on the same phase sees that freed current."""
    # 15 A/phase total gives a 4 A margin; P=3450 W keeps the site non-overloaded and satisfies the help margin for A's 7 A reduction.
    site_result = _site_result(15.0 * VOLTAGE)
    charger_a = _named_charger("charger_a", 3.0, 10.0, min_current_a=6.0)
    charger_b = _named_charger("charger_b", 16.0, 0.0, min_current_a=6.0)

    decisions = _allocate(site_result, [charger_a, charger_b])

    # A (first by id): its sub-minimum decrease becomes a pause, releasing the 10 A it drew.
    assert decisions["charger_a"].proposed_current_a == 0.0
    assert decisions["charger_a"].reason == "paused_safe_current_below_charger_minimum"

    # B sees 4A (site-wide) + 10A (freed by A) = 14A, not the raw 4A.
    assert decisions["charger_b"].proposed_current_a == 14.0
    assert decisions["charger_b"].proposed_current_a > site_result.measured_margin_a["L1"]


def test_an_undetermined_charger_blocks_its_phase_sharing_sibling() -> None:
    """If charger A's proposal is `None`, its behaviour on the phase is unknown, so B gets no un-reduced headroom on any phase A touches."""
    site_result = _site_result(8.0 * VOLTAGE)  # measured_margin_a = 11A
    charger_a = _named_charger("charger_a", 16.0, None)  # unusable measurement
    charger_b = _named_charger("charger_b", 16.0, 0.0)

    decisions = _allocate(site_result, [charger_a, charger_b])

    assert decisions["charger_a"].proposed_current_a is None
    assert decisions["charger_a"].reason == "charger_measurement_unusable"
    # B must not get the untouched 11 A while a shared-phase sibling's behaviour is unknown.
    assert decisions["charger_b"].proposed_current_a is None
    assert (
        decisions["charger_b"].reason
        == "shared_phase_blocked_by_another_chargers_undetermined_decision"
    )


def test_chargers_on_independent_phases_do_not_affect_each_other() -> None:
    """Chargers with no shared phase each get exactly what they would get alone."""
    site_result = _site_result(8.0 * VOLTAGE)  # 11A/phase everywhere
    charger_l1 = _named_charger("charger_l1", 16.0, 0.0, phases=1, phase="L1")
    charger_l2 = _named_charger("charger_l2", 16.0, 0.0, phases=1, phase="L2")

    decisions = _allocate(site_result, [charger_l1, charger_l2])

    assert decisions["charger_l1"].proposed_current_a == 11.0
    assert decisions["charger_l2"].proposed_current_a == 11.0  # unaffected by L1's claim


def test_each_chargers_decision_reflects_only_its_own_inputs() -> None:
    """Chargers' request, measurement and `basis` fields are never cross-contaminated in the result."""
    site_result = _site_result(8.0 * VOLTAGE)
    charger_a = _named_charger("charger_a", 16.0, 6.0, phases=1, phase="L1")
    charger_b = _named_charger("charger_b", 10.0, 8.0, phases=1, phase="L2")

    decisions = _allocate(site_result, [charger_a, charger_b])

    assert decisions["charger_a"].basis["L1"].requested_current_a == 16.0
    assert decisions["charger_a"].basis["L1"].measured_current_a == 6.0
    assert decisions["charger_b"].basis["L2"].requested_current_a == 10.0
    assert decisions["charger_b"].basis["L2"].measured_current_a == 8.0
    # No leakage: A's basis has no L2 entry, B's none on L1.
    assert "L2" not in decisions["charger_a"].basis
    assert "L1" not in decisions["charger_b"].basis


def test_allocation_is_order_independent_for_a_shared_phase() -> None:
    """The result is independent of input order (allocation runs in ascending `charger_entry_id`)."""
    site_result = _site_result(8.0 * VOLTAGE)
    charger_a = _named_charger("charger_a", 16.0, 0.0)
    charger_b = _named_charger("charger_b", 16.0, 0.0)

    forward = _allocate(site_result, [charger_a, charger_b])
    reversed_ = _allocate(site_result, [charger_b, charger_a])

    assert forward["charger_a"].proposed_current_a == reversed_["charger_a"].proposed_current_a
    assert forward["charger_b"].proposed_current_a == reversed_["charger_b"].proposed_current_a


def test_an_increase_on_one_charger_reduces_a_siblings_available_increase() -> None:
    """A grant to A reduces the shared headroom B sees: B ends up with strictly less than it would alone."""
    site_result = _site_result(8.0 * VOLTAGE)  # 11A/phase
    charger_a = _named_charger("charger_a", 16.0, 6.0)  # increase 6->?
    charger_b = _named_charger("charger_b", 16.0, 0.0)

    decisions = _allocate(site_result, [charger_a, charger_b])

    # A: cap = 6 + 11 = 17, min(16, 17) = 16 -> granted 16, reserving
    # 16-6 = 10A of the shared headroom.
    assert decisions["charger_a"].proposed_current_a == 16.0
    # B sees 11 - 10 = 1A of remaining headroom -> below its 6A minimum.
    assert decisions["charger_b"].proposed_current_a == 0.0
    assert decisions["charger_b"].reason == "paused_safe_current_below_charger_minimum"


# Apply-path re-check (`decision_still_applyable`): every charger write is gated on a fresh reading, with no Home Assistant involved. Stale, unusable, re-signed and missing readings all refuse.


def _still_applyable(decision, site_result, *, max_age_s: float = MAX_AGE) -> bool:
    """The call `SiteCapacityController` makes, against a real result's signed-power maps."""
    return decision_still_applyable(
        decision=decision,
        signed_active_power_w=site_result.phase_signed_active_power_w,
        signed_active_power_age_s=site_result.phase_signed_active_power_age_s,
        signed_active_power_reason=site_result.phase_signed_active_power_reason,
        max_age_s=max_age_s,
    )


def _healthy_importing_decision() -> tuple[RegulatorDecision, object]:
    """An ordinary proposal at an importing site stays applyable."""
    site_result = _site_result(8.0 * VOLTAGE)  # ~8A/phase of real headroom
    return _decide(site_result, _charger(16.0, 6.0)), site_result


def test_decision_still_applyable_for_a_fresh_signed_same_direction_reading() -> None:
    decision, site_result = _healthy_importing_decision()

    assert decision.proposed_current_a is not None
    assert _still_applyable(decision, site_result) is True


def test_decision_still_applyable_refuses_a_reading_that_has_gone_stale() -> None:
    """The same decision re-checked against stale copies of the phases: unchanged numbers with older ages must refuse."""
    decision, site_result = _healthy_importing_decision()
    assert _still_applyable(decision, site_result) is True

    stale_result = _site_result(8.0 * VOLTAGE, ages=(MAX_AGE + 60.0,) * 3)

    assert _still_applyable(decision, stale_result) is False


def test_decision_still_applyable_refuses_a_reading_carrying_a_usability_reason() -> None:
    """A `missing`/`invalid` reason on any single phase the decision drew on refuses."""
    decision, _ = _healthy_importing_decision()

    for phase in PHASES:
        assert (
            decision_still_applyable(
                decision=decision,
                signed_active_power_w={p: 8.0 * VOLTAGE for p in PHASES},
                signed_active_power_age_s={p: HEALTHY_AGE for p in PHASES},
                signed_active_power_reason={
                    p: ("invalid" if p == phase else None) for p in PHASES
                },
                max_age_s=MAX_AGE,
            )
            is False
        ), phase


def test_decision_still_applyable_refuses_a_sign_change_or_a_missing_phase() -> None:
    decision, site_result = _healthy_importing_decision()
    values = dict(site_result.phase_signed_active_power_w)
    ages = dict(site_result.phase_signed_active_power_age_s)
    reasons = dict(site_result.phase_signed_active_power_reason)

    def check(power: dict, age: dict | None = None, reason: dict | None = None) -> bool:
        return decision_still_applyable(
            decision=decision,
            signed_active_power_w=power,
            signed_active_power_age_s=ages if age is None else age,
            signed_active_power_reason=reasons if reason is None else reason,
            max_age_s=MAX_AGE,
        )

    assert check(values) is True
    # Same direction, different magnitude: still the snapshot it assumed.
    assert check({**values, "L1": values["L1"] - 200.0}) is True
    # Flipped into export, and flattened into the zero band: both refuse.
    assert check({**values, "L1": -abs(values["L1"]) - 1000.0}) is False
    assert check({**values, "L1": 0.0}) is False
    # A phase that has disappeared, or has no value: refuse, never assume.
    assert check({p: v for p, v in values.items() if p != "L1"}) is False
    assert check({**values, "L1": None}) is False
    # Unknown age, too: `None` is not "fresh enough".
    assert check(values, age={**ages, "L1": None}) is False


def test_decision_still_applyable_refuses_a_refusal_or_a_basisless_decision() -> None:
    """`None` is never a value to write, and a decision about no phase is not writable."""
    site_result = _site_result(8.0 * VOLTAGE)
    refusal = RegulatorDecision(
        proposed_current_a=None,
        reason="phase_measurement_unusable",
        limiting_phase="L1",
    )
    basisless = RegulatorDecision(
        proposed_current_a=10.0,
        reason="increase_within_safe_uncredited_margin",
        limiting_phase="L1",
    )

    assert _still_applyable(refusal, site_result) is False
    assert _still_applyable(basisless, site_result) is False


def test_a_per_phase_export_with_an_importing_charger_is_never_reduced() -> None:
    """A charger importing on an exporting phase with a dropped request: the decision must never be that reduction."""
    site_result = _site_result(-4100.0)  # ~-17.8A/phase at 230V: battery export
    decision = _decide(site_result, _charger(6.0, 10.0))

    assert decision.proposed_current_a != 6.0
    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"


def test_the_28a_export_10a_ev_counterexample_is_still_refused_a_reduction() -> None:
    """Rest of site exports 28 A while the EV imports 10 A (total -18 A): a credited reading would call a reduction safe; the decision must not."""
    site_result = _site_result(-(28.0 - 10.0) * VOLTAGE)
    decision = _decide(site_result, _charger(6.0, 10.0))

    assert decision.proposed_current_a != 6.0
    assert decision.reason == "decrease_would_increase_net_current_kept_unchanged"


def test_a_stale_phase_refuses_any_modulation() -> None:
    """One unusable phase in the charger's set gives `None` with a reason; a stale signed reading looks like that live."""
    site_result = _site_result(8.0 * VOLTAGE, ages=(HEALTHY_AGE, MAX_AGE + 60.0, HEALTHY_AGE))
    decision = _decide(site_result, _charger(16.0, 6.0))

    assert decision.proposed_current_a is None
    assert decision.reason == "phase_measurement_unusable"


def test_applyability_failure_names_the_check_that_stopped_a_write() -> None:
    """`decision_still_applyable` as a reason: every condition has its own code, per-phase ones name their phase, and the boolean agrees."""
    decision, site_result = _healthy_importing_decision()
    values = dict(site_result.phase_signed_active_power_w)
    ages = dict(site_result.phase_signed_active_power_age_s)
    reasons = dict(site_result.phase_signed_active_power_reason)

    def failure(power=None, age=None, reason=None, decided=None):
        decided = decision if decided is None else decided
        return applyability_failure(
            decision=decided,
            signed_active_power_w=values if power is None else power,
            signed_active_power_age_s=ages if age is None else age,
            signed_active_power_reason=reasons if reason is None else reason,
            max_age_s=MAX_AGE,
        )

    assert failure() is None

    observed = {
        "no_proposal": failure(
            decided=RegulatorDecision(
                proposed_current_a=None,
                reason="phase_measurement_unusable",
                limiting_phase="L1",
            )
        ),
        "no_basis": failure(
            decided=RegulatorDecision(
                proposed_current_a=10.0,
                reason="increase_within_safe_uncredited_margin",
                limiting_phase="L1",
            )
        ),
        "phase_unusable": failure(reason={**reasons, "L1": "invalid"}),
        "phase_value_missing": failure(power={**values, "L1": None}),
        "phase_age_unknown": failure(age={**ages, "L1": None}),
        "phase_stale": failure(age={**ages, "L1": MAX_AGE + 60.0}),
        "direction_changed": failure(power={**values, "L1": -abs(values["L1"]) - 1000.0}),
    }

    for expected, got in observed.items():
        assert got is not None, expected
        assert got.kind == expected, expected

    # Per-phase checks name their phase; the two decision-level ones cannot.
    for kind in ("phase_unusable", "phase_value_missing", "phase_age_unknown", "phase_stale"):
        assert observed[kind].phase == "L1", kind
    for kind in ("no_proposal", "no_basis"):
        assert observed[kind].phase is None, kind

    # And the boolean is *exactly* "no failure", at every one of these inputs:
    # the two can never disagree about the same reading.
    for power, age in (
        (values, ages),
        ({**values, "L1": None}, ages),
        (values, {**ages, "L1": MAX_AGE + 60.0}),
        ({**values, "L1": -abs(values["L1"]) - 1000.0}, ages),
    ):
        assert decision_still_applyable(
            decision=decision,
            signed_active_power_w=power,
            signed_active_power_age_s=age,
            signed_active_power_reason=reasons,
            max_age_s=MAX_AGE,
        ) is (
            applyability_failure(
                decision=decision,
                signed_active_power_w=power,
                signed_active_power_age_s=age,
                signed_active_power_reason=reasons,
                max_age_s=MAX_AGE,
            )
            is None
        )
