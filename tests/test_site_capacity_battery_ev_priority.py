"""EV priority over the battery and the two-tier headroom (real measured margin vs. estimate if a home battery yields), purely on `calculate_site_capacity`.

Scenarios: battery before EV, EV before battery, battery yields or not, a sudden heat-pump step,
missing/stale measurements and solar export.
"""

from __future__ import annotations

from custom_components.spotnav.site.site_capacity import (
    PHASES,
    BatteryYieldEstimate,
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


def _derived_input(active_power_w: float, reactive_power_var: float, voltage_v: float, age: float = HEALTHY_AGE) -> DerivedPhaseInput:
    return DerivedPhaseInput(
        active_power_w=PhaseValue(active_power_w, age),
        reactive_power_var=PhaseValue(reactive_power_var, age),
        voltage_v=PhaseValue(voltage_v, age),
    )


def _derived_balanced(p_per_phase: float, q_per_phase: float = 0.0, voltage: float = VOLTAGE) -> DerivedPhaseMeasurement:
    input_ = _derived_input(p_per_phase, q_per_phase, voltage)
    return DerivedPhaseMeasurement(l1=input_, l2=input_, l3=input_)


def _config(
    *,
    derived: DerivedPhaseMeasurement,
    battery: BatteryYieldEstimate | None = None,
    main_fuse_a: float = 25.0,
    safety_margin_a: float = 1.0,
) -> SiteCalculationConfig:
    return SiteCalculationConfig(
        enabled=True,
        main_fuse_a=main_fuse_a,
        safety_margin_a=safety_margin_a,
        measurement_mode="derived_phase_current",
        max_age_s=MAX_AGE,
        derived=derived,
        battery=battery,
    )


def _ev(requested_current_a: float | None = 16.0, measured_current_a: DirectPhaseMeasurement | None = None) -> ChargerRequest:
    return ChargerRequest(
        charger_entry_id="ev_charger",
        requested_current_a=requested_current_a,
        phases=3,
        min_current_a=6.0,
        measured_current_a=measured_current_a,
    )


def _direct(value: float, age: float = HEALTHY_AGE) -> DirectPhaseMeasurement:
    return DirectPhaseMeasurement(l1=PhaseValue(value, age), l2=PhaseValue(value, age), l3=PhaseValue(value, age))


def _aggregate_battery(power_w_total: float, age: float = HEALTHY_AGE) -> BatteryYieldEstimate:
    return BatteryYieldEstimate(aggregate_charge_power_w=PhaseValue(power_w_total, age))


def test_battery_before_ev_real_headroom_is_conservative_but_estimate_shows_more() -> None:
    # Battery alone draws 20 A/phase (4600 W/phase); the EV has not started (requested_current_a=0, a known request of zero).
    derived = _derived_balanced(p_per_phase=4600.0)
    battery = _aggregate_battery(power_w_total=4600.0 * 3)
    config = _config(derived=derived, battery=battery)
    ev = _ev(requested_current_a=0.0)

    result = calculate_site_capacity(config, [ev])

    assert result.state == "observing"
    # Real headroom: 25 - 1 - 20 = 4 A, what is safe now.
    for phase in PHASES:
        assert abs(result.phase_headroom_a[phase] - 4.0) < 1e-6
    # Estimated headroom if the battery yields: 4 + 20 = 24 A, a separately labeled number.
    assert result.battery_yield_basis == "assumed_equal_split"
    for phase in PHASES:
        assert abs(result.estimated_headroom_if_battery_yields_a[phase] - 24.0) < 1e-6
    # The real headroom is never conflated with the estimate.
    assert result.phase_headroom_a != result.estimated_headroom_if_battery_yields_a


def test_ev_before_battery_shows_all_three_headroom_tiers_distinctly() -> None:
    # The site total includes the EV's ~8.9 A and a battery aggregate reading is present (charging). In derived mode the EV credit is
    # diagnostic only, so the real headroom stays the uncredited margin and the battery estimate layers on that, not on the credited number.
    ev_current = 8.9
    total_current = 20.0
    derived = _derived_balanced(p_per_phase=VOLTAGE * total_current)
    battery = _aggregate_battery(power_w_total=VOLTAGE * 5.0 * 3)  # battery drawing 5A/phase
    config = _config(derived=derived, battery=battery)
    ev = _ev(requested_current_a=16.0, measured_current_a=_direct(ev_current))

    result = calculate_site_capacity(config, [ev])

    assert result.state == "observing"
    # Real headroom: the raw uncredited margin, 25 - 1 - 20 = 4 A, equal to measured_margin_a.
    for phase in PHASES:
        assert abs(result.phase_headroom_a[phase] - 4.0) < 1e-6
        assert result.phase_headroom_a[phase] == result.measured_margin_a[phase]
    # Diagnostic EV-credited estimate: 20 - 8.9 = 11.1 other load, so 25 - 1 - 11.1 = 12.9 A.
    for phase in PHASES:
        assert abs(result.calculated_headroom_after_ev_credit_a[phase] - 12.9) < 1e-6
    # The battery estimate layers on the real headroom (4 A), not the credited diagnostic (12.9 A): 4 + 5 = 9 A.
    for phase in PHASES:
        assert abs(result.estimated_headroom_if_battery_yields_a[phase] - 9.0) < 1e-6


def test_battery_yielding_increases_real_headroom_once_grid_power_actually_drops() -> None:
    """Battery power dropped from ~12.5 kW to ~6.8 kW once the EV's schedule started: real headroom increases once the battery actually yields, not because an estimate says it might."""
    before = _config(derived=_derived_balanced(p_per_phase=12500.0 / 3))
    after = _config(derived=_derived_balanced(p_per_phase=6800.0 / 3))
    ev = _ev(requested_current_a=10.0)

    result_before = calculate_site_capacity(before, [ev])
    result_after = calculate_site_capacity(after, [ev])

    for phase in PHASES:
        assert result_after.phase_headroom_a[phase] > result_before.phase_headroom_a[phase]


def test_battery_not_yielding_never_lets_the_optimistic_estimate_inflate_the_real_proposal() -> None:
    """However large the estimated headroom-if-battery-yields, the proposed current is bounded only by the real measured headroom."""
    derived = _derived_balanced(p_per_phase=4600.0)  # battery drawing hard, not yielding
    battery = _aggregate_battery(power_w_total=4600.0 * 3)
    config = _config(derived=derived, battery=battery)
    ev = _ev(requested_current_a=32.0)  # wants far more than is really available

    result = calculate_site_capacity(config, [ev])

    real_headroom = result.phase_headroom_a["L1"]
    estimated_headroom = result.estimated_headroom_if_battery_yields_a["L1"]
    assert estimated_headroom > real_headroom  # the estimate really is more optimistic here
    allocation = result.allocations[0]
    assert allocation.proposed_current_a is not None
    assert allocation.proposed_current_a <= real_headroom
    assert allocation.proposed_current_a < estimated_headroom


def test_sudden_heat_pump_load_reduces_real_headroom_and_proposal_promptly() -> None:
    """A sudden load step must reduce EV allocation promptly even if the battery is also expected to yield."""
    ev = _ev(requested_current_a=16.0)
    before_heat_pump = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0))
    # A large three-phase heat pump/boiler step adds another 8 A/phase.
    after_heat_pump = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 18.0))

    result_before = calculate_site_capacity(before_heat_pump, [ev])
    result_after = calculate_site_capacity(after_heat_pump, [ev])

    assert result_after.phase_headroom_a["L1"] < result_before.phase_headroom_a["L1"]
    assert result_after.allocations[0].proposed_current_a < result_before.allocations[0].proposed_current_a


