"""Pure site-capacity calculation: value objects in, `calculate_site_capacity` result out, no `hass` fixture."""

from __future__ import annotations

import math

from custom_components.spotnav.site.site_capacity import (
    ACTIVE_CONTROL_READY,
    CONFIRMED_UNCHANGED_MARGIN_A,
    MEASUREMENT_TOLERANCE_A,
    PHASES,
    REASON_STALE,
    ChargerRequest,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PhaseValue,
    SiteCalculationConfig,
    apparent_current_from_derived,
    calculate_site_capacity,
    classify_current,
    classify_phase_liveness,
    classify_power,
    classify_voltage,
    headroom_with_liveness_margin,
    normalize_power,
    normalize_voltage,
    round_down_to_step,
)

HEALTHY_AGE = 5.0
MAX_AGE = 120.0


def _direct(l1: float, l2: float, l3: float, age: float = HEALTHY_AGE) -> DirectPhaseMeasurement:
    return DirectPhaseMeasurement(
        l1=PhaseValue(l1, age),
        l2=PhaseValue(l2, age),
        l3=PhaseValue(l3, age),
    )


def _config(
    *,
    main_fuse_a: float | None = 25.0,
    safety_margin_a: float = 1.0,
    direct: DirectPhaseMeasurement | None = None,
    derived: DerivedPhaseMeasurement | None = None,
    enabled: bool = True,
    max_age_s: float = MAX_AGE,
    mode: str = "direct_phase_current",
) -> SiteCalculationConfig:
    return SiteCalculationConfig(
        enabled=enabled,
        main_fuse_a=main_fuse_a,
        safety_margin_a=safety_margin_a,
        measurement_mode=mode,  # type: ignore[arg-type]
        max_age_s=max_age_s,
        direct=direct,
        derived=derived,
    )


def _charger(
    charger_entry_id: str = "charger_a",
    *,
    requested_current_a: float = 16.0,
    phases: int = 3,
    phase: str | None = None,
    min_current_a: float = 6.0,
    measured_current_a: DirectPhaseMeasurement | None = None,
    priority: str = "normal",
    order: int = 0,
) -> ChargerRequest:
    return ChargerRequest(
        charger_entry_id=charger_entry_id,
        requested_current_a=requested_current_a,
        phases=phases,  # type: ignore[arg-type]
        phase=phase,  # type: ignore[arg-type]
        min_current_a=min_current_a,
        measured_current_a=measured_current_a,
        priority=priority,
        order=order,
    )


def test_balanced_three_phase_load_gives_equal_headroom_on_every_phase() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "observing"
    assert result.phase_headroom_a == {"L1": 19.0, "L2": 19.0, "L3": 19.0}
    assert result.limiting_phase in ("L1", "L2", "L3")  # tied; any is a valid pick


def test_unbalanced_three_phase_load_gives_different_headroom_per_phase() -> None:
    config = _config(direct=_direct(2.0, 8.0, 4.0))
    result = calculate_site_capacity(config, [_charger()])

    assert result.phase_headroom_a == {"L1": 22.0, "L2": 16.0, "L3": 20.0}


def test_most_loaded_phase_limits_a_three_phase_chargers_proposal() -> None:
    # Ceiling 24 A on every phase; L2 has only 10 A of headroom, so it limits a 16 A request.
    config = _config(direct=_direct(2.0, 14.0, 2.0))
    result = calculate_site_capacity(config, [_charger(requested_current_a=16.0)])

    allocation = result.allocations[0]
    assert allocation.proposed_current_a == 10.0
    assert allocation.limiting_phase == "L2"
    assert allocation.state == "capacity_limited"


def test_safety_margin_reduces_headroom_below_the_nominal_fuse_value() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=3.0, direct=_direct(0.0, 0.0, 0.0))
    result = calculate_site_capacity(config, [])

    assert result.phase_headroom_a == {"L1": 22.0, "L2": 22.0, "L3": 22.0}


def test_zero_safety_margin_is_allowed_and_uses_the_full_fuse_rating() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=0.0, direct=_direct(0.0, 0.0, 0.0))
    result = calculate_site_capacity(config, [])

    assert result.phase_headroom_a == {"L1": 25.0, "L2": 25.0, "L3": 25.0}


# The grid sensor already includes the charger's own draw: avoid double subtraction.


def test_chargers_own_measured_current_is_credited_back_exactly_once_in_the_diagnostic() -> None:
    # Site meter reads 15 A on L1 (5 A other load + 10 A charger). Crediting the charger once would give 19 A available,
    # not 24-15=9 A and not 24-15-10=-1 A. That credited value is `calculated_headroom_after_ev_credit_a`, diagnostic only;
    # `phase_headroom_a` and the proposal stay at the uncredited 24-15=9 A.
    measured = _direct(10.0, 10.0, 10.0)
    config = _config(direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(requested_current_a=19.0, measured_current_a=measured, min_current_a=0.0)

    result = calculate_site_capacity(config, [charger])

    assert result.calculated_headroom_after_ev_credit_a == {"L1": 19.0, "L2": 19.0, "L3": 19.0}
    # The real headroom never credits the charger's own current, in direct mode or derived mode.
    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}
    assert result.phase_headroom_a == result.measured_margin_a
    assert result.allocations[0].proposed_current_a == 9.0
    assert result.allocations[0].state == "capacity_limited"


def test_missing_measured_current_is_the_safe_default_not_a_credit() -> None:
    # Same 15 A total but the charger's current feedback is unknown: all 15 A is other load (undercount, never overcount).
    config = _config(direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(requested_current_a=19.0, measured_current_a=None)

    result = calculate_site_capacity(config, [charger])

    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}
    assert result.allocations[0].proposed_current_a == 9.0
    assert result.allocations[0].state == "capacity_limited"


def test_a_known_setpoint_never_substitutes_for_an_unknown_measured_current() -> None:
    # A commanded setpoint with no measured feedback credits nothing: a setpoint is not proof of a draw.
    config = _config(direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(requested_current_a=19.0, measured_current_a=None)  # 16A setpoint is not modeled here at all

    result = calculate_site_capacity(config, [charger])

    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}


def test_charger_on_but_actual_draw_could_be_zero_never_overestimates_headroom() -> None:
    # Charger "on" but possibly drawing 0 A (modeled as no measured current): the conservative no-credit case.
    config = _config(direct=_direct(20.0, 20.0, 20.0))
    charger = _charger(requested_current_a=10.0, measured_current_a=None)

    result = calculate_site_capacity(config, [charger])

    # Other load is the full measured 20 A (no credit), leaving 4 A of headroom.
    assert result.phase_headroom_a == {"L1": 4.0, "L2": 4.0, "L3": 4.0}


def test_a_charger_reading_above_the_site_total_is_never_credited_and_only_warns_on_a_magnitude_source() -> None:
    # A magnitude-only source cannot tell export from import, so the charger is treated as additive on
    # |site|: the reading is not refused, nothing is credited, and the odd reading is reported.
    measured = _direct(999.0, 999.0, 999.0)
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(requested_current_a=30.0, measured_current_a=measured)

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}
    assert result.allocations[0].proposed_current_a == 9.0
    assert result.charger_reading_exceeds_site_phases == PHASES


def test_safety_invariant_other_load_per_phase_is_never_negative() -> None:
    """`other_load` values never go negative; a wildly overstated credit lands in `inconsistent_phases` instead."""
    from custom_components.spotnav.site.site_capacity import other_load_per_phase

    for measured_total in (0.0, 5.0, 15.0, 100.0):
        for credited in (0.0, 10.0, 1000.0):
            charger = _charger(
                measured_current_a=_direct(credited, credited, credited, age=HEALTHY_AGE)
            )
            other, inconsistent = other_load_per_phase(
                {"L1": measured_total, "L2": measured_total, "L3": measured_total},
                [charger],
                MAX_AGE,
            )
            assert all(value >= 0.0 for value in other.values())
            if credited > measured_total + MEASUREMENT_TOLERANCE_A:
                assert inconsistent == frozenset(PHASES)
            else:
                assert inconsistent == frozenset()


def test_safety_invariant_accepted_credits_per_phase_never_exceed_measured_site_total() -> None:
    """On a phase not flagged inconsistent, the sum of credited charger currents never exceeds the site total (within tolerance)."""
    from custom_components.spotnav.site.site_capacity import other_load_per_phase

    for measured_total in (0.0, 10.0, 25.0):
        for per_charger in (0.0, 3.0, 8.0):
            chargers = [
                _charger("charger_a", measured_current_a=_direct(per_charger, per_charger, per_charger)),
                _charger("charger_b", measured_current_a=_direct(per_charger, per_charger, per_charger)),
            ]
            other, inconsistent = other_load_per_phase(
                {"L1": measured_total, "L2": measured_total, "L3": measured_total},
                chargers,
                MAX_AGE,
            )
            total_credited = 2 * per_charger
            if "L1" not in inconsistent:
                assert total_credited <= measured_total + MEASUREMENT_TOLERANCE_A
                assert other["L1"] == max(0.0, measured_total - total_credited)
            else:
                assert total_credited > measured_total + MEASUREMENT_TOLERANCE_A


