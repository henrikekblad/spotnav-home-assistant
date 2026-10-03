"""A downward step the battery absorbs (`execution/yield_stepping.py`).

The owner's recorder of 2026-10-03 12:32-12:37: fuse 20 A, margin 0, `car_first`. A grid-charging
battery fills the grid to about 20 A per phase, whatever the car draws. The car started at 14.8 A and
was stepped down to 6 A, one amp or two every 30 s, with the grid flat and the battery taking each
freed amp. The stepper's clock is the observation's `now`; no test sleeps.
"""

from __future__ import annotations

import math

from custom_components.spotnav.execution.yield_stepping import (
    YieldConfig,
    YieldObservation,
    YieldStepper,
    YieldVerdict,
)

PHASES = ("L1", "L2", "L3")
LIMIT_A = 20.0
CEILING_A = 23.0
REQUESTED_A = 14.8
LAG_S = 3.0
ABSORB_W = 4600.0 * 3


class _Site:
    def __init__(self, *, absorbs: bool = True, credit_allowed: bool = True, start_a: float = 14.8) -> None:
        self.stepper = YieldStepper(YieldConfig(ceiling_a=CEILING_A))
        self.absorbs = absorbs
        self.credit_allowed = credit_allowed
        self.now = 0.0
        self.assigned_a = start_a
        self.delivered_a = 0.89 * start_a
        self._pending: list[tuple[float, float]] = []
        self.grid_override_a: float | None = None
        self.verdicts: list[YieldVerdict] = []
        self.writes: list[tuple[float, float, str]] = []
        self.hard_stops = 0

    def grid_a(self) -> float:
        if self.grid_override_a is not None:
            return self.grid_override_a
        if self.absorbs:
            return LIMIT_A
        # No battery effect: the grid follows the car, starting just inside the hysteresis.
        return LIMIT_A + 0.5 - (0.89 * 14.8 - self.delivered_a)

    def tick(self) -> YieldVerdict:
        self.now += 1.0
        while self._pending and self._pending[0][0] <= self.now:
            self.delivered_a = self._pending.pop(0)[1]
        grid = self.grid_a()
        # The regulator's overload branch: the measured current is at the limit, so lower.
        raw = float(max(6, math.floor(self.delivered_a - 0.01))) if grid >= LIMIT_A - 0.01 else REQUESTED_A
        raw = min(raw, REQUESTED_A)
        observation = YieldObservation(
            now=self.now,
            site_current_a={phase: grid for phase in PHASES},
            delivered_current_a={phase: self.delivered_a for phase in PHASES},
            phases=PHASES,
            battery_charge_a=15.0,
            credit_allowed=self.credit_allowed,
            battery_power_w=ABSORB_W + (14.8 - self.assigned_a) * 230.0 * 3,
            limit_a={phase: LIMIT_A for phase in PHASES},
        )
        verdict = self.stepper.observe(raw, REQUESTED_A, self.assigned_a, observation)
        self.verdicts.append(verdict)
        target: float | None = None
        if verdict.action == "write":
            target = verdict.current_a
        elif verdict.action == "passthrough" and raw < self.assigned_a:
            target = raw
        if target is not None and target != self.assigned_a:
            self.assigned_a = target
            self.writes.append((self.now, target, verdict.reason))
            self._pending.append((self.now + LAG_S, 0.89 * target))
            if target < 6.0:
                self.hard_stops += 1
        return verdict

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end:
            self.tick()


def test_a_battery_that_absorbs_each_freed_amp_does_not_ratchet_the_car_down() -> None:
    site = _Site()
    site.run(300.0)

    assert site.hard_stops == 0
    assert min(a for _, a, _ in site.writes) >= 12.0
    assert site.assigned_a == 14.8
    reasons = [verdict.reason for verdict in site.verdicts]
    assert "down_step_absorbed_by_battery" in reasons
    assert "held_at_limit_battery_absorbs" in reasons
    restore = next(w for w in site.writes if w[2] == "down_step_absorbed_by_battery")
    assert restore[1] == 14.8


def test_the_car_is_back_at_its_plan_within_a_few_minutes() -> None:
    site = _Site()
    site.run(120.0)

    assert site.assigned_a == 14.8


def test_a_down_step_that_does_lower_the_grid_keeps_lowering() -> None:
    site = _Site(absorbs=False)
    site.run(45.0)

    assert not any(verdict.reason == "down_step_absorbed_by_battery" for verdict in site.verdicts)
    assert not site.stepper._battery_absorbs
    lowered_to = site.assigned_a
    assert lowered_to < 14.8
    # Still at the limit: the next reduction passes straight through, as before.
    site.grid_override_a = LIMIT_A + 0.5
    verdict = site.tick()
    assert verdict.reason == "passthrough_raw_reduce"
    assert site.assigned_a < lowered_to


def test_battery_first_keeps_todays_behaviour() -> None:
    site = _Site(credit_allowed=False)
    site.run(300.0)

    assert not any(
        verdict.reason in ("down_step_absorbed_by_battery", "held_at_limit_battery_absorbs")
        for verdict in site.verdicts
    )
    # Today's ratchet: the stepper never holds the car up, the grid sitting at the limit lowers it.
    assert min(a for _, a, _ in site.writes) <= 9.0
    assert not any(verdict.reason.startswith("held_while_verifying") for verdict in site.verdicts)


def test_a_real_overload_still_lowers_or_stops_the_car() -> None:
    site = _Site()
    site.run(100.0)
    assert site.stepper._battery_absorbs

    # Above the limit plus the hysteresis, below the ceiling: lowered.
    site.grid_override_a = LIMIT_A + 2.0
    verdict = site.tick()
    assert verdict.reason not in ("held_at_limit_battery_absorbs", "held_while_verifying_down_step")
    assert verdict.action in ("passthrough", "write")

    # At the hard ceiling: the existing hard stop.
    site.grid_override_a = CEILING_A
    verdict = site.tick()
    assert verdict.reason in ("passthrough_ceiling", "transient_step_back")
    assert verdict.urgent
