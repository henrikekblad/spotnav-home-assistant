"""The last amp to the plan, smaller than the regulator's deadband (`execution/yield_stepping.py`,
`site/regulator_damping.py`).

The owner's evening of 2026-10-17, 21:30: fuse 20 A, margin 0, deadband 2 A, dwell 60 s, a 3-phase car at
15 A against a plan of 16 A, a Sigenergy battery charging from the grid at about 2.4 kW and the grid at the
fuse. The stepper's probe to 16 A (one amp, cut short by the plan) was held by the damper's 2 A deadband
on every pass, so the car stayed at 15 A while the battery could have given way.

The plant drives the stepper and the real damper the way the site controller does; the controller's hold of
a reduction while the battery keeps the grid at the limit stands in as a band check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.spotnav.execution.yield_stepping import (
    YieldConfig,
    YieldObservation,
    YieldStepper,
    YieldVerdict,
)
from custom_components.spotnav.site.regulator_damping import RegulatorDamper

PHASES = ("L1", "L2", "L3")
LIMIT_A = 20.0
CEILING_A = 23.0
HELD_BAND_A = 0.5
REQUESTED_A = 16.0
START_A = 15.0
GRID_BASE_A = {"L1": 20.2, "L2": 20.0, "L3": 20.1}
DEADBAND_A = 2.0
DWELL_S = 60.0
CAR_LAG_S = 5.0
READING_EVERY_S = 30.0
BATTERY_LAG_S = 5.0
EPOCH = datetime(2026, 10, 17, 19, 30, tzinfo=UTC)


class _Evening:
    def __init__(self, *, yields: bool, battery_w: float = 2422.0, requested_a: float = REQUESTED_A) -> None:
        self.requested_a = requested_a
        self.stepper = YieldStepper(YieldConfig(ceiling_a=CEILING_A, real_excess_a=2.0))
        self.now = 0.0
        self.damper = RegulatorDamper(
            deadband_a=DEADBAND_A,
            dwell_s=DWELL_S,
            now=lambda: EPOCH + timedelta(seconds=self.now),
        )
        self.damper.record_write(START_A)
        self.yields = yields
        self.battery_w0 = battery_w
        self.assigned_a = START_A
        self.car_a = 0.98 * START_A
        self.reported_a = self.car_a
        self._reported_at = 0.0
        self._car_target: tuple[float, float] | None = None
        self._battery_drop_a = 0.0
        self._battery_drop_target: tuple[float, float] | None = None
        self.writes: list[tuple[float, float, str]] = []
        self.verdicts: list[YieldVerdict] = []

    def grid_a(self) -> dict[str, float]:
        rise = self.car_a - 0.98 * START_A - self._battery_drop_a
        return {phase: GRID_BASE_A[phase] + rise for phase in PHASES}

    def battery_w(self) -> float:
        return self.battery_w0 - self._battery_drop_a * 690.0

    def _advance_plant(self) -> None:
        if self._car_target is not None and self.now >= self._car_target[1]:
            self.car_a = self._car_target[0]
            self._car_target = None
            if self.yields:
                taken = self.car_a - 0.98 * START_A
                self._battery_drop_target = (
                    min(max(0.0, taken), self.battery_w0 / 690.0),
                    self.now + BATTERY_LAG_S,
                )
        if self._battery_drop_target is not None and self.now >= self._battery_drop_target[1]:
            self._battery_drop_a = self._battery_drop_target[0]
            self._battery_drop_target = None
        if self.now - self._reported_at >= READING_EVERY_S:
            self.reported_a = self.car_a
            self._reported_at = self.now

    def _raw_a(self) -> float:
        margin = min(LIMIT_A - value for value in self.grid_a().values())
        raw = float(int(self.reported_a + margin))
        return min(raw, self.requested_a) if raw >= 6.0 else 0.0

    def tick(self) -> None:
        self.now += 1.0
        self._advance_plant()
        grid = self.grid_a()
        battery_w = self.battery_w()
        observation = YieldObservation(
            now=self.now,
            site_current_a=grid,
            delivered_current_a={phase: self.reported_a for phase in PHASES},
            phases=PHASES,
            battery_charge_a=battery_w / 690.0 if battery_w > 0 else None,
            credit_allowed=True,
            battery_power_w=battery_w,
            battery_fresh=True,
            limit_a={phase: LIMIT_A for phase in PHASES},
        )
        raw = self._raw_a()
        verdict = self.stepper.observe(raw, self.requested_a, self.assigned_a, observation)
        self.verdicts.append(verdict)
        if verdict.action == "hold":
            return
        margin = min(LIMIT_A - value for value in grid.values())
        if verdict.action == "write":
            decision = self.damper.consider(
                proposed_current_a=verdict.current_a,
                margin_a=margin,
                urgent=verdict.urgent,
                verified_step=(
                    verdict.battery_credit_a is not None
                    or verdict.reason == "down_step_absorbed_by_battery"
                ),
                reaches_request=verdict.reaches_request,
            )
        else:
            if (
                raw < self.assigned_a
                and verdict.reason != "passthrough_ceiling"
                and all(value <= LIMIT_A + HELD_BAND_A for value in grid.values())
            ):
                return  # held_battery_at_limit
            decision = self.damper.consider(
                proposed_current_a=raw, margin_a=margin, urgent=verdict.urgent
            )
        if decision.write and decision.current_a is not None and decision.current_a != self.assigned_a:
            self.assigned_a = decision.current_a
            self.writes.append((self.now, decision.current_a, verdict.reason))
            self._car_target = (0.98 * decision.current_a, self.now + CAR_LAG_S)

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end:
            self.tick()


def test_the_last_amp_to_the_plan_is_taken_after_the_dwell_and_kept_when_the_battery_yields() -> None:
    evening = _Evening(yields=True)
    evening.run(600.0)

    assert evening.writes[0][1:] == (REQUESTED_A, "probe_step")
    assert evening.writes[0][0] >= DWELL_S
    assert [value for _, value, _ in evening.writes] == [REQUESTED_A]
    assert evening.assigned_a == REQUESTED_A


def test_the_last_amp_is_taken_back_when_the_battery_does_not_yield() -> None:
    evening = _Evening(yields=False)
    evening.run(300.0)

    assert [(value, reason) for _, value, reason in evening.writes][:2] == [
        (REQUESTED_A, "probe_step"),
        (START_A, "revert_not_absorbed"),
    ]
    assert evening.verdicts[-1].state == "backoff"


def test_no_last_amp_on_a_battery_below_the_threshold() -> None:
    evening = _Evening(yields=True, battery_w=1000.0)
    evening.run(600.0)

    assert not any(value > START_A for _, value, _ in evening.writes)
    assert not any(verdict.reason == "probe_step" for verdict in evening.verdicts)


def test_the_deadband_still_holds_a_small_regulator_raise() -> None:
    damper = RegulatorDamper(deadband_a=DEADBAND_A, dwell_s=0.0, now=lambda: EPOCH)
    damper.record_write(START_A)

    assert damper.consider(proposed_current_a=16.0, margin_a=3.0).reason == "deadband"
    assert damper.consider(proposed_current_a=16.0, margin_a=3.0, reaches_request=True).write


def test_a_whole_step_at_a_high_current_is_measured_from_before_it_was_written() -> None:
    # 15 A to 17 A: the site meter shows the step before the charger reports it, and the car reads
    # above `follow_ratio` of the new setting all along; the step must still be measured from before it.
    evening = _Evening(yields=False, requested_a=17.0)
    evening.run(300.0)

    assert [(value, reason) for _, value, reason in evening.writes][:2] == [
        (17.0, "probe_step"),
        (START_A, "revert_not_absorbed"),
    ]

    evening = _Evening(yields=True, requested_a=17.0)
    evening.run(300.0)

    assert [value for _, value, _ in evening.writes] == [17.0]
    assert evening.verdicts[-1].state == "confirmed"
