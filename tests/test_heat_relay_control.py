"""SpotNav's own switching of the heat relay, pure (`heat/relay_control.py`): the fixed rules on the plan, and the
check before control runs (pass, fail, not judgeable and tried again)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.heat.relay_control import (
    EFFECT_GRACE,
    MAX_WRITES_DAY,
    PHASE_CLOSED,
    PHASE_OPENED,
    PHASE_WAIT,
    RETURN_GRACE,
    Check,
    Sample,
    Situation,
    decide,
    lowered_within,
    planned_run_end,
    verdict,
)
from custom_components.spotnav.heat.shadow_rule import BandHour

T0 = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)
H = timedelta(hours=1)


def plan(*lowered: int) -> list[BandHour]:
    return [BandHour(T0 + h * H, "lower" if h in lowered else "normal", 20.5, "dear_hour", 100.0) for h in range(-4, 8)]


def situation(now: datetime, **extra: object) -> Situation:
    base: dict[str, object] = dict(
        now=now, plan_until=None, held_since=None, last_end=None, lowered_today=timedelta(0), writes_today=0
    )
    base.update(extra)
    return Situation(**base)  # type: ignore[arg-type]


def test_the_planned_run_that_holds_now() -> None:
    assert planned_run_end(plan(0, 1, 2, 4), T0 + 10 * MIN) == T0 + 3 * H
    assert planned_run_end(plan(0, 1, 2, 4), T0 + 3 * H) is None
    assert planned_run_end(plan(), T0) is None


def test_a_lowering_starts_where_the_plan_lowers_and_lasts_at_least_45_minutes() -> None:
    start = decide(situation(T0, plan_until=T0 + 2 * H))
    assert (start.closed, start.reason, start.until) == (True, "planned", T0 + 2 * H)
    # Twenty minutes left of a planned hour: no quarter blip.
    assert decide(situation(T0 + 40 * MIN, plan_until=T0 + H)).reason == "too_short"
    # Once started it is held 45 minutes even when the plan turns normal earlier (prices changed).
    held = decide(situation(T0 + 30 * MIN, plan_until=None, held_since=T0))
    assert (held.closed, held.reason, held.until) == (True, "min_lower", T0 + 45 * MIN)
    assert decide(situation(T0 + 45 * MIN, plan_until=None, held_since=T0)).reason == "normal"


def test_at_most_three_hours_in_a_row_then_an_hour_back_to_normal() -> None:
    assert decide(situation(T0 + 3 * H, plan_until=T0 + 5 * H, held_since=T0)).reason == "max_run"
    assert decide(situation(T0, plan_until=T0 + 5 * H)).until == T0 + 3 * H
    rest = decide(situation(T0 + 30 * MIN, plan_until=T0 + 2 * H, last_end=T0))
    assert (rest.closed, rest.reason) == (False, "rest")
    assert decide(situation(T0 + H, plan_until=T0 + 2 * H, last_end=T0)).closed is True


def test_at_most_six_hours_a_day_and_a_cap_on_writes() -> None:
    assert decide(situation(T0, plan_until=T0 + 2 * H, lowered_today=timedelta(hours=5, minutes=30))).reason == "too_short"
    assert decide(situation(T0, plan_until=T0 + 2 * H, lowered_today=timedelta(hours=5))).until == T0 + H
    assert decide(situation(T0 + 50 * MIN, plan_until=T0 + 2 * H, held_since=T0, lowered_today=timedelta(hours=6))).reason == "day_cap"
    assert decide(situation(T0, plan_until=T0 + 2 * H, writes_today=MAX_WRITES_DAY - 1)).reason == "write_cap"
    # The cap never keeps a lowering from ending.
    assert decide(situation(T0 + H, plan_until=None, held_since=T0, writes_today=MAX_WRITES_DAY)).closed is False


@pytest.mark.parametrize("safety", ["too_cold", "room_cold", "stale", "sg_ready_b"])
def test_safety_releases_at_once_even_within_the_45_minutes(safety: str) -> None:
    released = decide(situation(T0 + 5 * MIN, plan_until=T0 + 2 * H, held_since=T0, safety=safety))
    assert (released.closed, released.reason) == (False, safety)
    assert decide(situation(T0, plan_until=T0 + 2 * H, safety=safety)).closed is False


def test_settling_data_starts_nothing_but_ends_nothing() -> None:
    assert decide(situation(T0, plan_until=T0 + 2 * H, safety="settling")).reason == "settling"
    assert decide(situation(T0 + 10 * MIN, plan_until=T0 + 2 * H, held_since=T0, safety="settling")).closed is True


def test_the_lowered_time_within_a_day() -> None:
    day = (T0 - 16 * H, T0 + 8 * H)
    lowerings = [(T0 - 17 * H, T0 - 15 * H), (T0, T0 + H), (T0 + 2 * H, None)]
    assert lowered_within(lowerings, day[0], T0 + 150 * MIN) == timedelta(hours=1, minutes=30) + H


# ------------------------------------------------------------------------------------------------ the check

HEATING = Sample(1.1, "heating")


def run(check: Check, start: datetime, minutes: int, sample: Sample, relay: bool | None) -> tuple[datetime, list]:
    """Step `minutes` looks, one a minute; returns the last moment and the steps."""
    steps = []
    moment = start
    for _ in range(minutes):
        steps.append(check.step(moment, sample, relay))
        moment += MIN
    return moment, steps


def outcome(steps: list) -> str | None:
    """The first try's end among the steps."""
    return next((step.outcome for step in steps if step.outcome is not None), None)


