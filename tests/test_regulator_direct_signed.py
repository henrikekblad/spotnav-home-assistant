"""The regulator on a direct-measurement site whose per-phase current carries a confirmed sign
(`site/current_sign.py`, `SiteCapacityResult.phase_direction_source`)."""

from __future__ import annotations

import pytest

from custom_components.spotnav.site.regulator import (
    allocate_regulator_decisions,
    applyability_failure,
    decide_charging_current,
)
from custom_components.spotnav.site.site_capacity import (
    DIRECTION_NOMINAL_PHASE_VOLTAGE_V,
    PHASES,
    ChargerRequest,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PhaseValue,
    SiteCalculationConfig,
    calculate_site_capacity,
)

AGE = 0.5
MAX_AGE = 30.0
V = DIRECTION_NOMINAL_PHASE_VOLTAGE_V


def _three(values, age: float = AGE) -> DirectPhaseMeasurement:
    l1, l2, l3 = values
    return DirectPhaseMeasurement(l1=PhaseValue(l1, age), l2=PhaseValue(l2, age), l3=PhaseValue(l3, age))


def _direct_site(
    currents: tuple[float, float, float],
    *,
    fuse: float = 16.0,
    margin: float = 1.0,
    confirmed=PHASES,
    signed: bool = True,
    age: float = AGE,
):
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=fuse,
        safety_margin_a=margin,
        measurement_mode="direct_phase_current",
        max_age_s=MAX_AGE,
        direct=_three(tuple(abs(value) for value in currents), age),
        direct_signed=_three(currents, age) if signed else None,
        direct_sign_confirmed=frozenset(confirmed),
    )
    return calculate_site_capacity(config, [])


def _derived_site(currents: tuple[float, float, float], *, fuse: float = 16.0, margin: float = 1.0):
    """The same site measured in derived mode: active power `I x V`, no reactive power."""

    def phase(current: float) -> DerivedPhaseInput:
        return DerivedPhaseInput(
            active_power_w=PhaseValue(current * V, AGE),
            reactive_power_var=PhaseValue(0.0, AGE),
            voltage_v=PhaseValue(V, AGE),
        )

    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=fuse,
        safety_margin_a=margin,
        measurement_mode="derived_phase_current",
        max_age_s=MAX_AGE,
        derived=DerivedPhaseMeasurement(l1=phase(currents[0]), l2=phase(currents[1]), l3=phase(currents[2])),
    )
    return calculate_site_capacity(config, [])


def _site(mode: str, currents, **kwargs):
    return _direct_site(currents, **kwargs) if mode == "direct" else _derived_site(currents, **kwargs)


def _single_phase(phase: str, measured: float, requested: float, *, basis: str = "chosen") -> ChargerRequest:
    values = {p: (measured if p == phase else 0.0) for p in PHASES}
    return ChargerRequest(
        charger_entry_id="charger",
        requested_current_a=requested,
        phases=1,
        phase=phase,  # type: ignore[arg-type]
        measured_current_a=_three((values["L1"], values["L2"], values["L3"])),
        phase_basis=basis,  # type: ignore[arg-type]
    )


def _three_phase(measured: float, requested: float) -> ChargerRequest:
    return ChargerRequest(
        charger_entry_id="charger",
        requested_current_a=requested,
        phases=3,
        measured_current_a=_three((measured, measured, measured)),
    )


def _decide(site, request, confirmed: str | None = "net_import"):
    return decide_charging_current(
        site_result=site,
        request=request,
        voltage_by_phase={phase: V for phase in PHASES},
        confirmed_direction_by_phase={phase: confirmed for phase in PHASES},
        max_age_s=MAX_AGE,
    )


# The site that asked for this: a Tibber Pulse, L1 and L2 exporting, L3 importing, 16 A fuse, 1 A margin,
# an "Automatic" single-phase charger followed to L3.
USER_SITE = (-4.9, -6.1, 12.4)


