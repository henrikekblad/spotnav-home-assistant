"""Closed-loop fuse protection with signed, split and estimated sources.

A small plant (house load per phase with a power factor, solar export, one three-phase EV) is
simulated against the real `calculate_site_capacity` and `allocate_regulator_decisions`: each cycle
the meter's readings are built the way the controller builds them for the source style under test
(`combine_power_pair`, `classify_current(signed=True)`, ...), the regulator decides, and the decided
current becomes the EV's draw. The true per-phase current of the plant, `hypot(P, Q) / U`, is checked
against the main fuse after the regulator has acted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from custom_components.spotnav.site.measurement_source import combine_power_pair
from custom_components.spotnav.site.regulator import (
    allocate_regulator_decisions,
    classify_direction,
    DEFAULT_ZERO_MARGIN_W,
)
from custom_components.spotnav.site.site_capacity import (
    calculate_site_capacity,
    ChargerRequest,
    classify_current,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PHASES,
    PhaseValue,
    SiteCalculationConfig,
)

FUSE_A = 25.0
MARGIN_A = 1.0
VOLTAGE = 230.0
AGE = 2.0
MAX_AGE = 120.0
REQUESTED_A = 16.0


def pv(value: float) -> PhaseValue:
    return PhaseValue(value, AGE, None, report_age_s=AGE)


@dataclass
class Plant:
    """Signed house power per phase (negative = net solar export) at one power factor, plus the EV."""

    house_w: dict[str, float]
    power_factor: float = 0.95
    ev_a: float = 0.0

    def active_w(self, phase: str) -> float:
        return self.house_w[phase] + VOLTAGE * self.ev_a

    def reactive_var(self, phase: str) -> float:
        # Only the house is reactive; the EV charges at unity power factor.
        return abs(self.house_w[phase]) * math.tan(math.acos(self.power_factor))

    def true_current_a(self, phase: str) -> float:
        return math.hypot(self.active_w(phase), self.reactive_var(phase)) / VOLTAGE

    def worst_true_current_a(self) -> float:
        return max(self.true_current_a(phase) for phase in PHASES)


def derived_input(plant: Plant, phase: str, style: str, *, export_positive: bool = False, invert: bool = False) -> DerivedPhaseInput:
    p = plant.active_w(phase)
    q = plant.reactive_var(phase)
    s = math.hypot(p, q)
    i = s / VOLTAGE
    # What the meter writes: export-positive meters flip the sign of P.
    reported_p = -p if export_positive else p
    split = style.startswith("split_")
    style = style.removeprefix("split_")
    if split:
        power = combine_power_pair(pv(max(reported_p, 0.0)), pv(max(-reported_p, 0.0)), invert=invert)
    else:
        power = combine_power_pair(pv(reported_p), invert=invert)
    reactive = apparent = current = None
    if style == "reactive":
        reactive = pv(q)
    elif style == "apparent":
        apparent = pv(s)
    elif style == "measured_signed_current":
        # Signed current: negative while the phase nets to export.
        raw = -i if p < 0 else i
        value, problem = classify_current(raw, "A", signed=True)
        assert problem is None
        current = pv(value)
    return DerivedPhaseInput(
        active_power_w=power,
        reactive_power_var=reactive,
        voltage_v=pv(VOLTAGE),
        apparent_power_va=apparent,
        current_a=current,
    )


def cycle(plant: Plant, style: str, **kwargs) -> tuple[object, object]:
    """One regulator cycle: read, decide, apply the decision to the EV. Returns `(site, decision)`."""
    derived = DerivedPhaseMeasurement(
        l1=derived_input(plant, "L1", style, **kwargs),
        l2=derived_input(plant, "L2", style, **kwargs),
        l3=derived_input(plant, "L3", style, **kwargs),
    )
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=FUSE_A,
        safety_margin_a=MARGIN_A,
        measurement_mode="derived_phase_current",
        max_age_s=MAX_AGE,
        derived=derived,
    )
    request = ChargerRequest(
        charger_entry_id="c1",
        requested_current_a=REQUESTED_A,
        phases=3,
        measured_current_a=DirectPhaseMeasurement(
            l1=pv(plant.ev_a), l2=pv(plant.ev_a), l3=pv(plant.ev_a)
        ),
    )
    site = calculate_site_capacity(config, [request])
    assert site.state == "observing", (site.state, site.reason)
    signed = site.phase_signed_active_power_w
    decisions = allocate_regulator_decisions(
        site_result=site,
        requests=[request],
        voltage_by_phase={phase: VOLTAGE for phase in PHASES},
        confirmed_direction_by_phase={
            phase: classify_direction(signed[phase], DEFAULT_ZERO_MARGIN_W) for phase in PHASES
        },
        max_age_s=MAX_AGE,
    )
    decision = decisions["c1"]
    if decision.proposed_current_a is not None:
        plant.ev_a = decision.proposed_current_a
    return site, decision


#: House profiles, in steps: light, heavy, one busy phase, light again, heavy on all phases, light.
#: Each one fits under the fuse without the car, even at a power factor of 0.9.
PROFILES = [
    {"L1": 1500.0, "L2": 1200.0, "L3": 900.0},
    {"L1": 3800.0, "L2": 2500.0, "L3": 3100.0},
    {"L1": 4500.0, "L2": 900.0, "L3": 600.0},
    {"L1": 600.0, "L2": 500.0, "L3": 800.0},
    {"L1": 4200.0, "L2": 4100.0, "L3": 4300.0},
    {"L1": 1800.0, "L2": 1500.0, "L3": 1100.0},
]

EXACT_STYLES = ["reactive", "apparent", "measured_signed_current", "split_reactive", "split_apparent", "split_measured_signed_current"]
ESTIMATED_STYLES = ["estimated_signed", "split_estimated"]


def run_profiles(style: str, power_factor: float, *, scale: float = 1.0, **kwargs) -> list[Plant]:
    """Every profile in turn, scaled so the house alone stays under the fuse at this power factor."""
    plant = Plant({phase: watts * scale for phase, watts in PROFILES[0].items()}, power_factor=power_factor)
    after_reaction: list[Plant] = []
    for profile in PROFILES:
        plant.house_w = {phase: watts * scale for phase, watts in profile.items()}
        # Two cycles per change: the regulator's reaction, then its settling.
        for _ in range(2):
            cycle(plant, style, **kwargs)
        after_reaction.append(Plant(dict(plant.house_w), plant.power_factor, plant.ev_a))
    return after_reaction


@pytest.mark.parametrize("style", EXACT_STYLES + ESTIMATED_STYLES)
@pytest.mark.parametrize("power_factor", [0.9, 0.95, 1.0])
def test_the_true_current_stays_under_the_fuse_for_every_source_style_at_power_factor_0_9_or_better(
    style: str, power_factor: float
) -> None:
    for state in run_profiles(style, power_factor):
        assert state.worst_true_current_a() <= FUSE_A + 1e-6, (style, power_factor, state)


def test_split_and_signed_power_give_identical_decisions() -> None:
    signed_plant = Plant(dict(PROFILES[1]), power_factor=0.95, ev_a=10.0)
    split_plant = Plant(dict(PROFILES[1]), power_factor=0.95, ev_a=10.0)

    signed_site, signed_decision = cycle(signed_plant, "estimated_signed")
    split_site, split_decision = cycle(split_plant, "split_estimated")

    assert dict(signed_site.measured_phase_current_a) == dict(split_site.measured_phase_current_a)
    assert signed_decision.proposed_current_a == split_decision.proposed_current_a


@pytest.mark.parametrize("style", ["estimated_signed", "split_estimated"])
def test_an_estimated_site_says_it_is_estimated_at_every_cycle(style: str) -> None:
    plant = Plant(dict(PROFILES[1]), power_factor=0.95)

    site, _ = cycle(plant, style)

    assert site.current_estimated is True
    assert site.estimated_power_factor == 0.9
    assert set(site.phase_current_basis.values()) == {"estimated"}


def test_an_exact_site_is_not_marked_estimated() -> None:
    plant = Plant(dict(PROFILES[1]), power_factor=0.95)

    site, _ = cycle(plant, "reactive")

    assert site.current_estimated is False


def test_below_a_power_factor_of_0_9_an_estimate_can_overshoot_and_the_site_says_it_is_estimated() -> None:
    """The limit of the conservative estimate, stated rather than hidden: at PF 0.6 the assumed 0.9
    understates the current, the fuse can be exceeded, and the state is flagged `estimated`."""
    exceeded = False
    flagged = True
    plant = Plant(dict(PROFILES[0]), power_factor=0.6)
    for profile in PROFILES:
        plant.house_w = dict(profile)
        for _ in range(2):
            site, _decision = cycle(plant, "estimated_signed")
            flagged = flagged and site.current_estimated and site.estimated_power_factor == 0.9
        exceeded = exceeded or plant.worst_true_current_a() > FUSE_A
    assert exceeded, "the test profile no longer demonstrates the limit"
    assert flagged


@pytest.mark.parametrize("style", EXACT_STYLES)
def test_with_an_exact_source_a_low_power_factor_does_not_break_protection(style: str) -> None:
    for state in run_profiles(style, 0.6, scale=0.6):
        assert state.worst_true_current_a() <= FUSE_A + 1e-6


def test_an_export_positive_meter_needs_its_inversion_to_see_an_import_overload() -> None:
    """Huawei-style power (export positive): with the stored inversion the regulator sees the import
    overload and reduces; without it the same readings look like export and it takes no action."""
    heavy = {"L1": 5200.0, "L2": 5200.0, "L3": 5200.0}

    inverted = Plant(dict(heavy), power_factor=1.0, ev_a=16.0)
    cycle(inverted, "estimated_signed", export_positive=True, invert=True)
    assert inverted.worst_true_current_a() <= FUSE_A

    uninverted = Plant(dict(heavy), power_factor=1.0, ev_a=16.0)
    _site, decision = cycle(uninverted, "estimated_signed", export_positive=True, invert=False)
    assert decision.proposed_current_a is None  # refuses to guess; the flag is what makes it act
    assert uninverted.ev_a == 16.0


@pytest.mark.parametrize("style", EXACT_STYLES + ESTIMATED_STYLES)
@pytest.mark.parametrize("house_w", [-3500.0, -5000.0])
def test_with_the_house_exporting_the_first_increase_never_overloads_the_fuse(
    style: str, house_w: float
) -> None:
    """An exporting phase loads the fuse by its magnitude: the car is let in only by the magnitude's
    headroom, whichever way the meter reports the sign (signed, split, export-positive)."""
    for kwargs in ({}, {"export_positive": True, "invert": True}):
        plant = Plant({phase: house_w for phase in PHASES}, power_factor=0.95)

        site, decision = cycle(plant, style, **kwargs)

        assert site.state == "observing"
        assert plant.worst_true_current_a() <= FUSE_A + 1e-6
        # The export magnitude, not zero, is what the headroom is taken from.
        assert site.measured_phase_current_a["L1"] >= abs(house_w) / (VOLTAGE * 1.0) - 1e-6


def test_the_estimate_is_the_same_whether_the_phase_imports_or_exports() -> None:
    importing = Plant({"L1": 3000.0, "L2": 3000.0, "L3": 3000.0}, power_factor=1.0)
    exporting = Plant({"L1": -3000.0, "L2": -3000.0, "L3": -3000.0}, power_factor=1.0)

    imp_site, _ = cycle(importing, "estimated_signed")
    exp_site, _ = cycle(exporting, "estimated_signed")

    assert imp_site.measured_phase_current_a == exp_site.measured_phase_current_a


# ---- direct mode with signed currents -----------------------------------------------------------


def direct_site(readings: dict[str, tuple[object, bool]]):
    values = {}
    for phase, (raw, signed) in readings.items():
        value, problem = classify_current(raw, "A", signed=signed)
        values[phase] = PhaseValue(value, AGE, problem, report_age_s=AGE)
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=FUSE_A,
        safety_margin_a=MARGIN_A,
        measurement_mode="direct_phase_current",
        max_age_s=MAX_AGE,
        direct=DirectPhaseMeasurement(l1=values["L1"], l2=values["L2"], l3=values["L3"]),
    )
    request = ChargerRequest("c1", REQUESTED_A, 3)
    return calculate_site_capacity(config, [request])


def test_an_exporting_phase_loads_the_fuse_by_its_magnitude_not_by_its_sign() -> None:
    site = direct_site({"L1": (-20.0, True), "L2": (5.0, True), "L3": (5.0, True)})

    assert site.state == "observing"
    assert site.measured_phase_current_a["L1"] == 20.0
    # A sign mistake (reading -20 as "less than zero") would have granted 44 A of headroom.
    assert site.measured_margin_a["L1"] == FUSE_A - MARGIN_A - 20.0
    assert site.allocations[0].proposed_current_a == 0.0  # 4 A of headroom is below the 6 A minimum


def test_without_the_signed_flag_a_negative_current_makes_the_site_invalid_and_proposes_nothing() -> None:
    site = direct_site({"L1": (-20.0, False), "L2": (5.0, False), "L3": (5.0, False)})

    assert site.state == "invalid_measurements"
    assert site.allocations[0].proposed_current_a is None



# ---- a solar site exporting on one phase while the car charges ------------------------------------

SIGNED_STYLES = ["reactive", "apparent", "measured_signed_current", "split_reactive", "estimated_signed"]


@pytest.mark.parametrize("style", SIGNED_STYLES)
def test_a_phase_netting_to_export_while_the_car_draws_more_than_the_net_reading_is_still_regulated(
    style: str,
) -> None:
    # L1: the house exports 3000 W, the car draws 10 A (2300 W): the net is 700 W of export (3 A), so
    # the car draws more than the net reading. That is normal on a solar site and is never refused.
    plant = Plant({"L1": -3000.0, "L2": 1500.0, "L3": 1500.0}, power_factor=1.0, ev_a=10.0)

    site, decision = cycle(plant, style)

    assert site.state == "observing"
    assert decision is not None and decision.proposed_current_a is not None
    # The request is 16 A and every phase has room for it, so the car is let up to it.
    assert decision.proposed_current_a == REQUESTED_A
    assert plant.worst_true_current_a() <= FUSE_A + 1e-6


@pytest.mark.parametrize("style", SIGNED_STYLES)
def test_an_overload_on_another_phase_is_still_reduced_while_one_phase_exports(style: str) -> None:
    # L2 carries 5200 W of house load plus the car's 16 A: 38 A, over the 25 A fuse. L1 nets to 3.6 A of
    # export, with the car drawing more than that. The overload must be acted on, not hidden behind L1.
    plant = Plant({"L1": -4500.0, "L2": 5200.0, "L3": 1000.0}, power_factor=1.0, ev_a=16.0)
    assert plant.true_current_a("L2") > FUSE_A

    site, decision = cycle(plant, style)

    assert site.state == "observing"
    assert decision is not None and decision.proposed_current_a is not None
    assert decision.reason in {
        "reducing_current_due_to_active_import_overload",
        "paused_safe_current_below_charger_minimum",
    }
    assert plant.ev_a < 16.0
    assert plant.worst_true_current_a() <= FUSE_A + 1e-6


@pytest.mark.parametrize("style", SIGNED_STYLES)
def test_the_true_current_stays_under_the_fuse_over_a_day_of_solar_export_and_charging(style: str) -> None:
    plant = Plant({"L1": 0.0, "L2": 0.0, "L3": 0.0}, power_factor=0.95, ev_a=6.0)
    for l1, l2, l3 in (
        (-3000.0, 1500.0, 1500.0),
        (-4500.0, -900.0, 600.0),
        (-900.0, 4800.0, 3000.0),
        (-5000.0, -5000.0, -5000.0),
        (2500.0, 4500.0, 1200.0),
    ):
        plant.house_w = {"L1": l1, "L2": l2, "L3": l3}
        for _ in range(3):
            site, _decision = cycle(plant, style)
            assert site.state == "observing"
        assert plant.worst_true_current_a() <= FUSE_A + 1e-6, plant


def test_a_magnitude_only_source_is_judged_with_the_car_additive_and_never_invalid() -> None:
    # Direct, unsigned: the site reads 5 A on L1 while the car reads 10 A there. A sign is unknown, so
    # the worst case is assumed (the car adds to |site|); nothing is credited and the reading is kept.
    values = {
        "L1": PhaseValue(5.0, AGE, None, report_age_s=AGE),
        "L2": PhaseValue(12.0, AGE, None, report_age_s=AGE),
        "L3": PhaseValue(12.0, AGE, None, report_age_s=AGE),
    }
    config = SiteCalculationConfig(
        enabled=True,
        main_fuse_a=FUSE_A,
        safety_margin_a=MARGIN_A,
        measurement_mode="direct_phase_current",
        max_age_s=MAX_AGE,
        direct=DirectPhaseMeasurement(l1=values["L1"], l2=values["L2"], l3=values["L3"]),
    )
    request = ChargerRequest(
        "c1",
        REQUESTED_A,
        3,
        measured_current_a=DirectPhaseMeasurement(l1=pv(10.0), l2=pv(10.0), l3=pv(10.0)),
    )

    site = calculate_site_capacity(config, [request])

    assert site.state == "observing"
    assert site.measured_margin_a == {"L1": 19.0, "L2": 12.0, "L3": 12.0}
    assert site.allocations[0].proposed_current_a == 12.0
    assert site.charger_reading_exceeds_site_phases == ("L1",)
    assert site.phase_signed_margin_a == {"L1": None, "L2": None, "L3": None}


def test_a_reduction_that_would_overload_an_exporting_phase_takes_no_action() -> None:
    # L1 exports 26 A without the car; lowering the car to fix L2's overload would push L1 over the fuse.
    plant = Plant({"L1": -6000.0, "L2": 5200.0, "L3": 1000.0}, power_factor=1.0, ev_a=16.0)

    _site, decision = cycle(plant, "reactive")

    assert decision.proposed_current_a is None
    assert plant.ev_a == 16.0