def to_closed(check: Check, sample: Sample = HEATING) -> datetime:
    """From waiting through the baseline to the closing."""
    moment, steps = run(check, T0, 6, sample, False)
    assert steps[-1].closed is True and check.phase == PHASE_CLOSED
    return moment


def _power_try(check: Check, start: datetime, measured: bool = False) -> str | None:
    """One power-only try from `start` whose closing shows an effect and whose opening a return."""
    moment, _ = run(check, start, 6, HEATING, False)
    assert check.phase == PHASE_CLOSED
    moment, steps = run(check, moment, 3, Sample(0.6, "heating"), True)
    assert check.phase == PHASE_OPENED and steps[-1].closed is False
    steps = [check.step(moment + i * MIN, Sample(1.0, "heating"), False, measured=measured) for i in range(2)]
    return outcome(steps)


def test_the_check_passes_on_the_meters_power_on_two_tries_apart() -> None:
    check = Check()
    # One try may meet the compressor's own pause: not judged yet, tried again at another time.
    assert _power_try(check, T0) == "not_judgeable:power_once" and check.phase == PHASE_WAIT
    assert _power_try(check, T0 + 2 * H) == "passed"


def test_the_check_passes_on_one_power_try_where_measured_closings_show_an_effect() -> None:
    assert _power_try(Check(), T0, measured=True) == "passed"


def test_the_check_passes_on_the_calculated_supply_alone() -> None:
    check = Check()
    moment = to_closed(check, Sample(supply_c=40.3))
    moment, steps = run(check, moment, 2, Sample(supply_c=35.7), True)
    assert check.phase == PHASE_OPENED
    _moment, steps = run(check, moment, 2, Sample(supply_c=40.1), False)
    assert steps[-1].outcome == "passed"


def test_no_effect_on_the_supply_fails() -> None:
    check = Check()
    moment = to_closed(check, Sample(supply_c=40.3))
    _moment, steps = run(check, moment, int(EFFECT_GRACE / MIN) + 1, Sample(supply_c=40.2), True)
    # Closed through the grace, then opened with the verdict.
    assert all(step.closed for step in steps[: int(EFFECT_GRACE / MIN) - 2])
    assert outcome(steps) == "failed:no_effect" and verdict(outcome(steps)) == "failed"


