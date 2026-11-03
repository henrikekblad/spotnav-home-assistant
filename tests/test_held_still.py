"""A derived phase whose active power sits still but counts as fresh on its voltage (`_held_still_companion`): trusted
for holding and lowering a charger, never for raising one (`regulator._decide_phase`), and never to widen the signed
room for an increase (`_signed_margin_per_phase`)."""

from __future__ import annotations

from custom_components.spotnav.site.regulator import decide_charging_current
from custom_components.spotnav.site.site_capacity import (
    _held_still_companion,
    calculate_site_capacity,
    ChargerRequest,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PhaseValue,
    SiteCalculationConfig,
)

MAX_AGE = 120.0
STILL = 550.0  # the active power last changed this long ago
LIVE = 3.0


def _pv(value, age=LIVE, report_age=1.0):
    return PhaseValue(value, age, None, report_age_s=report_age)


def _phase(power, *, power_age=LIVE, apparent=None, reactive=None):
    return DerivedPhaseInput(
        active_power_w=_pv(power, power_age),
        reactive_power_var=None if reactive is None else _pv(reactive),
        voltage_v=_pv(230.0),
        apparent_power_va=None if apparent is None else _pv(apparent),
    )


def _config(l1: DerivedPhaseInput) -> SiteCalculationConfig:
    return SiteCalculationConfig(
        main_fuse_a=20.0,
        safety_margin_a=1.0,
        measurement_mode="derived_phase_current",
        max_age_s=MAX_AGE,
        derived=DerivedPhaseMeasurement(l1=l1, l2=_phase(1000.0, reactive=0.0), l3=_phase(1000.0, reactive=0.0)),
    )


def _decide(config: SiteCalculationConfig, measured: float, requested: float):
    request = ChargerRequest(
        "car",
        requested,
        1,
        phase="L1",
        min_current_a=6.0,
        measured_current_a=DirectPhaseMeasurement(_pv(measured), _pv(0.0), _pv(0.0)),
    )
    result = calculate_site_capacity(config, [request])
    decision = decide_charging_current(
        site_result=result,
        request=request,
        voltage_by_phase={phase: 230.0 for phase in ("L1", "L2", "L3")},
        confirmed_direction_by_phase={phase: "net_import" for phase in ("L1", "L2", "L3")},
        max_age_s=MAX_AGE,
    )
    return result, decision


def test_a_held_still_phase_never_raises_a_charger_but_holds_and_lowers_it() -> None:
    still = _config(_phase(-23.0, power_age=STILL, reactive=0.0))
    result, decision = _decide(still, 6.0, 16.0)
    assert result.phase_signed_active_power_held_still["L1"] is not None
    assert (decision.proposed_current_a, decision.reason) == (6.0, "held_still_no_raise")
    # Asked for what it draws: kept, as ever.
    _result, kept = _decide(still, 10.0, 10.0)
    assert (kept.proposed_current_a, kept.reason) == (10.0, "no_change_requested")
    # Lowering on a still import is judged as before.
    importing = _config(_phase(1500.0, power_age=STILL, reactive=0.0))
    _result, lowered = _decide(importing, 10.0, 8.0)
    assert lowered.reason == "decrease_confirmed_safe" and lowered.proposed_current_a == 8.0
    # The phase's power changes again: the raise is judged on it as before.
    moving = _config(_phase(-23.0, reactive=0.0))
    result, raised = _decide(moving, 6.0, 16.0)
    assert result.phase_signed_active_power_held_still["L1"] is None
    assert raised.reason == "increase_within_safe_uncredited_margin" and raised.proposed_current_a > 6.0


def test_a_still_power_never_widens_the_room_for_an_increase() -> None:
    """The fuse review's case: P still near 0 W, the meter's apparent power live at 18 A on L1 of a 20 A fuse. A
    still P would have called the live magnitude reactive and allowed a raise to 11 A; now no signed margin is taken
    from it, and the held-still phase raises nothing."""
    config = _config(_phase(-23.0, power_age=STILL, apparent=18.0 * 230.0))
    result, decision = _decide(config, 6.0, 16.0)
    assert result.phase_signed_margin_a["L1"] is None
    assert decision.proposed_current_a == 6.0 and decision.reason == "held_still_no_raise"


def test_a_power_of_unknown_age_or_unknown_reporting_is_never_held_still() -> None:
    no_report = DerivedPhaseInput(PhaseValue(-23.0, STILL, report_age_s=None), None, _pv(230.0))
    no_age = DerivedPhaseInput(PhaseValue(-23.0, None, report_age_s=1.0), None, _pv(230.0))
    assert _held_still_companion(no_report, MAX_AGE) is None
    assert _held_still_companion(no_age, MAX_AGE) is None
