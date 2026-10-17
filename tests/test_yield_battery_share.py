"""A car held at its minimum by a battery charging from the grid at the fuse (`execution/yield_stepping.py`).

The owner's night of 2026-10-17: fuse 20 A, margin 0, `car_first`, yield stepping on, a Sigenergy battery
charging from the grid at 7.3 kW (about 10.6 A per phase), the grid at 20.0-20.5 A on every phase, a 3-phase
OCPP charger at 7 A against a plan of 16 A. The regulator proposes 6 A (its overload branch), the controller
holds the car where it is (`held_battery_at_limit`), and nothing ever stepped it up: a probe was only
considered when the regulator proposed no lowering.

The plant here stands in for the rest of the path: the damper (a 60 s dwell for an unverified step up, at once
for a verified one or an urgent cut), the controller's hold of a reduction inside the held band, a charger
whose reading lags its setting, and a battery that does or does not give up what the car takes.
"""

from __future__ import annotations

from custom_components.spotnav.execution.yield_stepping import (
    YieldConfig,
    YieldObservation,
    YieldStepper,
    YieldVerdict,
)

PHASES = ("L1", "L2", "L3")
LIMIT_A = 20.0  # fuse 20 A, safety margin 0
CEILING_A = 23.0  # the default yield ceiling, 1.15 x the fuse
HELD_BAND_A = 0.5
REQUESTED_A = 16.0
GRID_BASE_A = {"L1": 20.46, "L2": 20.15, "L3": 20.20}
BATTERY_W = 7325.0
DWELL_S = 60.0
CAR_LAG_S = 5.0  # the car follows its setting
READING_EVERY_S = 30.0  # the charger reports its current
BATTERY_LAG_S = 5.0  # the battery's own regulation