def test_no_effect_on_the_power_alone_is_never_a_failure() -> None:
    check = Check()
    moment = to_closed(check)
    _moment, steps = run(check, moment, int(EFFECT_GRACE / MIN) + 1, HEATING, True)
    assert outcome(steps) == "not_judgeable:no_effect"
    # Not again within the hour; a second try that shows nothing is still only not judgeable (tried again).
    assert check.step(T0 + 40 * MIN, HEATING, False).closed is False and check.phase == PHASE_WAIT
    later = T0 + 2 * H
    moment, _ = run(check, later, 6, HEATING, False)
    assert check.phase == PHASE_CLOSED
    _moment, steps = run(check, moment, int(EFFECT_GRACE / MIN) + 1, HEATING, True)
    assert outcome(steps) == "not_judgeable:no_effect"


def test_a_supply_that_does_not_come_back_fails_a_power_that_does_not_is_not_judgeable() -> None:
    check = Check()
    moment = to_closed(check, Sample(supply_c=40.3))
    moment, _ = run(check, moment, 2, Sample(supply_c=35.7), True)
    _moment, steps = run(check, moment, int(RETURN_GRACE / MIN) + 1, Sample(supply_c=35.7), False)
    assert outcome(steps) == "failed:no_return"
    check = Check()
    moment = to_closed(check)
    moment, _ = run(check, moment, 2, Sample(0.5, "heating"), True)
    _moment, steps = run(check, moment, int(RETURN_GRACE / MIN) + 1, Sample(0.2, "idle"), False)
    assert outcome(steps) == "not_judgeable:no_return"


def test_a_hot_water_run_or_a_block_makes_the_moment_unjudgeable() -> None:
    check = Check()
    moment = to_closed(check)
    step = check.step(moment, Sample(3.0, "hot_water"), True)
    assert (step.closed, step.outcome) == (False, "not_judgeable:hot_water")
    check = Check()
    moment = to_closed(check)
    step = check.step(moment, HEATING, True, blocked="room_cold")
    assert (step.closed, step.outcome) == (False, "not_judgeable:room_cold")


def test_a_moment_that_stops_suiting_in_the_baseline_is_no_try() -> None:
    check = Check()
    check.step(T0, HEATING, False)
    check.step(T0 + MIN, Sample(0.1, "idle"), False)
    assert check.phase == PHASE_WAIT and check.tries == []
    # The pump not heating and no supply read: nothing starts.
    assert check.step(T0 + 2 * MIN, Sample(0.1, "idle"), False).closed is False and check.phase == PHASE_WAIT


def test_tries_are_spaced_and_capped_a_day() -> None:
    check = Check(tries=[T0 - 3 * H, T0 - 2 * H, T0 - H])
    assert check.may_try(T0) is False
    assert Check(tries=[T0 - 30 * MIN]).may_try(T0) is False
    assert Check(tries=[T0 - H]).may_try(T0) is True


def test_the_check_is_kept_without_its_running_try() -> None:
    check = Check()
    to_closed(check)
    kept = Check.from_dict(check.as_dict())
    assert kept.phase == PHASE_WAIT and kept.tries == check.tries
    assert Check.from_dict(None).tries == [] and Check.from_dict({"tries": ["x"], "power_passes": True}).power_passes == 0


def test_where_the_supply_reads_the_power_is_not_judged() -> None:
    check = Check()
    moment = to_closed(check, Sample(1.1, "heating", 40.3))
    # The compressor pauses by itself, but the calculated supply does not move: no effect.
    _moment, steps = run(check, moment, int(EFFECT_GRACE / MIN) + 1, Sample(0.5, "heating", 40.2), True)
    assert check.phase != PHASE_OPENED and outcome(steps) == "failed:no_effect"


def test_at_most_six_switchings_a_day_and_an_opening_never_refused() -> None:
    # Five writes today: a lowering (two more) would make seven.
    assert decide(situation(T0, plan_until=T0 + 2 * H, writes_today=5)).reason == "write_cap"
    assert decide(situation(T0, plan_until=T0 + 2 * H, writes_today=4)).closed is True
    assert decide(situation(T0 + H, plan_until=None, held_since=T0, writes_today=9)).closed is False
