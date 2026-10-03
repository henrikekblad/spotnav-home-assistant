"""A grid-charging home battery that gives way to the car (`execution/yield_stepping.py`).

The sequence is the owner's recorder of 2026-10-03 11:28-11:46: fuse 20 A with a 1 A margin, the
battery holds the grid at about 20 A per phase (4.6 kW), and gives up exactly what the car takes:
car 15.5 A against battery 4.1 kW, car 6 A against battery 11 kW. The stepper's own clock is the
observation's `now`; no test sleeps.
"""

from __future__ import annotations

from custom_components.spotnav.execution.yield_stepping import (
    YieldConfig,
    YieldObservation,
    YieldStepper,
    YieldVerdict,
)

PHASES = ("L1", "L2", "L3")
GRID_A = 20.0
CEILING_A = 23.0
REQUESTED_A = 16.0
FOLLOW_LAG_S = 3.0


def _battery_a(delivered_a: float, *, yields: bool) -> float:
    """The battery's charge current per phase: 15.9 A with the car at its floor, 5.9 A at 15.5 A."""
    if not yields:
        return 15.9
    return max(0.0, 15.9 - (delivered_a - 0.89 * 6.0) * 10.0 / 9.6)


class _Plant:
    """The car, the battery and the grid, advanced one second at a time."""

    def __init__(self, *, yields: bool = True, credit_allowed: bool = True, **config: float) -> None:
        self.stepper = YieldStepper(YieldConfig(ceiling_a=CEILING_A, **config))
        self.yields = yields
        self.credit_allowed = credit_allowed
        self.now = 0.0
        self.assigned_a = 6.0
        self._delivered_a = 0.89 * 6.0
        self._pending: list[tuple[float, float]] = []
        self.grid_override_a: float | None = None
        self.verdicts: list[YieldVerdict] = []
        self.writes: list[tuple[float, float, str]] = []

    @property
    def delivered_a(self) -> float:
        return self._delivered_a

    def tick(self, raw_a: float | None = None) -> YieldVerdict:
        self.now += 1.0
        while self._pending and self._pending[0][0] <= self.now:
            self._delivered_a = self._pending.pop(0)[1]
        grid_a = GRID_A if self.yields else GRID_A + (self._delivered_a - 0.89 * 6.0)
        if self.grid_override_a is not None:
            grid_a = self.grid_override_a
        if raw_a is None:
            # The regulator's overload branch: current plus the (negative) uncredited margin,
            # a standing pause at the floor.
            raw_a = max(0.0, self._delivered_a - 1.2)
            raw_a = 0.0 if raw_a < 6.0 else raw_a
        observation = YieldObservation(
            now=self.now,
            site_current_a={phase: grid_a for phase in PHASES},
            delivered_current_a={phase: self._delivered_a for phase in PHASES},
            phases=PHASES,
            battery_charge_a=_battery_a(self._delivered_a, yields=self.yields),
            credit_allowed=self.credit_allowed,
        )
        verdict = self.stepper.observe(raw_a, REQUESTED_A, self.assigned_a, observation)
        self.verdicts.append(verdict)
        if verdict.action == "write" and verdict.current_a is not None:
            self.assigned_a = verdict.current_a
            self.writes.append((self.now, verdict.current_a, verdict.reason))
            delivered = 0.89 * verdict.current_a
            self._pending.append((self.now + FOLLOW_LAG_S, delivered))
        return verdict

    def run_until_plan(self, limit_s: float) -> float | None:
        while self.now < limit_s:
            self.tick()
            if self.assigned_a >= REQUESTED_A:
                return self.now
        return None


def _climb_to(plant: "_Plant", amps: float) -> None:
    while plant.assigned_a < amps:
        assert plant.now < 600.0, "the climb stalled"
        plant.tick()


def test_the_car_climbs_to_the_plan_within_a_few_minutes_with_a_yielding_battery() -> None:
    plant = _Plant()
    reached_at = plant.run_until_plan(300.0)

    assert reached_at is not None and reached_at <= 300.0
    assert plant.assigned_a == REQUESTED_A
    reasons = [reason for _, _, reason in plant.writes]
    assert reasons[0] == "probe_step"
    assert reasons.count("confirmed_step") >= 3
    # Verified by the battery, the later steps are larger than the unverified 2 A probe...
    steps = [b - a for (_, a, _), (_, b, _) in zip(plant.writes, plant.writes[1:])]
    assert max(steps) == 3.0
    # ... and near the plan they are back to 1 A.
    assert steps[-1] == 1.0
    assert any(verdict.battery_verified for verdict in plant.verdicts)


