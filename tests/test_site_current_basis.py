"""How a derived phase's current is obtained (`derived_phase_current`), the conservative estimate
from active power, signed currents and import/export pairs.

The safety claim under test: an estimate made from active power alone is never below the true
current for a power factor of at least 0.9, and the result states that it is an estimate
(`current_estimated`, `estimated_power_factor`) so the card can say so.
"""

from __future__ import annotations

import math

import pytest

from custom_components.spotnav.site.measurement_source import combine_power_pair
from custom_components.spotnav.site.site_capacity import (
    calculate_site_capacity,
    classify_apparent_power,
    classify_current,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    derived_phase_current,
    estimate_covers_power_factor,
    estimate_current_from_power,
    ESTIMATED_POWER_FACTOR,
    PhaseValue,
    SiteCalculationConfig,
)

AGE = 5.0
MAX_AGE = 120.0
VOLTAGE = 230.0


def pv(value: float | None, *, problem=None, age: float | None = AGE) -> PhaseValue:
    return PhaseValue(value, age if value is not None or problem else None, problem, report_age_s=age)


def phase(
    p: float,
    *,
    q: float | None = None,
    s: float | None = None,
    i: float | None = None,
    v: float = VOLTAGE,
) -> DerivedPhaseInput:
    return DerivedPhaseInput(
        active_power_w=pv(p),
        reactive_power_var=None if q is None else pv(q),
        voltage_v=pv(v),
        apparent_power_va=None if s is None else pv(s),
        current_a=None if i is None else pv(i),
    )


# ---- the estimate -------------------------------------------------------------------------------


@pytest.mark.parametrize("power_factor", [0.9, 0.92, 0.95, 0.98, 1.0])
@pytest.mark.parametrize("watts", [-9000.0, -230.0, 0.0, 15.0, 1150.0, 4600.0, 7360.0, 17250.0])
@pytest.mark.parametrize("voltage", [207.0, 230.0, 253.0])
def test_the_estimate_is_never_below_the_true_current_for_a_power_factor_of_at_least_0_9(
    power_factor: float, watts: float, voltage: float
) -> None:
    true_current = abs(watts) / (voltage * power_factor)  # I = S / U = |P| / (PF x U)

    estimate = estimate_current_from_power(watts, voltage)

    assert estimate is not None
    assert estimate >= true_current - 1e-9
    assert estimate_covers_power_factor(power_factor)


def test_the_estimate_sweeps_every_power_factor_from_0_9_up_without_understating() -> None:
    for step in range(0, 101):
        power_factor = 0.9 + step * 0.001
        for watts in (100.0, 2300.0, 6900.0):
            assert estimate_current_from_power(watts, VOLTAGE) >= watts / (VOLTAGE * power_factor) - 1e-9


@pytest.mark.parametrize("power_factor", [0.5, 0.7, 0.85, 0.89])
def test_below_0_9_the_estimate_understates_and_says_it_does_not_cover(power_factor: float) -> None:
    watts = 4600.0
    true_current = watts / (VOLTAGE * power_factor)

    estimate = estimate_current_from_power(watts, VOLTAGE)

    assert estimate < true_current
    assert not estimate_covers_power_factor(power_factor)
    # The shortfall is exactly the ratio of the power factors.
    assert estimate / true_current == pytest.approx(power_factor / ESTIMATED_POWER_FACTOR)


def test_the_estimate_needs_a_positive_voltage_and_a_real_assumption() -> None:
    assert estimate_current_from_power(None, VOLTAGE) is None
    assert estimate_current_from_power(1000.0, None) is None
    assert estimate_current_from_power(1000.0, 0.0) is None
    with pytest.raises(ValueError):
        estimate_current_from_power(1000.0, VOLTAGE, power_factor=0.0)
    with pytest.raises(ValueError):
        estimate_current_from_power(1000.0, VOLTAGE, power_factor=1.2)


# ---- which basis wins ---------------------------------------------------------------------------


def test_the_meters_own_current_wins_over_everything() -> None:
    result = derived_phase_current(phase(2300.0, q=500.0, s=2400.0, i=10.5))

    assert result.basis == "measured"
    assert result.value == 10.5
    assert result.problem is None


