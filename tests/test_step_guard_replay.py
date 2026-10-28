"""The night of 2026-10-17 replayed: a car stepped to the edge and was cut on a stale charger reading.

Site "My Home": derived phase current, main fuse 20 A, a three-phase OCPP car asking for 16 A. The
house took about L1 2.9 A, L2 1.5 A, L3 5.2 A. The site meter reads every few seconds; the charger
reports its own current about once a minute. The regulator's decision log showed, every minute:
`wrote 7 -> 14` (L3 12.13 A with the car at 7 A), then L3 at 19.04 A with the charger still reporting
6.94 A, `14 -> 9` and `9 -> 7` on that one reading, then 7 A held for the dwell, and again.

The plant here is that site; the real `calculate_site_capacity`, `allocate_regulator_decisions`,
`StepGuard` and `RegulatorDamper` (deadband 2 A, dwell 60 s, as configured) decide. A lowering is
urgent, as the yield stepper's `passthrough_raw_reduce` makes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.site.regulator import (
    allocate_regulator_decisions,
    MUST_LOWER_REASONS,
)
from custom_components.spotnav.site.regulator_damping import RegulatorDamper
from custom_components.spotnav.site.site_capacity import (
    calculate_site_capacity,
    ChargerRequest,
    DerivedPhaseInput,
    DerivedPhaseMeasurement,
    DirectPhaseMeasurement,
    PHASES,
    PhaseValue,
    SiteCalculationConfig,
)
from custom_components.spotnav.site.step_guard import StepGuard, TRIM_MAX_OVER_A

VOLTAGE = 230.0
FUSE_A = 20.0
REQUESTED_A = 16.0
TICK_S = 5.0
CHARGER_REPORT_S = 60.0
SITE_AGE_S = 0.5
# The house without the car, from the log (site minus the charger's own reading).
HOUSE_A = {"L1": 2.90, "L2": 1.49, "L3": 5.19}


@dataclass
class Night:
    safety_margin_a: float
    guarded: bool = True
    house_a: dict[str, float] = field(default_factory=lambda: dict(HOUSE_A))
    t: float = 0.0
    written: float = 7.0
    draw: float = 7.0
    charger_reading: float = 6.94
    charger_read_at: float = 0.0
    writes: list[tuple[float, float, float]] = field(default_factory=list)
    worst_over_a: list[tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.start = datetime(2026, 10, 17, 20, 30, tzinfo=timezone.utc)
        self.damper = RegulatorDamper(deadband_a=2.0, dwell_s=60.0, now=lambda: self.start + timedelta(seconds=self.t))
        self.damper.record_write(self.written)
        self.guard = StepGuard()

    @property
    def limit_a(self) -> float:
        return FUSE_A - self.safety_margin_a

    def site_a(self, phase: str) -> float:
        return self.house_a[phase] + self.draw

    def tick(self) -> None:
        self.t += TICK_S
        # The car takes what it was set to within a tick; the charger reports it once a minute.
        self.draw = self.written
        if self.t - self.charger_read_at >= CHARGER_REPORT_S:
            self.charger_reading = self.draw
            self.charger_read_at = self.t
        self.worst_over_a.append((self.t, max(self.site_a(p) for p in PHASES) - self.limit_a))
        self.decide()

    def decide(self) -> None:
        derived = DerivedPhaseMeasurement(
            **{
                phase.lower(): DerivedPhaseInput(
                    active_power_w=PhaseValue(self.site_a(phase) * VOLTAGE, SITE_AGE_S),
                    reactive_power_var=PhaseValue(0.0, SITE_AGE_S),
                    voltage_v=PhaseValue(VOLTAGE, SITE_AGE_S),
                )
                for phase in PHASES
            }
        )
        age = self.t - self.charger_read_at
        reading = PhaseValue(self.charger_reading, age)
        request = ChargerRequest(
            charger_entry_id="halo",
            requested_current_a=REQUESTED_A,
            phases=3,
            measured_current_a=DirectPhaseMeasurement(l1=reading, l2=reading, l3=reading),
        )
        site = calculate_site_capacity(
            SiteCalculationConfig(
                main_fuse_a=FUSE_A,
                safety_margin_a=self.safety_margin_a,
                measurement_mode="derived_phase_current",
                max_age_s=120.0,
                derived=derived,
            ),
            [request],
        )
        decision = allocate_regulator_decisions(
            site_result=site,
            requests=[request],
            voltage_by_phase={phase: VOLTAGE for phase in PHASES},
            confirmed_direction_by_phase={phase: "net_import" for phase in PHASES},
            max_age_s=120.0,
        )["halo"]
        proposed = decision.proposed_current_a
        assert proposed is not None, decision.reason
        margins = [site.measured_margin_a[phase] for phase in PHASES]
        margin = None if any(m is None for m in margins) else min(margins)
        site_read_at = self.t - SITE_AGE_S
        if self.guarded:
            proposed = self.guard.guard(
                now=self.t,
                proposed_a=proposed,
                previous_a=self.written,
                requested_a=REQUESTED_A,
                margin_a=margin,
                charger_read_at=self.charger_read_at,
                site_read_at=site_read_at,
                min_current_a=6.0,
            ).proposed_a
        damping = self.damper.consider(
            proposed_current_a=proposed,
            margin_a=margin,
            urgent=proposed < self.written or decision.reason in MUST_LOWER_REASONS,
        )
        if damping.write and damping.current_a is not None and round(damping.current_a) != self.written:
            setpoint = float(round(damping.current_a))
            self.writes.append((self.t, self.written, setpoint))
            if self.guarded:
                self.guard.note_write(now=self.t, previous_a=self.written, written_a=setpoint, site_read_at=site_read_at)
            self.written = setpoint

    def run(self, seconds: float) -> None:
        end = self.t + seconds
        while self.t < end:
            self.tick()


def test_without_the_guard_the_logged_loop_comes_back() -> None:
    """The replay reproduces the night: a step to 14 A, a cut back to 7 A on the stale reading, every minute."""
    night = Night(safety_margin_a=1.0, guarded=False)
    night.run(15 * 60)
    lowerings = [w for w in night.writes if w[2] < w[1]]
    assert len(lowerings) >= 4
    assert {w[2] for w in lowerings} == {7.0}


@pytest.mark.parametrize(("margin_a", "settled_a"), [(1.0, 12.0), (0.0, 13.0)])
def test_the_logged_night_settles_one_step_below_the_edge_without_oscillating(margin_a: float, settled_a: float) -> None:
    night = Night(safety_margin_a=margin_a)
    night.run(15 * 60)
    # One step up after the dwell, and nothing after it.
    assert night.writes == [(65.0, 7.0, settled_a)]
    assert night.written == settled_a
    # The site never went over the limit (fuse less the site's own safety margin, 0 included).
    assert max(over for _, over in night.worst_over_a) <= 0.0


def test_a_real_overload_is_still_cut_at_once_and_far_enough() -> None:
    night = Night(safety_margin_a=0.0)
    night.run(5 * 60)
    assert night.written == 13.0
    # A heater on L3: the site goes 3.8 A over the fuse.
    night.house_a["L3"] += 4.6
    night.tick()
    assert night.worst_over_a[-1][1] > TRIM_MAX_OVER_A
    assert night.writes[-1][0] == night.t and night.written < 13.0
    night.tick()
    # Under the fuse again on the very next reading.
    assert night.worst_over_a[-1][1] <= 0.0


def test_a_step_that_overshoots_is_trimmed_by_its_excess_and_not_tried_again() -> None:
    """The house rises just as the car steps: the one cut takes off what is over (plus a step), on a
    charger reading that does not show the step yet, and the level that overshot is not tried again."""
    night = Night(safety_margin_a=0.0)
    night.run(65)
    assert night.writes == [(65.0, 7.0, 13.0)]
    night.house_a["L3"] += 2.0  # L3 at 20.19 A on the next reading
    night.run(10 * 60)
    assert night.writes == [(65.0, 7.0, 13.0), (70.0, 13.0, 11.0)]
    assert max(over for t, over in night.worst_over_a if t > 70.0) <= 0.0


def test_one_reading_gets_one_cut() -> None:
    guard = StepGuard()
    guard.note_write(now=0.0, previous_a=7.0, written_a=14.0, site_read_at=0.0)
    first = guard.guard(
        now=5.0, proposed_a=9.0, previous_a=14.0, requested_a=16.0, margin_a=-0.5, charger_read_at=-30.0, site_read_at=4.5
    )
    assert (first.proposed_a, first.rule) == (12.0, "trimmed_overshoot")
    guard.note_write(now=5.0, previous_a=14.0, written_a=12.0, site_read_at=4.5)
    second = guard.guard(
        now=6.0, proposed_a=7.0, previous_a=12.0, requested_a=16.0, margin_a=-0.5, charger_read_at=-30.0, site_read_at=4.5
    )
    assert (second.proposed_a, second.rule) == (12.0, "held_same_reading")
    # The level that overshot is not tried again for the backoff.
    again = guard.guard(
        now=120.0, proposed_a=16.0, previous_a=12.0, requested_a=16.0, margin_a=5.0, charger_read_at=100.0, site_read_at=119.5
    )
    assert (again.proposed_a, again.rule) == (13.0, "overshoot_backoff")


def test_a_large_overload_passes_through_even_on_the_same_reading() -> None:
    guard = StepGuard()
    guard.note_write(now=0.0, previous_a=7.0, written_a=14.0, site_read_at=0.0)
    guard.note_write(now=5.0, previous_a=14.0, written_a=12.0, site_read_at=4.5)
    result = guard.guard(
        now=6.0, proposed_a=6.0, previous_a=12.0, requested_a=16.0, margin_a=-3.0, charger_read_at=-30.0, site_read_at=4.5
    )
    assert (result.proposed_a, result.rule) == (6.0, None)


def test_a_raise_held_by_the_request_is_not_cut_short() -> None:
    guard = StepGuard()
    result = guard.guard(
        now=0.0, proposed_a=16.0, previous_a=10.0, requested_a=16.0, margin_a=8.0, charger_read_at=0.0, site_read_at=0.0
    )
    assert (result.proposed_a, result.rule) == (16.0, None)