def test_stale_measured_current_is_not_credited() -> None:
    measured = DirectPhaseMeasurement(
        l1=PhaseValue(10.0, age_s=MAX_AGE + 10.0),
        l2=PhaseValue(10.0, age_s=MAX_AGE + 10.0),
        l3=PhaseValue(10.0, age_s=MAX_AGE + 10.0),
    )
    config = _config(direct=_direct(15.0, 15.0, 15.0), max_age_s=MAX_AGE)
    charger = _charger(requested_current_a=19.0, measured_current_a=measured)

    result = calculate_site_capacity(config, [charger])

    # No credit: treated like "not measured at all".
    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}


def test_three_phase_charger_measurement_credit_is_all_or_nothing() -> None:
    # L1/L2 fine but L3 stale: a three-phase charger's credit is all-or-nothing, since partial credit would understate its draw.
    measured = DirectPhaseMeasurement(
        l1=PhaseValue(10.0, age_s=HEALTHY_AGE),
        l2=PhaseValue(10.0, age_s=HEALTHY_AGE),
        l3=PhaseValue(10.0, age_s=MAX_AGE + 10.0),  # stale
    )
    config = _config(direct=_direct(15.0, 15.0, 15.0), max_age_s=MAX_AGE)
    charger = _charger(requested_current_a=19.0, measured_current_a=measured, phases=3)

    result = calculate_site_capacity(config, [charger])

    # No credit on any phase, not even L1/L2.
    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}


def test_single_phase_charger_measurement_only_needs_its_own_configured_phase() -> None:
    # A single-phase charger on L1 is unaffected by L2/L3 quality; checked via the diagnostic credited estimate.
    measured = DirectPhaseMeasurement(
        l1=PhaseValue(10.0, age_s=HEALTHY_AGE),
        l2=PhaseValue(None, problem="invalid"),  # irrelevant to an L1-only charger
        l3=PhaseValue(None, problem="invalid"),
    )
    config = _config(direct=_direct(15.0, 15.0, 15.0), max_age_s=MAX_AGE)
    charger = _charger(requested_current_a=19.0, measured_current_a=measured, phases=1, phase="L1")

    result = calculate_site_capacity(config, [charger])

    assert result.calculated_headroom_after_ev_credit_a["L1"] == 19.0  # 24 - (15 - 10) = 19, credited
    assert result.phase_headroom_a["L1"] == 9.0  # 24 - 15, never credited


def test_invalid_measured_current_is_not_credited() -> None:
    measured = DirectPhaseMeasurement(
        l1=PhaseValue(None, HEALTHY_AGE, problem="invalid"),
        l2=PhaseValue(None, HEALTHY_AGE, problem="invalid"),
        l3=PhaseValue(None, HEALTHY_AGE, problem="invalid"),
    )
    config = _config(direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(requested_current_a=19.0, measured_current_a=measured)

    result = calculate_site_capacity(config, [charger])

    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}


def test_derived_mode_does_not_credit_a_setpoint_based_current_either() -> None:
    # Derived mode behaves like direct mode when measured_current_a is unknown; no "credit the setpoint" shortcut.
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(3450.0, 0.0, 230.0),  # 15A apparent
        l2=_derived_input(3450.0, 0.0, 230.0),
        l3=_derived_input(3450.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")
    charger = _charger(requested_current_a=19.0, measured_current_a=None)

    result = calculate_site_capacity(config, [charger])

    for phase in ("L1", "L2", "L3"):
        assert abs(result.phase_headroom_a[phase] - 9.0) < 1e-6


def test_a_charger_with_a_known_zero_request_is_capacity_available_not_unknown() -> None:
    # A charger that is off is an explicit 0.0 and behaves normally, never as "unknown".
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    charger = _charger(requested_current_a=0.0, min_current_a=0.0)

    result = calculate_site_capacity(config, [charger])

    allocation = result.allocations[0]
    assert allocation.requested_current_a == 0.0
    assert allocation.proposed_current_a == 0.0
    assert allocation.state == "capacity_available"


def test_an_active_charger_with_unknown_requested_current_proposes_nothing() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    charger = _charger(requested_current_a=None)

    result = calculate_site_capacity(config, [charger])

    allocation = result.allocations[0]
    assert allocation.requested_current_a is None
    assert allocation.proposed_current_a is None
    assert allocation.state == "requested_current_unknown"


def test_unknown_requested_current_is_never_displayed_as_zero() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))
    charger = _charger(requested_current_a=None)

    result = calculate_site_capacity(config, [charger])

    allocation = result.allocations[0]
    assert allocation.requested_current_a != 0.0
    assert allocation.requested_current_a is None


def test_one_unknown_active_charger_blocks_a_sibling_sharing_its_phase() -> None:
    # Both three-phase, so they share every phase; the known charger's headroom is not safe to hand out while the unknown one's draw is unquantified.
    config = _config(direct=_direct(0.0, 0.0, 0.0))  # 24A headroom everywhere
    chargers = [
        _charger("charger_known", requested_current_a=10.0, min_current_a=1.0),
        _charger("charger_unknown", requested_current_a=None),
    ]

    result = calculate_site_capacity(config, chargers)
    by_id = {a.charger_entry_id: a for a in result.allocations}

    assert by_id["charger_unknown"].state == "requested_current_unknown"
    assert by_id["charger_unknown"].proposed_current_a is None
    # The known charger, sharing every phase with the unknown one, also gets no proposal.
    assert by_id["charger_known"].state == "requested_current_unknown"
    assert by_id["charger_known"].proposed_current_a is None


def test_an_unknown_charger_on_one_phase_does_not_block_a_charger_on_a_different_phase() -> None:
    config = _config(direct=_direct(0.0, 0.0, 0.0))
    chargers = [
        _charger("charger_l1_unknown", requested_current_a=None, phases=1, phase="L1"),
        _charger("charger_l2_known", requested_current_a=10.0, phases=1, phase="L2", min_current_a=1.0),
    ]

    result = calculate_site_capacity(config, chargers)
    by_id = {a.charger_entry_id: a for a in result.allocations}

    assert by_id["charger_l1_unknown"].proposed_current_a is None
    # L2 shares no phase with the unknown L1 charger, so it is unaffected.
    assert by_id["charger_l2_known"].proposed_current_a == 10.0
    assert by_id["charger_l2_known"].state == "capacity_available"


def test_one_chargers_measurement_exceeding_site_total_is_conservative_not_invalid() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(measured_current_a=_direct(20.0, 20.0, 20.0))  # 20A > 15A measured total

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    assert result.allocations[0].proposed_current_a == 9.0  # 25 - 1 - |15|, the charger additive
    assert result.charger_reading_exceeds_site_phases == PHASES


def test_two_chargers_combined_measurement_exceeding_site_total_only_warns() -> None:
    # Neither charger alone exceeds the 15 A total but their sum (16 A) does.
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    chargers = [
        _charger("charger_a", measured_current_a=_direct(8.0, 8.0, 8.0)),
        _charger("charger_b", measured_current_a=_direct(8.0, 8.0, 8.0)),
    ]

    result = calculate_site_capacity(config, chargers)

    assert result.state == "observing"
    assert result.charger_reading_exceeds_site_phases == PHASES


def test_combined_measurement_exactly_equal_to_site_total_is_accepted() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    charger = _charger(requested_current_a=5.0, measured_current_a=_direct(15.0, 15.0, 15.0))

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    # The credited diagnostic reaches the site ceiling (other_load = 0); the real headroom is ceiling minus the full measured total.
    assert result.calculated_headroom_after_ev_credit_a == {"L1": 24.0, "L2": 24.0, "L3": 24.0}
    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}


def test_a_small_deviation_within_tolerance_is_accepted_and_credited() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    over_by_a_sliver = 15.0 + (MEASUREMENT_TOLERANCE_A / 2)
    charger = _charger(
        requested_current_a=5.0,
        measured_current_a=_direct(over_by_a_sliver, over_by_a_sliver, over_by_a_sliver),
    )

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    # other_load clamps to 0, so the credited diagnostic reaches the ceiling; the real headroom does not.
    assert result.calculated_headroom_after_ev_credit_a == {"L1": 24.0, "L2": 24.0, "L3": 24.0}
    assert result.phase_headroom_a == {"L1": 9.0, "L2": 9.0, "L3": 9.0}