def test_apparent_power_is_s_over_u_when_there_is_no_measured_current() -> None:
    result = derived_phase_current(phase(2000.0, q=3000.0, s=2415.0))

    assert result.basis == "apparent"
    assert result.value == pytest.approx(2415.0 / VOLTAGE)


def test_reactive_power_is_used_when_present_and_nothing_better_is() -> None:
    result = derived_phase_current(phase(3000.0, q=1000.0))

    assert result.basis == "reactive"
    assert result.value == pytest.approx(math.hypot(3000.0, 1000.0) / VOLTAGE)


def test_without_current_apparent_or_reactive_power_the_current_is_estimated_from_power() -> None:
    result = derived_phase_current(phase(-2300.0))

    assert result.basis == "estimated"
    assert result.value == pytest.approx(2300.0 / (VOLTAGE * 0.9))


def test_an_unusable_configured_source_never_falls_through_to_a_cruder_basis() -> None:
    """A Q entity that is configured but unavailable must not silently become an estimate."""
    broken = DerivedPhaseInput(
        active_power_w=pv(3000.0),
        reactive_power_var=PhaseValue(None, None, problem="missing"),
        voltage_v=pv(VOLTAGE),
    )

    result = derived_phase_current(broken)

    assert result.value is None and result.basis is None
    assert result.problem == "missing"


def test_a_missing_measured_current_falls_back_to_a_configured_exact_source_not_an_estimate() -> None:
    mixed = DerivedPhaseInput(
        active_power_w=pv(3000.0),
        reactive_power_var=pv(1000.0),
        voltage_v=pv(VOLTAGE),
        current_a=PhaseValue(None, None, problem="missing"),
    )

    result = derived_phase_current(mixed)

    assert result.basis == "reactive"


def test_missing_active_power_or_voltage_is_unusable_whatever_else_is_configured() -> None:
    for broken in (
        DerivedPhaseInput(PhaseValue(None, None, problem="missing"), None, pv(VOLTAGE), current_a=pv(9.0)),
        DerivedPhaseInput(pv(1000.0), None, PhaseValue(None, None, problem="invalid"), current_a=pv(9.0)),
    ):
        result = derived_phase_current(broken)
        assert result.value is None and result.problem in ("missing", "invalid")


def test_the_oldest_input_sets_freshness() -> None:
    mixed = DerivedPhaseInput(
        active_power_w=pv(3000.0, age=2.0),
        reactive_power_var=None,
        voltage_v=pv(VOLTAGE, age=30.0),
        current_a=pv(13.0, age=7.0),
    )

    assert derived_phase_current(mixed).age_s == 30.0


# ---- the site result states what it did ---------------------------------------------------------


def _site(derived_inputs: dict[str, DerivedPhaseInput], *, fuse: float = 25.0):
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=fuse,
        safety_margin_a=1.0,
        measurement_mode="derived_phase_current",
        max_age_s=MAX_AGE,
        derived=DerivedPhaseMeasurement(l1=derived_inputs["L1"], l2=derived_inputs["L2"], l3=derived_inputs["L3"]),
    )
    return calculate_site_capacity(config, [])


def test_an_estimated_site_says_so_and_names_the_assumed_power_factor() -> None:
    result = _site({"L1": phase(2300.0), "L2": phase(1150.0), "L3": phase(-2300.0)})

    assert result.state == "observing"
    assert result.current_estimated is True
    assert result.estimated_power_factor == ESTIMATED_POWER_FACTOR
    assert dict(result.phase_current_basis) == {"L1": "estimated", "L2": "estimated", "L3": "estimated"}
    assert result.measured_phase_current_a["L3"] == pytest.approx(2300.0 / (VOLTAGE * 0.9))


def test_a_site_with_exact_sources_is_not_marked_estimated() -> None:
    result = _site({"L1": phase(2300.0, i=10.2), "L2": phase(1150.0, s=1200.0), "L3": phase(900.0, q=300.0)})

    assert result.current_estimated is False
    assert result.estimated_power_factor is None
    assert dict(result.phase_current_basis) == {"L1": "measured", "L2": "apparent", "L3": "reactive"}


def test_one_estimated_phase_marks_the_site_estimated() -> None:
    result = _site({"L1": phase(2300.0, i=10.0), "L2": phase(1150.0, i=5.0), "L3": phase(900.0)})

    assert result.current_estimated is True
    assert result.phase_current_basis["L3"] == "estimated"
    assert result.phase_current_basis["L1"] == "measured"