class _Night:
    """The site, the car and the battery, one second per tick."""

    def __init__(self, *, yields: bool, battery_w: float = BATTERY_W, fresh: bool = True) -> None:
        self.stepper = YieldStepper(YieldConfig(ceiling_a=CEILING_A, real_excess_a=2.0))
        self.yields = yields
        self.battery_w0 = battery_w
        self.fresh = fresh
        self.now = 0.0
        self.assigned_a = 7.0
        self.car_a = 0.98 * 7.0  # what the car draws
        self.reported_a = self.car_a  # what the charger last reported
        self._reported_at = 0.0
        self._car_target: tuple[float, float] | None = None
        self._battery_drop_a = 0.0
        self._battery_drop_target: tuple[float, float] | None = None
        self._pending: tuple[float, float] | None = None  # damper: (value, since)
        self.writes: list[tuple[float, float, str]] = []
        self.verdicts: list[YieldVerdict] = []
        self.max_over_a = 0.0

    def grid_a(self) -> dict[str, float]:
        rise = self.car_a - 0.98 * 7.0 - self._battery_drop_a
        return {phase: GRID_BASE_A[phase] + rise for phase in PHASES}

    def battery_w(self) -> float:
        return self.battery_w0 - self._battery_drop_a * 230.0 * 3

    def _advance_plant(self) -> None:
        if self._car_target is not None and self.now >= self._car_target[1]:
            self.car_a = self._car_target[0]
            self._car_target = None
            if self.yields:
                taken = self.car_a - 0.98 * 7.0
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
        """The regulator: the charger's reading plus the limiting phase's margin, a whole amp."""
        margin = min(LIMIT_A - value for value in self.grid_a().values())
        raw = float(int(self.reported_a + margin))
        return min(raw, REQUESTED_A) if raw >= 6.0 else 0.0

    def _set(self, value: float, reason: str) -> None:
        self.assigned_a = value
        self._pending = None
        self.writes.append((self.now, value, reason))
        self._car_target = (0.98 * value, self.now + CAR_LAG_S)

    def tick(self) -> YieldVerdict:
        self.now += 1.0
        self._advance_plant()
        grid = self.grid_a()
        self.max_over_a = max(self.max_over_a, max(grid.values()) - LIMIT_A)
        battery_w = self.battery_w()
        observation = YieldObservation(
            now=self.now,
            site_current_a=grid,
            delivered_current_a={phase: self.reported_a for phase in PHASES},
            phases=PHASES,
            battery_charge_a=battery_w / 690.0 if battery_w > 0 else None,
            credit_allowed=True,
            battery_power_w=battery_w,
            battery_fresh=self.fresh,
            limit_a={phase: LIMIT_A for phase in PHASES},
        )
        raw = self._raw_a()
        verdict = self.stepper.observe(raw, REQUESTED_A, self.assigned_a, observation)
        self.verdicts.append(verdict)
        if verdict.action == "hold":
            return verdict
        value = verdict.current_a if verdict.action == "write" else raw
        assert value is not None
        if verdict.action == "passthrough" and value < self.assigned_a:
            # The controller holds a reduction while the grid is within the band above the limit and
            # the battery charges (`held_battery_at_limit`); anything above goes through.
            if verdict.reason != "passthrough_ceiling" and all(
                v <= LIMIT_A + HELD_BAND_A for v in grid.values()
            ):
                return verdict
        if value < self.assigned_a:
            self._set(value, verdict.reason)
        elif value > self.assigned_a:
            if verdict.battery_credit_a is not None:
                self._set(value, verdict.reason)
            elif self._pending is None or self._pending[0] != value:
                self._pending = (value, self.now)
            elif self.now - self._pending[1] >= DWELL_S:
                self._set(value, verdict.reason)
        else:
            self._pending = None
        return verdict

    def run(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end:
            self.tick()


def test_a_yielding_battery_lets_the_car_climb_to_its_plan_and_the_site_stays_within_the_allowance() -> None:
    night = _Night(yields=True)
    night.run(900.0)

    assert night.assigned_a == REQUESTED_A, night.writes
    reasons = [reason for _, _, reason in night.writes]
    assert reasons[0] == "probe_step"
    assert "confirmed_step" in reasons
    assert not any(reason.startswith("revert") for reason in reasons)
    # Never lowered on the way up.
    values = [7.0] + [value for _, value, _ in night.writes]
    assert values == sorted(values)
    # Over the limit by no more than the allowance (the held band plus one step) at any moment.
    assert night.max_over_a <= 2.5 + 1e-9
    assert any(verdict.battery_verified for verdict in night.verdicts)
    reached = next(t for t, value, _ in night.writes if value == REQUESTED_A)
    assert reached <= 600.0


def test_a_battery_that_does_not_yield_gets_one_probe_then_the_backoff() -> None:
    night = _Night(yields=False)
    night.run(600.0)

    # One step to 9 A, taken back as soon as it is measured, and no other step up for the backoff (the
    # regulator may still cut on the charger's reading of the step it has not caught up with).
    assert [(value, reason) for _, value, reason in night.writes][:2] == [
        (9.0, "probe_step"),
        (7.0, "revert_not_absorbed"),
    ]
    assert [reason for _, _, reason in night.writes].count("probe_step") == 1
    up_at, back_at = night.writes[0][0], night.writes[1][0]
    assert back_at - up_at <= 90.0
    assert night.max_over_a <= 2.5 + 1e-9
    assert night.verdicts[-1].state == "backoff"

    # The backoff doubles: 10 minutes, then 20.
    night.run(1800.0)
    probes = [t for t, _, reason in night.writes if reason == "probe_step"]
    assert len(probes) == 3
    assert probes[1] - probes[0] >= 600.0 and probes[2] - probes[1] >= 1200.0


def test_a_probe_the_car_reading_never_shows_is_taken_back(monkeypatch) -> None:
    # The car's reading freezes: an OCPP charger that stopped reporting.
    monkeypatch.setattr(f"{__name__}.READING_EVERY_S", 10_000.0)
    night = _Night(yields=True)
    night.run(300.0)
    reasons = [reason for _, _, reason in night.writes]
    assert reasons[:2] == ["probe_step", "revert_probe_not_seen"]
    assert night.assigned_a == 7.0


def test_no_step_into_the_battery_share_on_a_stale_or_small_battery_reading() -> None:
    for night in (_Night(yields=True, fresh=False), _Night(yields=True, battery_w=1000.0)):
        night.run(600.0)
        assert night.writes == []
        assert night.verdicts[-1].reason == "passthrough_raw_reduce"


def test_no_step_when_the_site_is_already_above_the_held_band() -> None:
    night = _Night(yields=True)
    for phase in PHASES:
        GRID_BASE_A[phase] += 0.4  # L1 at 20.86 A: a step would leave it 2.86 A over
    try:
        night.run(300.0)
    finally:
        for phase in PHASES:
            GRID_BASE_A[phase] -= 0.4
    # The reduction goes through as before: no step is tried first.
    assert night.writes and night.writes[0][2] == "passthrough_raw_reduce"
    assert night.writes[0][0] <= 2.0


def test_a_step_that_goes_over_the_allowance_is_taken_back_at_once() -> None:
    night = _Night(yields=False)
    # Something else on the site switches on as the step lands.
    original = night.grid_a

    def grid_with_load() -> dict[str, float]:
        grid = original()
        return {phase: value + (0.5 if night.assigned_a > 7.0 else 0.0) for phase, value in grid.items()}

    night.grid_a = grid_with_load  # type: ignore[method-assign]
    night.run(200.0)
    reasons = [reason for _, _, reason in night.writes]
    assert reasons[:2] == ["probe_step", "revert_over_allowance"]
    up_at, back_at = night.writes[0][0], night.writes[1][0]
    assert back_at - up_at <= CAR_LAG_S + 1.0