def test_a_deviation_just_beyond_tolerance_only_warns() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    over_the_tolerance = 15.0 + MEASUREMENT_TOLERANCE_A + 0.01
    charger = _charger(
        measured_current_a=_direct(over_the_tolerance, over_the_tolerance, over_the_tolerance)
    )

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    assert result.charger_reading_exceeds_site_phases == PHASES


def test_the_charger_warning_is_independent_of_charger_order() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    chargers_forward = [
        _charger("charger_a", measured_current_a=_direct(8.0, 8.0, 8.0)),
        _charger("charger_b", measured_current_a=_direct(8.0, 8.0, 8.0)),
    ]
    chargers_backward = list(reversed(chargers_forward))

    forward = calculate_site_capacity(config, chargers_forward)
    backward = calculate_site_capacity(config, chargers_backward)

    assert forward.state == backward.state == "observing"
    assert forward.charger_reading_exceeds_site_phases == backward.charger_reading_exceeds_site_phases == PHASES


def test_the_charger_warning_names_only_the_phase_concerned() -> None:
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=_direct(15.0, 15.0, 15.0))
    measured = DirectPhaseMeasurement(
        l1=PhaseValue(20.0, HEALTHY_AGE),  # 20A > 15A total on L1 only
        l2=PhaseValue(5.0, HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE),
    )
    charger = _charger(measured_current_a=measured)

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    assert result.charger_reading_exceeds_site_phases == ("L1",)


def test_derived_mode_with_a_signed_power_source_never_invalidates_on_a_charger_credit() -> None:
    # Derived mode knows the direction, so the charger reading above the net reading is normal
    # (the rest of the site may export): no refusal and no miswiring warning.
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(3450.0, 0.0, 230.0),  # 15A apparent
        l2=_derived_input(3450.0, 0.0, 230.0),
        l3=_derived_input(3450.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")
    charger = _charger(requested_current_a=5.0, measured_current_a=_direct(999.0, 999.0, 999.0))

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    assert result.charger_reading_exceeds_site_phases == ()


def test_derived_mode_ev_credit_is_diagnostic_only_not_used_for_the_real_proposal() -> None:
    """In derived mode the real proposal stays on the uncredited margin.

    The vector-corrected EV-credited number is exposed only as
    `calculated_headroom_after_ev_credit_a`, a diagnostic that never feeds
    `phase_headroom_a` or `allocations` (crediting is not provably safe under reactive load).
    """
    voltage = 230.0
    ev_current = 8.9
    # A site total that already includes the EV's ~8.9 A draw, at unity power factor: P = V * I_total, Q = 0.
    total_current = 20.0
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(voltage * total_current, 0.0, voltage),
        l2=_derived_input(voltage * total_current, 0.0, voltage),
        l3=_derived_input(voltage * total_current, 0.0, voltage),
    )
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=None, derived=derived, mode="derived_phase_current")
    charger = _charger(
        requested_current_a=16.0,
        measured_current_a=_direct(ev_current, ev_current, ev_current),
    )

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    # The real headroom is the raw uncredited margin: 25 - 1 - 20 = 4 A, equal to `measured_margin_a`.
    for phase in PHASES:
        assert abs(result.phase_headroom_a[phase] - 4.0) < 1e-6
        assert result.phase_headroom_a[phase] == result.measured_margin_a[phase]
    allocation = result.allocations[0]
    # 4 A is below the 6 A minimum, so 0 A is proposed, not the 12 A+ the credited estimate would suggest.
    assert allocation.proposed_current_a == 0.0
    assert allocation.state == "below_minimum_current"

    # The credited estimate stays available as a diagnostic: 20 - 8.9 = 11.1 A other load, so 25 - 1 - 11.1 = 12.9 A.
    for phase in PHASES:
        assert abs(result.calculated_headroom_after_ev_credit_a[phase] - 12.9) < 1e-6


def test_derived_ev_credit_diagnostic_can_be_unsafe_under_reactive_cancellation() -> None:
    """Counterexample: a mostly-reactive household load opposing the EV's reactive contribution makes the credited estimate overstate headroom.

    `phase_headroom_a` (what `allocations` uses) stays at or below the true safe value,
    which is why the credited estimate must stay diagnostic-only in derived mode.
    """
    voltage = 230.0
    ev_current = 20.0
    cos_phi = 0.8  # a plausible EVSE power factor, not an extreme one
    sin_phi = math.sqrt(1.0 - cos_phi**2)
    p_ev_true = voltage * ev_current * cos_phi
    q_ev_true = voltage * ev_current * sin_phi
    # Household load with real power plus a reactive component that over-cancels the EV's reactive contribution (e.g. power-factor correction).
    p_house = 1500.0
    q_house = -1.5 * q_ev_true

    combined_derived = DerivedPhaseMeasurement(
        l1=_derived_input(p_house + p_ev_true, q_house + q_ev_true, voltage),
        l2=_derived_input(p_house + p_ev_true, q_house + q_ev_true, voltage),
        l3=_derived_input(p_house + p_ev_true, q_house + q_ev_true, voltage),
    )
    household_only_derived = DerivedPhaseMeasurement(
        l1=_derived_input(p_house, q_house, voltage),
        l2=_derived_input(p_house, q_house, voltage),
        l3=_derived_input(p_house, q_house, voltage),
    )
    charger = _charger(
        measured_current_a=_direct(ev_current, ev_current, ev_current), min_current_a=0.0
    )

    combined_config = _config(direct=None, derived=combined_derived, mode="derived_phase_current")
    combined_result = calculate_site_capacity(combined_config, [charger])

    household_only_config = _config(
        direct=None, derived=household_only_derived, mode="derived_phase_current"
    )
    household_only_result = calculate_site_capacity(household_only_config, [])

    # The true other-load current is what the site measures with the household load alone.
    true_other_load_current = household_only_result.measured_phase_current_a["L1"]
    true_safe_headroom = combined_config.main_fuse_a - combined_config.safety_margin_a - true_other_load_current

    # The credited diagnostic overstates headroom versus the true value: the unsafe direction.
    assert combined_result.calculated_headroom_after_ev_credit_a["L1"] > true_safe_headroom + 1.0

    # The real headroom never does; it is the raw uncredited margin.
    assert combined_result.phase_headroom_a["L1"] <= true_safe_headroom
    assert combined_result.phase_headroom_a["L1"] == combined_result.measured_margin_a["L1"]


def test_direct_mode_credited_diagnostic_is_badly_wrong_when_the_site_nets_to_export() -> None:
    """Import/export direction: 28 A of export plus the EV's 10 A of import nets to 18 A of export, which a magnitude-only sensor cannot tell from 18 A of import.

    Reducing the EV to 6 A makes net export grow to 22 A. Direct-mode crediting has
    no notion of direction, shown on the diagnostic `calculated_headroom_after_ev_credit_a`;
    the real `phase_headroom_a` does not credit the EV, so it carries no such error.
    """
    main_fuse_a = 40.0
    safety_margin_a = 1.0

    # The true other load alone, no EV: exporting 28 A, read as 28 by a magnitude-only sensor.
    other_load_alone = _config(
        main_fuse_a=main_fuse_a, safety_margin_a=safety_margin_a, direct=_direct(28.0, 28.0, 28.0)
    )
    other_load_alone_result = calculate_site_capacity(other_load_alone, [])
    true_other_load = other_load_alone_result.measured_phase_current_a["L1"]
    true_headroom = main_fuse_a - safety_margin_a - true_other_load

    # Combined: EV imports 10 A while the rest exports 28 A, net -18 A, read as 18.
    combined_10a = _config(
        main_fuse_a=main_fuse_a, safety_margin_a=safety_margin_a, direct=_direct(18.0, 18.0, 18.0)
    )
    ev_10a = _charger(measured_current_a=_direct(10.0, 10.0, 10.0), min_current_a=0.0)
    result_10a = calculate_site_capacity(combined_10a, [ev_10a])

    # Reduce the EV to 6 A: net export grows to 22 A at the meter.
    combined_6a = _config(
        main_fuse_a=main_fuse_a, safety_margin_a=safety_margin_a, direct=_direct(22.0, 22.0, 22.0)
    )
    ev_6a = _charger(measured_current_a=_direct(6.0, 6.0, 6.0), min_current_a=0.0)
    result_6a = calculate_site_capacity(combined_6a, [ev_6a])

    # The credited estimate computes other load as 18 - 10 = 8 A and 22 - 6 = 16 A, both far below the true 28 A.
    assert result_10a.calculated_headroom_after_ev_credit_a["L1"] == main_fuse_a - safety_margin_a - 8.0
    assert result_6a.calculated_headroom_after_ev_credit_a["L1"] == main_fuse_a - safety_margin_a - 16.0
    assert result_10a.calculated_headroom_after_ev_credit_a["L1"] > true_headroom + 10.0
    assert result_6a.calculated_headroom_after_ev_credit_a["L1"] > true_headroom + 5.0

    # The real headroom never credits the EV, so it is the raw margin against the unsigned meter reading in both snapshots.
    assert result_10a.phase_headroom_a["L1"] == main_fuse_a - safety_margin_a - 18.0
    assert result_6a.phase_headroom_a["L1"] == main_fuse_a - safety_margin_a - 22.0
    assert result_10a.phase_headroom_a["L1"] == result_10a.measured_margin_a["L1"]
    assert result_6a.phase_headroom_a["L1"] == result_6a.measured_margin_a["L1"]