def test_the_site_result_carries_the_confirmed_signed_current_as_power() -> None:
    site = _direct_site(USER_SITE, confirmed=("L1", "L3"))

    assert site.phase_direction_source == {"L1": "signed_current", "L2": None, "L3": "signed_current"}
    assert site.phase_signed_active_power_w["L1"] == pytest.approx(-4.9 * V)
    assert site.phase_signed_active_power_w["L2"] is None
    assert site.phase_signed_active_power_reason["L2"] == "missing"
    assert site.phase_signed_active_power_w["L3"] == pytest.approx(12.4 * V)
    assert site.phase_signed_active_power_age_s["L3"] == AGE
    assert site.phase_signed_current_a == {"L1": -4.9, "L2": -6.1, "L3": 12.4}
    # The fuse still judges |I|; the signed increase margin and the solar diagnostic stay derived-only.
    assert site.measured_phase_current_a == {"L1": 4.9, "L2": 6.1, "L3": 12.4}
    assert site.measured_margin_a["L3"] == pytest.approx(2.6)
    assert all(value is None for value in site.phase_signed_margin_a.values())
    assert all(value is None for value in site.phase_signed_active_power_diagnostic_w.values())


def test_an_unsigned_direct_site_knows_no_direction() -> None:
    site = _direct_site((5.0, 6.0, 12.4), signed=False)
    assert all(source is None for source in site.phase_direction_source.values())

    decision = _decide(site, _single_phase("L3", 10.0, 12.0, basis="measured"))
    assert decision.proposed_current_a is None
    assert decision.reason == "phase_direction_unknown"


def test_a_signed_site_not_yet_confirmed_knows_no_direction() -> None:
    decision = _decide(_direct_site(USER_SITE, confirmed=()), _single_phase("L3", 14.2, 16.0, basis="measured"))
    assert (decision.proposed_current_a, decision.reason) == (None, "phase_direction_unknown")


def test_the_charger_s_own_phase_must_be_confirmed() -> None:
    site = _direct_site(USER_SITE, confirmed=("L1", "L2"))
    decision = _decide(site, _single_phase("L3", 14.2, 16.0, basis="measured"))
    assert (decision.proposed_current_a, decision.reason, decision.limiting_phase) == (
        None,
        "phase_direction_unknown",
        "L3",
    )


def test_an_automatic_charger_on_the_worst_phase_needs_every_phase_confirmed() -> None:
    site = _direct_site(USER_SITE, confirmed=("L3",))
    worst = _single_phase("L3", 14.2, 16.0, basis="worst_case")
    followed = _single_phase("L3", 14.2, 16.0, basis="measured")

    assert _decide(site, worst).reason == "phase_direction_unknown"
    assert _decide(site, followed).reason == "increase_within_safe_uncredited_margin"


@pytest.mark.parametrize("mode", ["direct", "derived"])
def test_user_site_an_import_overload_on_l3_lowers_the_charger(mode: str) -> None:
    site = _site(mode, (-4.9, -6.1, 17.5))
    decisions = allocate_regulator_decisions(
        site_result=site,
        requests=[_single_phase("L3", 14.2, 16.0, basis="measured")],
        voltage_by_phase={phase: V for phase in PHASES},
        confirmed_direction_by_phase={phase: None for phase in PHASES},
        max_age_s=MAX_AGE,
    )
    decision = decisions["charger"]
    # 17.5 A against a 15 A ceiling: 2.5 A off the charger's 14.2 A, rounded down.
    assert decision.proposed_current_a == 11.0
    assert decision.reason == "reducing_current_due_to_active_import_overload"
    assert decision.limiting_phase == "L3"


@pytest.mark.parametrize("mode", ["direct", "derived"])
def test_user_site_an_increase_within_headroom_is_proposed(mode: str) -> None:
    decision = _decide(_site(mode, USER_SITE), _single_phase("L3", 14.2, 16.0, basis="measured"))
    # 2.6 A of headroom on L3 over the charger's 14.2 A: the requested 16 A fits.
    assert (decision.proposed_current_a, decision.reason) == (16.0, "increase_within_safe_uncredited_margin")


def test_user_site_an_increase_is_capped_by_the_magnitude_headroom() -> None:
    decision = _decide(_direct_site((-4.9, -6.1, 14.0)), _single_phase("L3", 10.0, 16.0, basis="measured"))
    # 1 A of headroom on |net|: export elsewhere is never credited on a direct site.
    assert (decision.proposed_current_a, decision.reason) == (11.0, "increase_within_safe_uncredited_margin")