# Sign convention: positive battery power is charging, negative is discharging; only a positive reading yields an estimate.


def test_battery_charging_positive_power_produces_a_positive_yield_estimate() -> None:
    config = _config(
        derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0),
        battery=_aggregate_battery(power_w_total=VOLTAGE * 6.0 * 3),  # +6A/phase charging
    )
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "assumed_equal_split"
    for phase in PHASES:
        assert abs(result.estimated_headroom_if_battery_yields_a[phase] - (result.phase_headroom_a[phase] + 6.0)) < 1e-6


def test_battery_discharging_negative_power_never_produces_a_positive_yield_estimate() -> None:
    """Negative `battery_power` means discharging. A discharging battery cannot "yield" grid headroom (stopping it would increase grid draw), so this resolves to `unknown`, never a zero or negative addition."""
    config = _config(
        derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0),
        battery=_aggregate_battery(power_w_total=-VOLTAGE * 6.0 * 3),  # discharging
    )
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_battery_zero_power_never_produces_a_positive_yield_estimate() -> None:
    config = _config(
        derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0),
        battery=_aggregate_battery(power_w_total=0.0),
    )
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_battery_discharging_per_phase_current_never_produces_a_positive_yield_estimate() -> None:
    """The same sign rule applies to a per-phase battery-current reading, gated via a fresh signed direction-confirmation reading."""
    discharging = BatteryYieldEstimate(
        per_phase_charge_current_a=_direct(-5.0),
        direction_confirmation_power_w=PhaseValue(-1000.0, HEALTHY_AGE),
    )
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=discharging)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_positive_per_phase_current_without_direction_confirmation_is_never_treated_as_charging() -> None:
    """A per-phase battery current magnitude, however positive and fresh, is not evidence of charging (a discharging or standby battery reports the same); without a fresh signed `direction_confirmation_power_w` it resolves to `unknown`."""
    unconfirmed = BatteryYieldEstimate(per_phase_charge_current_a=_direct(5.0))
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=unconfirmed)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_positive_per_phase_current_with_fresh_direction_confirmation_does_produce_a_yield_estimate() -> None:
    """A fresh, positive, signed direction-confirmation reading makes the per-phase-current path usable, then it behaves like any verified input."""
    confirmed = BatteryYieldEstimate(
        per_phase_charge_current_a=_direct(5.0),
        direction_confirmation_power_w=PhaseValue(1000.0, HEALTHY_AGE),
    )
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=confirmed)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "measured_per_phase"
    for phase in PHASES:
        assert abs(result.estimated_headroom_if_battery_yields_a[phase] - (result.phase_headroom_a[phase] + 5.0)) < 1e-6