def test_direct_mode_real_headroom_still_not_a_full_bidirectional_answer_under_export() -> None:
    """Known gap: `phase_headroom_a` still assumes the site current flows in the import direction.

    Reducing the EV's import while the site nets to export grows net export, yet
    `SiteCapacityResult` carries no signed/direction field to say so.
    """
    main_fuse_a = 40.0
    safety_margin_a = 1.0

    # 28 A of export, EV importing 10 A: net export 18 A at the meter.
    combined_10a = _config(
        main_fuse_a=main_fuse_a, safety_margin_a=safety_margin_a, direct=_direct(18.0, 18.0, 18.0)
    )
    ev_10a = _charger(measured_current_a=_direct(10.0, 10.0, 10.0), min_current_a=0.0)
    result_10a = calculate_site_capacity(combined_10a, [ev_10a])

    # Reducing the EV to 6 A of import grows net export to 22 A.
    combined_6a = _config(
        main_fuse_a=main_fuse_a, safety_margin_a=safety_margin_a, direct=_direct(22.0, 22.0, 22.0)
    )
    ev_6a = _charger(measured_current_a=_direct(6.0, 6.0, 6.0), min_current_a=0.0)
    result_6a = calculate_site_capacity(combined_6a, [ev_6a])

    # The real headroom shrinks when the EV's import is reduced, and no result field exposes direction.
    assert result_6a.phase_headroom_a["L1"] < result_10a.phase_headroom_a["L1"]


def test_active_control_readiness_gate_is_consciously_open() -> None:
    """`ACTIVE_CONTROL_READY` is a deliberate gate: a silent flip in either direction fails this test.

    It is `True` because the sign convention was verified against sustained export, a
    signed source is confirmed in derived mode, and the regulation model states its limits.
    """
    assert ACTIVE_CONTROL_READY is True


def test_phase_headroom_equals_measured_margin_in_every_measurement_mode() -> None:
    """`phase_headroom_a` is the raw uncredited margin in every mode, whatever charger is configured."""
    fuse_a = 25.0
    safety_margin_a = 1.0
    charger = _charger(
        requested_current_a=16.0,
        measured_current_a=_direct(12.0, 12.0, 12.0),
        min_current_a=0.0,
    )

    direct_config = _config(
        main_fuse_a=fuse_a, safety_margin_a=safety_margin_a, direct=_direct(20.0, 20.0, 20.0)
    )
    derived_config = _config(
        main_fuse_a=fuse_a,
        safety_margin_a=safety_margin_a,
        direct=None,
        derived=DerivedPhaseMeasurement(
            l1=_derived_input(20.0 * 230.0, 0.0, 230.0),
            l2=_derived_input(20.0 * 230.0, 0.0, 230.0),
            l3=_derived_input(20.0 * 230.0, 0.0, 230.0),
        ),
        mode="derived_phase_current",
    )

    for mode, config in (
        ("direct_phase_current", direct_config),
        ("derived_phase_current", derived_config),
    ):
        result = calculate_site_capacity(config, [charger])

        assert result.phase_headroom_a == result.measured_margin_a, mode
        # Both are the uncredited 25 - 1 - 20 = 4 A, not the credited diagnostic figure.
        assert result.measured_margin_a == {
            phase: fuse_a - safety_margin_a - 20.0 for phase in PHASES
        }, mode


def test_two_chargers_on_the_same_phase_never_get_the_same_headroom_twice() -> None:
    # 20 A of headroom on every phase; two chargers want 16 A each, and the total allocated must not exceed 20 A.
    config = _config(direct=_direct(4.0, 4.0, 4.0))  # ceiling 24, other load 4 -> headroom 20
    chargers = [
        _charger("charger_a", requested_current_a=16.0, min_current_a=1.0),
        _charger("charger_b", requested_current_a=16.0, min_current_a=1.0),
    ]

    result = calculate_site_capacity(config, chargers)

    proposed_by_id = {a.charger_entry_id: a.proposed_current_a for a in result.allocations}
    assert sum(proposed_by_id.values()) <= 20.0
    # Deterministic order: "charger_a" sorts first and is served first.
    assert proposed_by_id["charger_a"] == 16.0
    assert proposed_by_id["charger_b"] == 4.0
    assert result.allocations[1].state == "capacity_limited"


def test_allocation_order_is_independent_of_input_order() -> None:
    config = _config(direct=_direct(4.0, 4.0, 4.0))
    chargers_forward = [
        _charger("charger_a", requested_current_a=16.0),
        _charger("charger_b", requested_current_a=16.0),
    ]
    chargers_backward = list(reversed(chargers_forward))

    forward = calculate_site_capacity(config, chargers_forward)
    backward = calculate_site_capacity(config, chargers_backward)

    forward_by_id = {a.charger_entry_id: a.proposed_current_a for a in forward.allocations}
    backward_by_id = {a.charger_entry_id: a.proposed_current_a for a in backward.allocations}
    assert forward_by_id == backward_by_id


def test_safety_invariant_total_allocated_current_never_exceeds_site_capacity() -> None:
    """Over several three-phase chargers sharing every phase, the sum of proposed currents never exceeds the headroom."""
    headroom = {"L1": 20.0, "L2": 20.0, "L3": 20.0}
    requests = [
        _charger("charger_a", requested_current_a=16.0, min_current_a=1.0),
        _charger("charger_b", requested_current_a=16.0, min_current_a=1.0),
        _charger("charger_c", requested_current_a=16.0, min_current_a=1.0),
    ]

    from custom_components.spotnav.site.site_capacity import allocate_chargers

    allocations = allocate_chargers(headroom, requests)

    total_allocated = sum(a.proposed_current_a for a in allocations.values())
    assert total_allocated <= headroom["L1"]


def test_three_phase_charger_reserves_headroom_on_all_three_phases_for_the_next_charger() -> None:
    # charger_a is single-phase on L1, charger_b three-phase: A only reserves L1, and B is limited by the tightest phase.
    config = _config(direct=_direct(0.0, 0.0, 0.0))  # 24A headroom everywhere
    chargers = [
        _charger("charger_a", requested_current_a=10.0, phases=1, phase="L1"),
        _charger("charger_b", requested_current_a=20.0, phases=3),
    ]

    result = calculate_site_capacity(config, chargers)
    by_id = {a.charger_entry_id: a for a in result.allocations}

    assert by_id["charger_a"].proposed_current_a == 10.0
    # L1 has 14 A left (24-10), so B is capped by L1 at 14 A.
    assert by_id["charger_b"].proposed_current_a == 14.0
    assert by_id["charger_b"].limiting_phase == "L1"


def test_single_phase_charger_on_an_explicitly_chosen_phase_is_limited_by_that_phase_only() -> None:
    # L2 is heavily loaded; a charger wired to L1 must not be limited by it.
    config = _config(direct=_direct(0.0, 20.0, 0.0))
    charger = _charger(requested_current_a=16.0, phases=1, phase="L1")

    result = calculate_site_capacity(config, [charger])

    assert result.allocations[0].proposed_current_a == 16.0
    assert result.allocations[0].state == "capacity_available"


def test_single_phase_charger_with_unknown_phase_gets_no_safe_recommendation() -> None:
    config = _config(direct=_direct(0.0, 0.0, 0.0))
    charger = _charger(requested_current_a=16.0, phases=1, phase=None)

    result = calculate_site_capacity(config, [charger])

    allocation = result.allocations[0]
    assert allocation.proposed_current_a is None
    assert allocation.state == "invalid_measurements"
    # The site's three-phase measurements are otherwise fine.
    assert result.state == "observing"