def test_the_estimated_headroom_is_never_larger_than_the_true_headroom_for_pf_at_least_0_9() -> None:
    fuse, margin = 25.0, 1.0
    for power_factor in (0.9, 0.95, 1.0):
        for watts in (1000.0, 3450.0, 5000.0):
            true_current = watts / (VOLTAGE * power_factor)
            result = _site(
                {"L1": phase(watts), "L2": phase(watts), "L3": phase(watts)},
                fuse=fuse,
            )
            assert result.measured_margin_a["L1"] <= fuse - margin - true_current + 1e-9


def test_the_existing_reactive_power_site_computes_as_before() -> None:
    """The owner's derived site (P, Q and V per phase, nothing else) is unchanged."""
    result = _site({"L1": phase(3000.0, q=1000.0), "L2": phase(0.0, q=0.0), "L3": phase(-1500.0, q=200.0)})

    assert result.current_estimated is False
    assert result.measured_phase_current_a["L1"] == pytest.approx(math.hypot(3000.0, 1000.0) / VOLTAGE)
    assert dict(result.phase_current_basis) == {"L1": "reactive", "L2": "reactive", "L3": "reactive"}


# ---- signed currents ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [-12.5, "-12.5", 12.5])
def test_a_signed_source_reads_the_magnitude(raw) -> None:
    assert classify_current(raw, "A", signed=True) == (12.5, None)


def test_an_unsigned_source_still_rejects_a_negative_current() -> None:
    assert classify_current(-12.5, "A") == (None, "invalid")


def test_a_signed_source_still_rejects_what_is_not_a_current() -> None:
    assert classify_current("abc", "A", signed=True) == (None, "invalid")
    assert classify_current(None, "A", signed=True) == (None, "missing")
    assert classify_current(-1.0, None, signed=True) == (None, "invalid")  # no unit
    assert classify_current(float("-inf"), "A", signed=True) == (None, "invalid")
    assert classify_current(-3000, "mA", signed=True) == (3.0, None)


# ---- apparent power -----------------------------------------------------------------------------


def test_apparent_power_units_and_refusals() -> None:
    assert classify_apparent_power("2415", "VA") == (2415.0, None)
    assert classify_apparent_power("2.415", "kVA") == (2415.0, None)
    assert classify_apparent_power("-5", "VA") == (None, "invalid")
    assert classify_apparent_power("5", "W") == (None, "invalid")
    assert classify_apparent_power("5", None) == (None, "invalid")
    assert classify_apparent_power("unavailable", "VA") == (None, "missing")


# ---- import/export pairs and inversion ----------------------------------------------------------


def test_a_pair_is_import_minus_export() -> None:
    assert combine_power_pair(pv(3000.0), pv(0.0)).value == 3000.0
    assert combine_power_pair(pv(0.0), pv(1800.0)).value == -1800.0
    assert combine_power_pair(pv(500.0), pv(200.0)).value == 300.0


def test_a_pair_needs_both_halves_and_never_reads_a_missing_half_as_zero() -> None:
    missing = PhaseValue(None, None, problem="missing")
    assert combine_power_pair(pv(3000.0), missing).problem == "missing"
    assert combine_power_pair(missing, pv(3000.0)).problem == "missing"
    invalid = PhaseValue(None, AGE, problem="invalid")
    assert combine_power_pair(pv(3000.0), invalid).problem == "invalid"
    assert combine_power_pair(pv(3000.0), invalid).value is None


def test_a_pair_is_as_old_as_its_older_half() -> None:
    result = combine_power_pair(pv(100.0, age=3.0), pv(0.0, age=90.0))

    assert result.age_s == 90.0
    assert result.report_age_s == 90.0


def test_one_unknown_age_makes_the_pair_age_unknown() -> None:
    unknown = PhaseValue(0.0, None, report_age_s=None)

    assert combine_power_pair(pv(100.0), unknown).age_s is None


def test_inversion_negates_a_single_entity_and_a_pair() -> None:
    assert combine_power_pair(pv(1800.0), invert=True).value == -1800.0
    assert combine_power_pair(pv(0.0), pv(1800.0), invert=True).value == 1800.0
    assert combine_power_pair(pv(-1800.0), invert=True).value == 1800.0