def test_direction_confirmation_with_one_stale_current_phase_still_reports_unknown() -> None:
    """A fresh direction confirmation does not bypass the freshness/validity bar of the current reading: every phase must still be usable."""
    partially_stale = DirectPhaseMeasurement(
        l1=PhaseValue(5.0, HEALTHY_AGE),
        l2=PhaseValue(5.0, HEALTHY_AGE),
        l3=PhaseValue(5.0, MAX_AGE + 100.0),
    )
    confirmed_but_stale = BatteryYieldEstimate(
        per_phase_charge_current_a=partially_stale,
        direction_confirmation_power_w=PhaseValue(1000.0, HEALTHY_AGE),
    )
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=confirmed_but_stale)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_stale_direction_confirmation_reports_unknown_even_though_current_is_fresh() -> None:
    """The confirmation reading has its own freshness bar: a stale one cannot vouch for "charging right now"."""
    stale_confirmation = BatteryYieldEstimate(
        per_phase_charge_current_a=_direct(5.0),
        direction_confirmation_power_w=PhaseValue(1000.0, MAX_AGE + 100.0),
    )
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=stale_confirmation)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_battery_switching_from_charging_to_discharging_is_detected_even_though_current_magnitude_is_unchanged() -> None:
    """The per-phase current magnitude stays the same across two calculations, yet the estimate tracks the battery's actual direction, because it comes from a freshly re-checked signal, not a static flag."""
    unchanged_current = _direct(6.0)

    while_charging = BatteryYieldEstimate(
        per_phase_charge_current_a=unchanged_current,
        direction_confirmation_power_w=PhaseValue(1000.0, HEALTHY_AGE),
    )
    config_charging = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=while_charging)
    result_charging = calculate_site_capacity(config_charging, [_ev(requested_current_a=10.0)])

    assert result_charging.battery_yield_basis == "measured_per_phase"
    for phase in PHASES:
        assert result_charging.estimated_headroom_if_battery_yields_a[phase] is not None

    # Same current magnitude, but the battery now discharges: only the freshly re-read direction signal changed.
    while_discharging = BatteryYieldEstimate(
        per_phase_charge_current_a=unchanged_current,
        direction_confirmation_power_w=PhaseValue(-1000.0, HEALTHY_AGE),
    )
    config_discharging = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=while_discharging)
    result_discharging = calculate_site_capacity(config_discharging, [_ev(requested_current_a=10.0)])

    assert result_discharging.battery_yield_basis == "unknown"
    assert all(v is None for v in result_discharging.estimated_headroom_if_battery_yields_a.values())