def test_requested_current_exactly_equal_to_headroom_is_capacity_available_not_limited() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))  # headroom 19A everywhere
    charger = _charger(requested_current_a=19.0)

    result = calculate_site_capacity(config, [charger])

    assert result.allocations[0].proposed_current_a == 19.0
    assert result.allocations[0].state == "capacity_available"


def test_headroom_exactly_equal_to_minimum_current_is_not_below_minimum() -> None:
    config = _config(main_fuse_a=13.0, safety_margin_a=1.0, direct=_direct(6.0, 6.0, 6.0))
    # ceiling 12, other load 6 -> headroom 6.0, equal to min_current_a.
    charger = _charger(requested_current_a=16.0, min_current_a=6.0)

    result = calculate_site_capacity(config, [charger])

    assert result.allocations[0].proposed_current_a == 6.0
    assert result.allocations[0].state == "capacity_limited"


def test_measurement_age_exactly_at_the_maximum_is_still_fresh() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0, age=MAX_AGE), max_age_s=MAX_AGE)

    result = calculate_site_capacity(config, [])

    assert result.state == "observing"


def test_measurement_age_one_second_past_the_maximum_is_stale() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0, age=MAX_AGE + 1.0), max_age_s=MAX_AGE)

    result = calculate_site_capacity(config, [])

    assert result.state == "stale_measurements"


def test_negative_headroom_never_produces_a_negative_proposed_current() -> None:
    # Other load alone (28 A) already exceeds the 24 A ceiling.
    config = _config(direct=_direct(28.0, 28.0, 28.0))
    charger = _charger(requested_current_a=16.0)

    result = calculate_site_capacity(config, [charger])

    assert all(value < 0 for value in result.phase_headroom_a.values())
    assert result.allocations[0].proposed_current_a == 0.0
    assert result.allocations[0].state == "below_minimum_current"


def test_round_down_to_step_never_rounds_up() -> None:
    assert round_down_to_step(19.9) == 19.0
    assert round_down_to_step(19.0) == 19.0
    assert round_down_to_step(-0.5) == -1.0
    assert round_down_to_step(19.9, step=0.5) == 19.5


def test_fractional_headroom_is_proposed_as_a_whole_ampere_rounded_down() -> None:
    config = _config(direct=_direct(4.7, 4.7, 4.7))  # ceiling 24, other load 4.7 -> 19.3
    charger = _charger(requested_current_a=25.0)

    result = calculate_site_capacity(config, [charger])

    assert result.allocations[0].proposed_current_a == 19.0


def test_headroom_below_the_configured_minimum_current_recommends_pausing() -> None:
    config = _config(direct=_direct(20.0, 20.0, 20.0))  # headroom 4A
    charger = _charger(requested_current_a=16.0, min_current_a=6.0)

    result = calculate_site_capacity(config, [charger])

    assert result.allocations[0].proposed_current_a == 0.0
    assert result.allocations[0].state == "below_minimum_current"


def test_one_stale_phase_marks_the_whole_site_stale_even_if_others_are_fresh() -> None:
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, HEALTHY_AGE),
        l2=PhaseValue(5.0, MAX_AGE + 10.0),
        l3=PhaseValue(5.0, HEALTHY_AGE),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "stale_measurements"
    assert all(a.state == "stale_measurements" for a in result.allocations)
    assert all(a.proposed_current_a is None for a in result.allocations)


def test_unknown_age_is_treated_as_stale_not_assumed_fresh() -> None:
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, age_s=None),
        l2=PhaseValue(5.0, HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [])

    assert result.state == "stale_measurements"


# Phase liveness diagnostics: `age_s > max_age_s` (`last_updated`) cannot tell a flat-but-healthy sensor from a dead one,
# so `classify_phase_liveness` also uses `report_age_s` (`last_reported`, moved by any state write).


def test_classify_phase_liveness_fresh_ignores_report_age_entirely() -> None:
    assert classify_phase_liveness(HEALTHY_AGE, None, MAX_AGE) == "fresh"
    assert classify_phase_liveness(HEALTHY_AGE, MAX_AGE + 999.0, MAX_AGE) == "fresh"


def test_classify_phase_liveness_confirmed_unchanged_when_report_is_fresh() -> None:
    assert (
        classify_phase_liveness(MAX_AGE + 10.0, HEALTHY_AGE, MAX_AGE) == "confirmed_unchanged"
    )


def test_classify_phase_liveness_unconfirmed_stale_when_no_report_signal_exists() -> None:
    assert classify_phase_liveness(MAX_AGE + 10.0, None, MAX_AGE) == "unconfirmed_stale"


def test_classify_phase_liveness_no_recent_report_when_report_is_also_stale() -> None:
    assert (
        classify_phase_liveness(MAX_AGE + 10.0, MAX_AGE + 10.0, MAX_AGE) == "no_recent_report"
    )