@pytest.mark.parametrize("mode", ["direct", "derived"])
def test_user_site_a_decrease_on_a_phase_netting_to_export_is_refused(mode: str) -> None:
    decision = _decide(_site(mode, USER_SITE), _single_phase("L1", 10.0, 8.0))
    assert (decision.proposed_current_a, decision.reason) == (
        10.0,
        "decrease_would_increase_net_current_kept_unchanged",
    )


@pytest.mark.parametrize(
    ("site_current_a", "measured_a", "requested_a", "confirmed", "reason", "proposed"),
    [
        (1.5, 10.0, 8.0, "net_import", "near_zero_crossing_kept_unchanged", 10.0),
        (-1.5, 10.0, 8.0, "net_import", "near_zero_crossing_kept_unchanged", 10.0),
        (8.0, 10.0, 9.0, None, "insufficient_consecutive_confirmation_kept_unchanged", 10.0),
        (8.0, 10.0, 9.0, "near_zero", "insufficient_consecutive_confirmation_kept_unchanged", 10.0),
        (8.0, 10.0, 9.0, "net_import", "decrease_confirmed_safe", 9.0),
        (8.0, 10.0, 5.0, "net_import", "decrease_margin_exceeded_kept_unchanged", 10.0),
        (3.0, 10.0, 6.0, "net_import", "decrease_margin_exceeded_kept_unchanged", 10.0),
        (3.0, 10.0, 2.0, "net_import", "decrease_would_increase_net_current_kept_unchanged", 10.0),
        (-8.0, 10.0, 9.0, "net_import", "decrease_would_increase_net_current_kept_unchanged", 10.0),
    ],
)
def test_near_zero_and_confirmation_rules_match_derived_mode(
    site_current_a: float, measured_a: float, requested_a: float, confirmed, reason: str, proposed: float
) -> None:
    currents = (site_current_a, site_current_a, site_current_a)
    request = _three_phase(measured_a, requested_a)
    direct = _decide(_direct_site(currents, fuse=20.0), request, confirmed)
    derived = _decide(_derived_site(currents, fuse=20.0), request, confirmed)

    assert (derived.proposed_current_a, derived.reason) == (proposed, reason)
    assert (direct.proposed_current_a, direct.reason) == (derived.proposed_current_a, derived.reason)
    assert {phase: basis.direction for phase, basis in direct.basis.items()} == {
        phase: basis.direction for phase, basis in derived.basis.items()
    }


def test_a_direct_decrease_stays_within_the_worst_case_of_an_unknown_active_share() -> None:
    """|I| with a sign does not say how much is active: a decrease that derived mode allows on active power
    is kept when `|net| + drop` could pass the ceiling."""
    request = _single_phase("L3", 14.0, 12.0)
    derived = _decide(_derived_site((5.0, 5.0, 14.5)), request)
    direct = _decide(_direct_site((5.0, 5.0, 14.5)), request)

    assert (derived.proposed_current_a, derived.reason) == (12.0, "decrease_confirmed_safe")
    assert (direct.proposed_current_a, direct.reason) == (14.0, "decrease_margin_exceeded_kept_unchanged")
    # Within the headroom it goes through.
    within = _decide(_direct_site((5.0, 5.0, 12.0)), _single_phase("L3", 14.0, 12.0))
    assert (within.proposed_current_a, within.reason) == (12.0, "decrease_confirmed_safe")


def test_the_re_check_before_a_write_works_on_direct_signed_readings() -> None:
    decision = _decide(_direct_site(USER_SITE), _single_phase("L3", 14.2, 16.0, basis="measured"))
    assert decision.proposed_current_a == 16.0

    def failure(site):
        found = applyability_failure(
            decision=decision,
            signed_active_power_w=site.phase_signed_active_power_w,
            signed_active_power_age_s=site.phase_signed_active_power_age_s,
            signed_active_power_reason=site.phase_signed_active_power_reason,
            max_age_s=MAX_AGE,
        )
        return None if found is None else (found.kind, found.phase)

    assert failure(_direct_site(USER_SITE)) is None
    assert failure(_direct_site((-4.9, -6.1, -3.0))) == ("direction_changed", "L3")
    assert failure(_direct_site(USER_SITE, confirmed=("L1", "L2"))) == ("phase_unusable", "L3")
    assert failure(_direct_site(USER_SITE, age=MAX_AGE + 1)) == ("phase_unusable", "L3")