def test_aggregate_power_path_is_unaffected_by_the_per_phase_direction_confirmation_field() -> None:
    """The direction-confirmation field only gates the per-phase-current path; the aggregate-power path uses the sign convention on its own reading."""
    battery = BatteryYieldEstimate(aggregate_charge_power_w=PhaseValue(4600.0 * 3, HEALTHY_AGE))
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=battery)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "assumed_equal_split"


def test_no_battery_configured_reports_unknown_basis_not_a_guessed_split() -> None:
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=None)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_stale_battery_measurement_is_treated_as_unknown_not_fresh() -> None:
    stale_battery = _aggregate_battery(power_w_total=4600.0 * 3, age=MAX_AGE + 100.0)
    config = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 10.0), battery=stale_battery)
    ev = _ev(requested_current_a=10.0)

    result = calculate_site_capacity(config, [ev])

    assert result.battery_yield_basis == "unknown"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_missing_site_measurements_still_report_no_battery_estimate_rather_than_crashing() -> None:
    """Even when the site's measurements are missing/stale (`_short_circuit`), the battery-estimate field is present and well-formed, never omitted or raising."""
    empty_input = DerivedPhaseInput(
        active_power_w=PhaseValue(None, None, problem="missing"),
        reactive_power_var=PhaseValue(None, None, problem="missing"),
        voltage_v=PhaseValue(None, None, problem="missing"),
    )
    derived = DerivedPhaseMeasurement(l1=empty_input, l2=empty_input, l3=empty_input)
    config = _config(derived=derived, battery=_aggregate_battery(power_w_total=4600.0 * 3))

    result = calculate_site_capacity(config, [_ev(requested_current_a=10.0)])

    assert result.state == "missing_measurements"
    assert all(v is None for v in result.estimated_headroom_if_battery_yields_a.values())


def test_solar_export_produces_a_finite_conservative_headroom_not_a_crash() -> None:
    """Documented open question: whether grid-power entities report export as negative is unverified.

    `apparent_current_from_derived` uses `hypot(P, Q)`, a magnitude, so a large export is treated like an
    equally large import: conservative, since it can only inflate "other load". This pins that behavior.
    """
    exporting = _config(derived=_derived_balanced(p_per_phase=-VOLTAGE * 15.0))  # exporting 15A/phase
    importing = _config(derived=_derived_balanced(p_per_phase=VOLTAGE * 15.0))  # importing 15A/phase
    ev = _ev(requested_current_a=10.0)

    result_exporting = calculate_site_capacity(exporting, [ev])
    result_importing = calculate_site_capacity(importing, [ev])

    assert result_exporting.state == "observing"
    for phase in PHASES:
        assert result_exporting.phase_headroom_a[phase] >= 0.0 or result_exporting.phase_headroom_a[phase] is not None
    # Same magnitude of P gives identical treatment: exporting and importing 15 A/phase produce the same headroom.
    assert result_exporting.phase_headroom_a == result_importing.phase_headroom_a