def test_only_one_phase_stalling_is_confirmed_unchanged_not_a_dead_link() -> None:
    """Only L2 stops changing (a flat load) while its source keeps being re-polled: the site proposes, with L2's headroom one `CONFIRMED_UNCHANGED_MARGIN_A` smaller."""
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        l2=PhaseValue(5.0, MAX_AGE + 900.0, report_age_s=HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "observing"
    # Ceiling 24 A: L1/L3 keep their 19 A, L2 keeps 19 A less the margin, so L2 limits and `confidence` says the proposal uses a flat reading.
    assert result.phase_headroom_a == {
        "L1": 19.0,
        "L2": 19.0 - CONFIRMED_UNCHANGED_MARGIN_A,
        "L3": 19.0,
    }
    assert result.limiting_phase == "L2"
    assert result.confidence == "guarded"
    assert result.allocations[0].proposed_current_a == 16.0
    # The invariant holds with the discount applied: it is part of reading the measurement, not of crediting math.
    assert result.measured_margin_a == result.phase_headroom_a
    assert result.phase_liveness == {
        "L1": "fresh",
        "L2": "confirmed_unchanged",
        "L3": "fresh",
    }


def test_a_confirmed_unchanged_phase_yields_strictly_less_than_the_same_reading_fresh() -> None:
    """Same readings, except L2's has not moved: fresh proposes at "high" confidence with 16 A of L2 headroom, confirmed-unchanged with one margin less at "guarded"."""
    fresh = calculate_site_capacity(
        _config(
            direct=DirectPhaseMeasurement(
                l1=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
                l2=PhaseValue(8.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
                l3=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
            )
        ),
        [_charger()],
    )
    flat = calculate_site_capacity(
        _config(
            direct=DirectPhaseMeasurement(
                l1=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
                l2=PhaseValue(8.0, MAX_AGE + 900.0, report_age_s=HEALTHY_AGE),
                l3=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
            )
        ),
        [_charger()],
    )

    assert fresh.confidence == "high"
    assert flat.confidence == "guarded"
    assert fresh.phase_headroom_a == {"L1": 19.0, "L2": 16.0, "L3": 19.0}
    assert flat.phase_headroom_a == {
        "L1": 19.0,
        "L2": 16.0 - CONFIRMED_UNCHANGED_MARGIN_A,
        "L3": 19.0,
    }
    # A proposal can only ever be smaller, never larger.
    assert flat.allocations[0].proposed_current_a <= fresh.allocations[0].proposed_current_a


def test_unconfirmed_stale_and_no_recent_report_still_block_with_todays_reason() -> None:
    """The other two liveness verdicts make the site refuse, and the diagnostic fields say which case it was."""
    for report_age in (None, MAX_AGE + 900.0):
        direct = DirectPhaseMeasurement(
            l1=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
            l2=PhaseValue(5.0, MAX_AGE + 900.0, report_age_s=report_age),
            l3=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        )

        result = calculate_site_capacity(_config(direct=direct), [_charger()])

        assert result.state == "stale_measurements"
        assert result.reason == REASON_STALE
        assert result.phase_headroom_a == {"L1": None, "L2": None, "L3": None}
        assert all(allocation.proposed_current_a is None for allocation in result.allocations)
        assert result.phase_liveness["L2"] in ("unconfirmed_stale", "no_recent_report")
        # A site that proposes nothing has no proposal for "guarded" to describe: it keeps the mode's own confidence.
        assert result.confidence in ("high", "low")


def test_an_unknown_age_is_never_accepted_even_when_the_report_is_fresh() -> None:
    """Freshness is never assumed: a fresh `report_age_s` with unknown `age_s` looks `confirmed_unchanged`, but the gate still refuses it."""
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        l2=PhaseValue(5.0, None, report_age_s=HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
    )

    result = calculate_site_capacity(_config(direct=direct), [_charger()])

    assert result.state == "stale_measurements"
    assert result.reason == REASON_STALE
    assert result.phase_liveness["L2"] == "confirmed_unchanged"
    assert result.phase_headroom_a == {"L1": None, "L2": None, "L3": None}


def test_confidence_is_guarded_even_when_the_flat_phase_is_not_the_limiting_one() -> None:
    """`confidence` says a flat reading is in the proposal; `measurement_mode` and `phase_liveness` say which phase."""
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        l2=PhaseValue(12.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        l3=PhaseValue(5.0, MAX_AGE + 900.0, report_age_s=HEALTHY_AGE),
    )

    result = calculate_site_capacity(_config(direct=direct), [_charger()])

    assert result.limiting_phase == "L2"
    assert result.phase_liveness["L3"] == "confirmed_unchanged"
    assert result.confidence == "guarded"


def test_confidence_is_still_high_or_low_when_every_phase_was_fresh() -> None:
    """"guarded" only ever means a flat phase: an all-fresh site is unchanged in both modes."""
    direct = calculate_site_capacity(_config(direct=_direct(5.0, 5.0, 5.0)), [_charger()])
    derived = calculate_site_capacity(
        _config(
            mode="derived_phase_current",
            derived=DerivedPhaseMeasurement(
                l1=_derived_input(1000.0, 0.0, 230.0),
                l2=_derived_input(1000.0, 0.0, 230.0),
                l3=_derived_input(1000.0, 0.0, 230.0),
            ),
        ),
        [_charger()],
    )

    assert direct.state == "observing"
    assert direct.confidence == "high"
    assert derived.state == "observing"
    assert derived.confidence == "low"


def test_the_liveness_margin_can_never_raise_headroom() -> None:
    """Over a range of headrooms, margins and phase sets, the discounted headroom is never above the input and is exactly the margin below it on the named phases."""
    for headroom_l1 in (0.0, 1.0, 7.5, 24.0, 100.0):
        for margin in (-5.0, -0.5, 0.0, 0.5, 1.0, 3.0):
            for named in (frozenset(), frozenset({"L1"}), frozenset({"L1", "L3"}), frozenset(PHASES)):
                headroom = {"L1": headroom_l1, "L2": 5.0, "L3": 5.0}

                result = headroom_with_liveness_margin(headroom, named, margin)

                assert set(result) == set(headroom)
                for phase, value in headroom.items():
                    assert result[phase] <= value
                    if phase in named:
                        assert result[phase] == value - abs(margin)
                    else:
                        assert result[phase] == value


def test_only_one_phase_stalling_with_no_report_signal_is_unconfirmed_not_trusted() -> None:
    """L2's source gives no `report_age_s` at all: `phase_liveness` says there is no evidence rather than guessing."""
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        l2=PhaseValue(5.0, MAX_AGE + 900.0, report_age_s=None),
        l3=PhaseValue(5.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "stale_measurements"
    assert result.phase_liveness["L2"] == "unconfirmed_stale"


def test_the_entire_source_going_quiet_shows_no_recent_report_on_every_phase() -> None:
    """The whole site measurement stops at once: every phase's `report_age_s` is stale, so each resolves to `no_recent_report`.

    `state`/`allocations` are unaffected (`stale_measurements`). It is not named "dead":
    a dead link and a slow coordinator look identical from here.
    """
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, MAX_AGE + 500.0, report_age_s=MAX_AGE + 500.0),
        l2=PhaseValue(5.0, MAX_AGE + 500.0, report_age_s=MAX_AGE + 500.0),
        l3=PhaseValue(5.0, MAX_AGE + 500.0, report_age_s=MAX_AGE + 500.0),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "stale_measurements"
    assert result.phase_liveness == {
        "L1": "no_recent_report",
        "L2": "no_recent_report",
        "L3": "no_recent_report",
    }


def test_phase_liveness_is_none_when_the_phase_value_itself_is_missing() -> None:
    """No value at all means no liveness question, distinct from every real liveness state."""
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(None, None, problem="missing"),
        l2=PhaseValue(5.0, HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [])

    assert result.phase_liveness["L1"] is None


def test_derived_mode_combines_report_age_like_it_combines_age_the_oldest_or_unknown_wins() -> None:
    """If any of a phase's P/Q/V sub-readings has no report signal, the combined `report_age_s` is `None`."""
    derived = DerivedPhaseMeasurement(
        l1=DerivedPhaseInput(
            active_power_w=PhaseValue(1000.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
            reactive_power_var=PhaseValue(0.0, HEALTHY_AGE, report_age_s=None),
            voltage_v=PhaseValue(230.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        ),
        l2=DerivedPhaseInput(
            active_power_w=PhaseValue(1000.0, MAX_AGE + 10.0, report_age_s=HEALTHY_AGE),
            reactive_power_var=PhaseValue(0.0, MAX_AGE + 10.0, report_age_s=30.0),
            voltage_v=PhaseValue(230.0, MAX_AGE + 10.0, report_age_s=HEALTHY_AGE),
        ),
        l3=DerivedPhaseInput(
            active_power_w=PhaseValue(1000.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
            reactive_power_var=PhaseValue(0.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
            voltage_v=PhaseValue(230.0, HEALTHY_AGE, report_age_s=HEALTHY_AGE),
        ),
    )
    config = _config(derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    # L1 is fresh outright, so its report signal is never consulted.
    assert result.phase_liveness["L1"] == "fresh"
    # L2 is stale; its oldest report_age_s of the three is 30.0 s, within max_age_s: corroborated, not just silent.
    assert result.phase_liveness["L2"] == "confirmed_unchanged"


def test_classify_current_reports_missing_for_none_unknown_and_unavailable() -> None:
    assert classify_current(None, "A") == (None, "missing")
    assert classify_current("unknown", "A") == (None, "missing")
    assert classify_current("unavailable", "A") == (None, "missing")
    assert classify_current("", "A") == (None, "missing")


def test_classify_current_reports_invalid_for_nan_inf_and_negative() -> None:
    assert classify_current(float("nan"), "A") == (None, "invalid")
    assert classify_current(float("inf"), "A") == (None, "invalid")
    assert classify_current(-1.0, "A") == (None, "invalid")
    assert classify_current("not-a-number", "A") == (None, "invalid")
    assert classify_current(True, "A") == (None, "invalid")  # bool is never a measurement


def test_missing_phase_measurement_yields_missing_measurements_state() -> None:
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(None, problem="missing"),
        l2=PhaseValue(5.0, HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [])

    assert result.state == "missing_measurements"
    assert result.phase_headroom_a == {"L1": None, "L2": None, "L3": None}


def test_invalid_phase_measurement_yields_invalid_measurements_state() -> None:
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(None, problem="invalid"),
        l2=PhaseValue(5.0, HEALTHY_AGE),
        l3=PhaseValue(5.0, HEALTHY_AGE),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [])

    assert result.state == "invalid_measurements"


def test_invalid_takes_priority_over_missing_and_stale_in_the_reported_state() -> None:
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(None, problem="missing"),
        l2=PhaseValue(None, problem="invalid"),
        l3=PhaseValue(5.0, MAX_AGE + 10.0),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [])

    assert result.state == "invalid_measurements"


def test_normalize_current_converts_milliamps_to_amps() -> None:
    assert classify_current(1500, "mA")[0] == 1.5
    assert classify_current("2000", "mA")[0] == 2.0


def test_normalize_current_rejects_incompatible_units() -> None:
    assert classify_current(10, "kWh")[0] is None
    assert classify_current(10, "V")[0] is None
    assert classify_current(10, "%")[0] is None


def test_classify_voltage_rejects_zero_and_negative_as_invalid_not_missing() -> None:
    assert classify_voltage(0, "V") == (None, "invalid")
    assert classify_voltage(-230, "V") == (None, "invalid")
    assert classify_voltage(None, "V") == (None, "missing")


def test_classify_power_accepts_negative_values_and_kilowatt_unit() -> None:
    assert classify_power(-500.0, "W") == (-500.0, None)
    assert classify_power(1.5, "kW") == (1500.0, None)
    assert classify_power(1.5, "kvar") == (1500.0, None)


def test_missing_unit_is_invalid_not_a_guessed_unit() -> None:
    # A numeric value with no unit is never assumed to be in the obvious unit.
    assert classify_current(10, None) == (None, "invalid")
    assert classify_current(10, "") == (None, "invalid")
    assert classify_power(500, None) == (None, "invalid")
    assert classify_voltage(230, None) == (None, "invalid")
    assert classify_current(10, None)[0] is None
    assert normalize_power(500, None) is None
    assert normalize_voltage(230, None) is None


def test_absent_state_stays_missing_regardless_of_unit() -> None:
    # A genuinely absent state is always "missing", never "invalid", with or without a unit attribute.
    assert classify_current(None, None) == (None, "missing")
    assert classify_current("unknown", "A") == (None, "missing")
    assert classify_current("unavailable", None) == (None, "missing")
    assert classify_current("", "A") == (None, "missing")


def test_a_valid_number_with_a_correct_explicit_unit_still_normalizes() -> None:
    # The ordinary explicit-and-correct-unit case still works.
    assert classify_current(10, "A") == (10.0, None)
    assert classify_power(500, "W") == (500.0, None)
    assert classify_voltage(230, "V") == (230.0, None)


def _derived_input(
    p: float | None,
    q: float | None,
    v: float | None,
    age: float = HEALTHY_AGE,
    report_age: float | None = None,
) -> DerivedPhaseInput:
    # `report_age` is an optional param (default `None`), so `confirmed_unchanged` phases (stale `age`, fresh `report_age`) can be built here too.
    return DerivedPhaseInput(
        active_power_w=PhaseValue(
            p, age, problem=None if p is not None else "missing", report_age_s=report_age
        ),
        reactive_power_var=PhaseValue(
            q, age, problem=None if q is not None else "missing", report_age_s=report_age
        ),
        voltage_v=PhaseValue(
            v, age, problem=None if v is not None else "missing", report_age_s=report_age
        ),
    )


def test_apparent_current_from_derived_matches_the_documented_formula() -> None:
    # sqrt(3000^2 + 1000^2) / 230 ~= 13.73 A
    result = apparent_current_from_derived(3000.0, 1000.0, 230.0)
    assert result is not None
    assert abs(result - (3000.0**2 + 1000.0**2) ** 0.5 / 230.0) < 1e-9


def test_derived_mode_computes_headroom_from_apparent_current_per_phase() -> None:
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, 0.0, 230.0),  # 10A
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.state == "observing"
    assert result.confidence == "low"
    for phase in ("L1", "L2", "L3"):
        assert abs(result.measured_phase_current_a[phase] - 10.0) < 1e-6
        assert abs(result.phase_headroom_a[phase] - 14.0) < 1e-6


def test_derived_mode_with_missing_reactive_power_yields_no_value_for_that_phase() -> None:
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, None, 230.0),
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.measured_phase_current_a["L1"] is None
    assert result.state == "missing_measurements"


def test_derived_mode_with_invalid_voltage_yields_no_value_for_that_phase() -> None:
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, 0.0, 0.0, age=HEALTHY_AGE),  # voltage 0 is invalid, not missing
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    # Simulate classify_voltage's "invalid" explicitly; `_derived_input` only sets "missing" for a bare None.
    derived = DerivedPhaseMeasurement(
        l1=DerivedPhaseInput(
            active_power_w=PhaseValue(2300.0, HEALTHY_AGE),
            reactive_power_var=PhaseValue(0.0, HEALTHY_AGE),
            voltage_v=PhaseValue(None, HEALTHY_AGE, problem="invalid"),
        ),
        l2=derived.l2,
        l3=derived.l3,
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.measured_phase_current_a["L1"] is None
    assert result.state == "invalid_measurements"


# Signed phase active power is diagnostic only: never an available current, never fed into `state`/`reason`/`allocations`/`phase_headroom_a`.


def test_signed_active_power_is_positive_during_import() -> None:
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, 0.0, 230.0),
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w == {"L1": 2300.0, "L2": 2300.0, "L3": 2300.0}
    assert all(reason is None for reason in result.phase_signed_active_power_reason.values())
    # Diagnostic only: never influences the real gating/proposal fields.
    assert result.state == "observing"


def test_signed_active_power_is_negative_during_export() -> None:
    """A negative reading (negative = exporting) passes through with its sign intact."""
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(-4129.0, -180.0, 230.0),
        l2=_derived_input(-4275.0, -90.0, 230.0),
        l3=_derived_input(-4285.0, 33.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w == {"L1": -4129.0, "L2": -4275.0, "L3": -4285.0}
    assert all(reason is None for reason in result.phase_signed_active_power_reason.values())


def test_signed_active_power_with_mixed_phase_signs() -> None:
    """One phase may export while another imports; each phase's sign is reported independently."""
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(500.0, 0.0, 230.0),
        l2=_derived_input(-300.0, 0.0, 230.0),
        l3=_derived_input(0.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w == {"L1": 500.0, "L2": -300.0, "L3": 0.0}


def test_signed_active_power_is_none_with_reason_missing_when_phase_is_absent() -> None:
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(None, 0.0, 230.0),
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w["L1"] is None
    assert result.phase_signed_active_power_reason["L1"] == "missing"
    # The field is always present even with no reading, so the age stays `None` too.
    assert "L1" in result.phase_signed_active_power_age_s


def test_signed_active_power_is_none_with_reason_stale_when_too_old() -> None:
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, 0.0, 230.0, age=MAX_AGE + 30.0),
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w["L1"] is None
    assert result.phase_signed_active_power_reason["L1"] == "stale"
    # The raw age stays visible even though the value was discarded for being too old.
    assert result.phase_signed_active_power_age_s["L1"] == MAX_AGE + 30.0
    # L2/L3 are unaffected: evaluated per phase.
    assert result.phase_signed_active_power_w["L2"] == 2300.0
    assert result.phase_signed_active_power_reason["L2"] is None


def test_signed_active_power_is_none_with_reason_invalid_for_an_unrecognized_unit() -> None:
    value, problem = classify_power("500", None)  # no unit at all -> invalid
    assert problem == "invalid"
    derived = DerivedPhaseMeasurement(
        l1=DerivedPhaseInput(
            active_power_w=PhaseValue(value, HEALTHY_AGE, problem=problem),
            reactive_power_var=PhaseValue(0.0, HEALTHY_AGE),
            voltage_v=PhaseValue(230.0, HEALTHY_AGE),
        ),
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w["L1"] is None
    assert result.phase_signed_active_power_reason["L1"] == "invalid"


def test_signed_active_power_discards_a_malformed_phasevalue_with_a_value_and_a_problem() -> None:
    """A `PhaseValue` violating its contract (`.problem` is `None` exactly when `.value` is not) must not be treated as usable because `.value` is present."""
    derived = DerivedPhaseMeasurement(
        l1=DerivedPhaseInput(
            active_power_w=PhaseValue(2300.0, HEALTHY_AGE, problem="invalid"),
            reactive_power_var=PhaseValue(0.0, HEALTHY_AGE),
            voltage_v=PhaseValue(230.0, HEALTHY_AGE),
        ),
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w["L1"] is None
    assert result.phase_signed_active_power_reason["L1"] == "invalid"
    # A discarded value always carries a reason.
    assert result.phase_signed_active_power_reason["L1"] is not None


def test_signed_active_power_does_not_detect_a_brief_cross_phase_desynchronization() -> None:
    """Documented scope limit: each phase is judged only on its own freshness, so three individually fresh but mutually inconsistent readings are reported as given."""
    derived = DerivedPhaseMeasurement(
        # All three fresh by their own age_s but an impossible combination for one grid P, like a sub-second desync between separately polled registers.
        l1=_derived_input(-4129.0, 0.0, 230.0, age=2.0),
        l2=_derived_input(2045.0, 0.0, 230.0, age=2.0),
        l3=_derived_input(1952.0, 0.0, 230.0, age=2.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    # Reported as given: no cross-phase check, no flag, no `None`.
    assert result.phase_signed_active_power_w == {"L1": -4129.0, "L2": 2045.0, "L3": 1952.0}
    assert all(reason is None for reason in result.phase_signed_active_power_reason.values())


def test_signed_active_power_is_none_in_direct_mode() -> None:
    """Direct mode has no P/Q reading: every phase is `None`, never guessed from the current magnitude."""
    config = _config(direct=_direct(5.0, 5.0, 5.0))

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_w == {"L1": None, "L2": None, "L3": None}
    assert all(reason is None for reason in result.phase_signed_active_power_reason.values())


# `phase_signed_active_power_diagnostic_w`/`phase_voltage_v` are age-independent siblings for callers with their own liveness-graded freshness rule (the solar controller).
# Each test pins one property against the strict field, so reintroducing the age gate on the diagnostic field fails here.


def test_diagnostic_signed_power_survives_where_the_strict_field_does_not() -> None:
    """A phase whose raw age passed `max_age_s` but whose report is recent (`confirmed_unchanged`): the strict field is `None`, the diagnostic one keeps the reading."""
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, 0.0, 230.0, age=MAX_AGE + 60.0, report_age=HEALTHY_AGE),
        l2=_derived_input(2300.0, 0.0, 230.0),
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_liveness["L1"] == "confirmed_unchanged"
    assert result.phase_signed_active_power_w["L1"] is None  # the strict field: unchanged
    assert result.phase_signed_active_power_diagnostic_w["L1"] == 2300.0
    assert result.phase_voltage_v["L1"] == 230.0


def test_diagnostic_signed_power_is_none_when_missing_or_invalid_not_merely_old() -> None:
    """The diagnostic field drops exactly "missing"/"invalid", like the strict one; only the age condition is loosened."""
    value, problem = classify_power("500", None)  # no unit -> invalid
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(None, 0.0, 230.0),  # missing
        l2=DerivedPhaseInput(
            active_power_w=PhaseValue(value, HEALTHY_AGE, problem=problem),
            reactive_power_var=PhaseValue(0.0, HEALTHY_AGE),
            voltage_v=PhaseValue(230.0, HEALTHY_AGE),
        ),
        l3=_derived_input(2300.0, 0.0, 230.0, age=MAX_AGE + 999.0),  # merely old
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_diagnostic_w["L1"] is None
    assert result.phase_signed_active_power_diagnostic_w["L2"] is None
    # L3 is only old, never missing/invalid, so the diagnostic field keeps it.
    assert result.phase_signed_active_power_diagnostic_w["L3"] == 2300.0


def test_diagnostic_voltage_mirrors_the_signed_power_diagnostics_rule() -> None:
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, 0.0, 231.5, age=MAX_AGE + 60.0, report_age=HEALTHY_AGE),
        l2=_derived_input(2300.0, 0.0, None),  # missing voltage
        l3=_derived_input(2300.0, 0.0, 229.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.phase_voltage_v["L1"] == 231.5  # confirmed_unchanged, kept
    assert result.phase_voltage_v["L2"] is None  # missing
    assert result.phase_voltage_v["L3"] == 229.0


def test_diagnostic_fields_are_all_none_in_direct_mode() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))

    result = calculate_site_capacity(config, [])

    assert result.phase_signed_active_power_diagnostic_w == {"L1": None, "L2": None, "L3": None}
    assert result.phase_voltage_v == {"L1": None, "L2": None, "L3": None}


def test_diagnostic_fields_present_on_a_short_circuit_result_too() -> None:
    """`_short_circuit` (stale/invalid/missing site state) still populates the diagnostic fields from whatever raw readings exist."""
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(2300.0, 0.0, 230.0, age=MAX_AGE + 60.0, report_age=HEALTHY_AGE),
        l2=_derived_input(None, 0.0, 230.0),  # missing -> forces missing_measurements
        l3=_derived_input(2300.0, 0.0, 230.0),
    )
    config = _config(direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [])

    assert result.state == "missing_measurements"
    assert result.phase_signed_active_power_diagnostic_w["L1"] == 2300.0
    assert result.phase_voltage_v["L1"] == 230.0


def test_restart_with_no_persisted_freshness_never_assumes_safety() -> None:
    # A fresh `SiteCalculationConfig` with every age unknown (as after a restart) is never safe to recommend from.
    direct = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, age_s=None),
        l2=PhaseValue(5.0, age_s=None),
        l3=PhaseValue(5.0, age_s=None),
    )
    config = _config(direct=direct)

    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "stale_measurements"
    assert result.allocations[0].proposed_current_a is None


def test_disabled_short_circuits_before_reading_any_measurement() -> None:
    config = _config(enabled=False, direct=None)

    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "disabled"
    assert result.allocations[0].proposed_current_a is None


def test_missing_main_fuse_is_not_configured_never_a_guess() -> None:
    config = _config(main_fuse_a=None, direct=_direct(5.0, 5.0, 5.0))

    result = calculate_site_capacity(config, [_charger()])

    assert result.state == "not_configured"


def test_zero_or_negative_main_fuse_is_not_configured() -> None:
    config = _config(main_fuse_a=0.0, direct=_direct(5.0, 5.0, 5.0))
    result = calculate_site_capacity(config, [_charger()])
    assert result.state == "not_configured"


def test_unavailable_measurement_mode_is_not_configured() -> None:
    config = _config(mode="unavailable", direct=None, derived=None)

    result = calculate_site_capacity(config, [])

    assert result.state == "not_configured"


def test_no_chargers_configured_still_reports_site_health() -> None:
    """A site without any charger still surfaces site-level measurement health."""
    config = _config(direct=_direct(5.0, 5.0, 5.0))

    result = calculate_site_capacity(config, [])

    assert result.state == "observing"
    assert result.allocations == ()



def test_a_phase_exporting_while_the_charger_draws_more_than_the_net_reading_is_observed() -> None:
    # Solar site: the rest exports 13 A on every phase while the charger draws 10 A, so the net reads
    # 3 A of export. That is normal; the fuse is judged on |net|, and export leaves room for the charger.
    voltage = 230.0
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(-3.0 * voltage, 0.0, voltage),
        l2=_derived_input(-3.0 * voltage, 0.0, voltage),
        l3=_derived_input(-3.0 * voltage, 0.0, voltage),
    )
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=None, derived=derived, mode="derived_phase_current")
    charger = _charger(requested_current_a=16.0, measured_current_a=_direct(10.0, 10.0, 10.0))

    result = calculate_site_capacity(config, [charger])

    assert result.state == "observing"
    assert result.measured_margin_a == {"L1": 21.0, "L2": 21.0, "L3": 21.0}  # 24 - |3|
    # Worst case of the signed rest (-13 A): the charger may add up to 24 + 3 = 27 A on top of its draw.
    for phase in PHASES:
        assert abs(result.phase_signed_margin_a[phase] - 27.0) < 1e-6
    assert result.charger_reading_exceeds_site_phases == ()