def test_the_credited_climb_is_faster_than_the_uncredited_one() -> None:
    credited = _Plant()
    uncredited = _Plant(credit_allowed=False)

    credited_at = credited.run_until_plan(900.0)
    uncredited_at = uncredited.run_until_plan(900.0)

    assert credited_at is not None and uncredited_at is not None
    assert credited_at < uncredited_at
    # `battery_first` keeps today's steps: 2 A, never credited.
    assert all(verdict.battery_credit_a is None for verdict in uncredited.verdicts)
    steps = [b - a for (_, a, _), (_, b, _) in zip(uncredited.writes, uncredited.writes[1:])]
    assert set(steps) <= {2.0}


def test_a_single_transient_steps_back_once_and_does_not_reset_to_the_floor() -> None:
    plant = _Plant()
    _climb_to(plant, 11.0)
    before = plant.assigned_a
    # The 11:41:47 sample: one L3 reading near 25 A while the battery lags.
    plant.grid_override_a = 25.0
    verdict = plant.tick()
    plant.grid_override_a = None

    assert (verdict.action, verdict.reason, verdict.urgent) == ("write", "transient_step_back", True)
    assert before - 3.0 <= plant.assigned_a < before
    assert plant.assigned_a > 6.0
    assert verdict.state == "confirmed"

    # The climb resumes and reaches the plan.
    assert plant.run_until_plan(plant.now + 300.0) is not None


def test_two_consecutive_over_ceiling_samples_still_stop_hard() -> None:
    plant = _Plant()
    _climb_to(plant, 11.0)
    plant.grid_override_a = 25.0
    first = plant.tick()
    second = plant.tick(raw_a=6.0)

    assert first.reason == "transient_step_back"
    assert (second.action, second.reason, second.urgent) == ("passthrough", "passthrough_ceiling", True)
    assert second.state == "backoff"


def test_a_large_overshoot_is_never_a_transient() -> None:
    plant = _Plant()
    _climb_to(plant, 11.0)
    plant.grid_override_a = CEILING_A + 6.0
    verdict = plant.tick(raw_a=6.0)

    assert (verdict.reason, verdict.urgent, verdict.state) == ("passthrough_ceiling", True, "backoff")


def test_repeated_transients_become_a_hard_stop() -> None:
    plant = _Plant()
    _climb_to(plant, 11.0)
    reasons = []
    for _ in range(3):
        plant.grid_override_a = 25.0
        reasons.append(plant.tick(raw_a=6.0).reason)
        plant.grid_override_a = None
        plant.tick()

    assert reasons[:2] == ["transient_step_back", "transient_step_back"]
    assert reasons[2] == "passthrough_ceiling"


def test_a_transient_before_yield_is_verified_is_the_hard_stop() -> None:
    plant = _Plant()
    plant.grid_override_a = 25.0
    verdict = plant.tick(raw_a=0.0)

    assert verdict.reason == "passthrough_ceiling"


def test_a_battery_that_does_not_yield_keeps_the_car_low() -> None:
    """Grid charging that does not give way: the grid follows the car, the step is not absorbed,
    it is reverted, and the car stays near the floor instead of climbing to the plan."""
    plant = _Plant(yields=False)
    plant.run_until_plan(900.0)

    assert plant.assigned_a < REQUESTED_A
    assert plant.assigned_a <= 8.0
    assert not any(verdict.battery_verified for verdict in plant.verdicts)
    # The revert restores the value from before the step, not the stepped one.
    assert [(a, r) for _, a, r in plant.writes][:2] == [(8.0, "probe_step"), (6.0, "revert_not_absorbed")]


def test_a_battery_that_stays_put_is_not_credited_even_if_the_grid_is_flat(monkeypatch) -> None:
    """The grid stays flat because something else gives way: the battery's own charge does not fall,
    so there is no credit and steps stay at 2 A."""
    monkeypatch.setattr(
        "tests.test_yield_battery_credit._battery_a", lambda delivered_a, *, yields: 12.0
    )
    plant = _Plant()
    plant.run_until_plan(900.0)

    assert not any(verdict.battery_verified for verdict in plant.verdicts)
    steps = [b - a for (_, a, _), (_, b, _) in zip(plant.writes, plant.writes[1:])]
    assert set(steps) <= {2.0}


def test_a_credited_step_never_exceeds_the_battery_charge_or_three_amps() -> None:
    plant = _Plant()
    plant.run_until_plan(300.0)

    credited = [verdict for verdict in plant.verdicts if verdict.battery_credit_a is not None]
    assert credited
    assert all(verdict.battery_credit_a >= 1.0 for verdict in credited)
    previous = 6.0
    for _, current_a, reason in plant.writes:
        if reason == "confirmed_step":
            assert current_a - previous <= 3.0
        previous = current_a