def test_the_signed_margin_equals_the_plain_margin_on_net_import_without_reactive_load() -> None:
    voltage = 230.0
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(10.0 * voltage, 0.0, voltage),
        l2=_derived_input(10.0 * voltage, 0.0, voltage),
        l3=_derived_input(10.0 * voltage, 0.0, voltage),
    )
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [_charger(measured_current_a=_direct(5.0, 5.0, 5.0))])

    for phase in PHASES:
        assert abs(result.phase_signed_margin_a[phase] - result.measured_margin_a[phase]) < 1e-6


def test_reactive_load_shrinks_the_signed_margin_below_the_active_only_value() -> None:
    # 0 W but 10 A reactive on a 24 A ceiling: sqrt(24^2 - 10^2) = 21.8 A of room, not 24 A.
    voltage = 230.0
    derived = DerivedPhaseMeasurement(
        l1=_derived_input(0.0, 10.0 * voltage, voltage),
        l2=_derived_input(0.0, 10.0 * voltage, voltage),
        l3=_derived_input(0.0, 10.0 * voltage, voltage),
    )
    config = _config(main_fuse_a=25.0, safety_margin_a=1.0, direct=None, derived=derived, mode="derived_phase_current")

    result = calculate_site_capacity(config, [_charger()])

    for phase in PHASES:
        assert abs(result.phase_signed_margin_a[phase] - math.sqrt(24.0**2 - 10.0**2)) < 1e-6


def test_a_magnitude_source_has_no_signed_margin() -> None:
    config = _config(direct=_direct(5.0, 5.0, 5.0))

    result = calculate_site_capacity(config, [_charger()])

    assert result.phase_signed_margin_a == {"L1": None, "L2": None, "L3": None}


def test_a_first_priority_charger_is_served_before_a_normal_one_and_a_last_one_after() -> None:
    # 24 A of headroom on every phase, three-phase chargers asking for 16 A each: the first served gets it all.
    config = _config(direct=_direct(0.0, 0.0, 0.0))
    chargers = [
        _charger("a_last", priority="last"),
        _charger("b_normal"),
        _charger("c_first", priority="first"),
    ]

    by_id = {a.charger_entry_id: a for a in calculate_site_capacity(config, chargers).allocations}

    assert by_id["c_first"].proposed_current_a == 16.0
    assert by_id["b_normal"].proposed_current_a == 6.0 or by_id["b_normal"].proposed_current_a == 8.0
    assert by_id["a_last"].proposed_current_a == 0.0


def test_chargers_of_one_priority_are_served_in_the_sites_charger_order_not_by_id() -> None:
    config = _config(direct=_direct(0.0, 0.0, 0.0))
    # "z" joined first, so it is served first although its id sorts last, whichever way they are listed.
    chargers = [_charger("a_entry", order=1), _charger("z_entry", order=0)]

    by_id = {a.charger_entry_id: a for a in calculate_site_capacity(config, chargers).allocations}

    assert by_id["z_entry"].proposed_current_a == 16.0
    assert by_id["a_entry"].proposed_current_a < 16.0
