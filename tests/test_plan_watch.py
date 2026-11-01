"""The behind-plan rule (`planning/plan_watch.py`), fed one check at a time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.planning.plan_watch import (
    DECISION_NOT_CHARGING,
    DECISION_STALE,
    END_WAIT_S,
    BEHIND_KWH,
    DECISION_BEHIND,
    DECISION_CAR_ENDED,
    DECISION_END_OK,
    DECISION_END_REPLAN,
    DECISION_HELD,
    DECISION_NO_COUNT,
    DECISION_ON_PLAN,
    DECISION_RECENT,
    DECISION_REPLAN,
    DECISION_SETTLING,
    next_check_at,
    PlanWatch,
    WindowFacts,
)

START = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
WINDOW = (START, END)
KW = 11.0
MINUTE = timedelta(minutes=1)


def _facts(kwh: float | None, at: datetime | None = None, **changes) -> WindowFacts:
    return WindowFacts(
        window=changes.pop("window", WINDOW), power_kw=changes.pop("power_kw", KW), reading_kwh=kwh, reading_at=at,
        **changes,
    )


def _run(watch: PlanWatch, kw: float, minutes: range, *, held: str | None = None) -> list[tuple[int, str | None]]:
    """A car drawing `kw`, checked once a minute; the register reports at every check."""
    out = []
    for minute in minutes:
        now = START + minute * MINUTE
        kwh = 100.0 + kw * minute / 60
        result = watch.check(now, _facts(kwh, now, held=held))
        out.append((minute, result))
    return out


def test_a_car_on_plan_is_never_planned_again() -> None:
    watch = PlanWatch()
    results = _run(watch, KW, range(0, 120))
    assert all(result is None for _, result in results)
    assert watch.last_check is not None and watch.last_check.decision == DECISION_ON_PLAN


def test_a_shortfall_is_planned_again_once_confirmed_a_minute_later() -> None:
    watch = PlanWatch()
    results = dict(_run(watch, KW / 2, range(0, 30)))
    # 5.5 kWh an hour behind: 0.5 kWh after about 5.5 minutes, confirmed one check later.
    first = min(minute for minute, result in results.items() if result == DECISION_REPLAN)
    assert first == 7
    check = watch.last_check
    assert check is not None


def test_replans_come_at_most_every_five_minutes() -> None:
    watch = PlanWatch()
    replans = [minute for minute, result in _run(watch, 0.0, range(0, 60)) if result == DECISION_REPLAN]
    assert len(replans) >= 2
    assert all(later - earlier >= 5 for earlier, later in zip(replans, replans[1:]))


def test_the_first_minutes_of_a_window_are_not_judged() -> None:
    watch = PlanWatch()
    watch.check(START, _facts(100.0, START))
    watch.check(START + MINUTE, _facts(100.0, START + MINUTE))
    assert watch.last_check.decision == DECISION_SETTLING
    assert watch.check(START + 3 * MINUTE, _facts(100.0)) is None
    assert watch.last_check.decision == DECISION_BEHIND


def test_a_shortfall_spotnav_made_is_not_planned_again_mid_window_but_is_at_its_end() -> None:
    watch = PlanWatch()
    results = _run(watch, KW / 2, range(0, 120), held="balancing_limit")
    assert all(result is None for _, result in results)
    assert watch.last_check.decision == DECISION_HELD
    after = END + timedelta(seconds=2)
    assert watch.check(after, _facts(100.0 + KW / 2 * 2, after, window=None)) == DECISION_END_REPLAN
    assert watch.last_check.decision == DECISION_END_REPLAN


def test_a_hold_seen_once_keeps_the_window_for_its_end() -> None:
    watch = PlanWatch()
    _run(watch, KW / 2, range(0, 3), held="balancing")
    results = _run(watch, KW / 2, range(3, 60))
    assert all(result is None for _, result in results)


def test_a_window_that_delivered_most_of_its_share_ends_quietly() -> None:
    watch = PlanWatch()
    _run(watch, KW * 0.85, range(0, 120))
    assert watch.check(END + timedelta(seconds=2), _facts(100.0 + KW * 0.85 * 2, window=None)) is None
    assert watch.last_check.decision == DECISION_END_OK


def test_a_window_ending_before_need_met_is_judged_up_to_the_need() -> None:
    watch = PlanWatch()
    met = START + timedelta(minutes=30)
    for minute in range(0, 120):
        now = START + minute * MINUTE
        kwh = 100.0 + KW * min(minute, 30) / 60
        assert watch.check(now, _facts(kwh, now, need_met_at=met)) is None
    assert watch.check(END + timedelta(seconds=2), _facts(100.0 + KW / 2, window=None)) is None
    assert watch.last_check.decision == DECISION_END_OK


def test_without_a_count_nothing_is_judged() -> None:
    watch = PlanWatch()
    for minute in range(0, 60):
        assert watch.check(START + minute * MINUTE, _facts(None)) is None
    assert watch.last_check.decision == DECISION_NO_COUNT
    assert watch.check(END + timedelta(seconds=2), _facts(None, window=None)) is None


def test_the_car_ending_the_charge_is_left_to_its_own_path() -> None:
    watch = PlanWatch()
    _run(watch, KW, range(0, 10))
    for minute in range(10, 120):
        now = START + minute * MINUTE
        assert watch.check(now, _facts(100.0 + KW * 10 / 60, now, car_ended=True)) is None
    assert watch.last_check.decision == DECISION_CAR_ENDED
    assert watch.check(END + timedelta(seconds=2), _facts(100.0, window=None)) is None


def test_a_register_that_reports_every_five_minutes_is_not_a_car_behind() -> None:
    watch = PlanWatch()
    reported_at, reported = START, 100.0
    for minute in range(0, 120):
        now = START + minute * MINUTE
        if minute % 5 == 0:
            reported_at, reported = now, 100.0 + KW * minute / 60
        assert watch.check(now, _facts(reported, reported_at)) is None, minute


def _register_run(
    watch: PlanWatch, *, kw: float, period_min: int, phase_min: int, window: tuple[datetime, datetime],
    minutes: int, writes_same: bool = False,
) -> list[str | None]:
    """A car drawing `kw` inside `window` (nothing outside it), and a register posting every `period_min` minutes from
    `phase_min`; checked once a minute (plus once two seconds after the window's end) for `minutes` minutes."""
    start, end = window
    results = []
    posted_at, posted = start - timedelta(minutes=30), 100.0
    instants = sorted({start + minute * MINUTE for minute in range(minutes)} | {end + timedelta(seconds=2)})
    for now in instants:
        if (now - start) // MINUTE % period_min == phase_min % period_min and now.second == 0:
            charged = max(0.0, (min(now, end) - start).total_seconds() / 3600) * kw
            if 100.0 + charged != posted or writes_same:
                posted_at, posted = now, 100.0 + charged
        results.append(watch.check(now, _facts(posted, posted_at, window=window if start <= now < end else None)))
    return results


@pytest.mark.parametrize("phase", range(5))
def test_a_car_on_plan_with_a_five_minute_register_is_never_planned_again(phase: int) -> None:
    """One-slot windows, a register that posts every five minutes, at each phase of its posts."""
    watch = PlanWatch()
    window = (START, START + timedelta(minutes=15))
    results = _register_run(watch, kw=KW, period_min=5, phase_min=phase, window=window, minutes=40)
    assert DECISION_END_REPLAN not in results and DECISION_REPLAN not in results
    assert watch.last_check.decision == DECISION_END_OK


@pytest.mark.parametrize("phase", range(5))
def test_a_car_at_half_power_with_a_five_minute_register_is_planned_again_at_the_window_end(phase: int) -> None:
    watch = PlanWatch()
    window = (START, START + timedelta(minutes=15))
    results = _register_run(watch, kw=KW / 2, period_min=5, phase_min=phase, window=window, minutes=40)
    assert DECISION_END_REPLAN in results


def test_a_fifteen_minute_register_at_full_power_is_never_planned_again() -> None:
    watch = PlanWatch()
    for phase in range(0, 15, 3):
        results = _register_run(watch, kw=KW, period_min=15, phase_min=phase, window=WINDOW, minutes=150)
        assert DECISION_REPLAN not in results and DECISION_END_REPLAN not in results, phase


def test_a_fifteen_minute_register_still_finds_a_car_at_half_power() -> None:
    watch = PlanWatch()
    results = _register_run(watch, kw=KW / 2, period_min=15, phase_min=7, window=WINDOW, minutes=150)
    assert DECISION_REPLAN in results


def test_a_register_that_stopped_posting_says_nothing() -> None:
    watch = PlanWatch()
    stuck_at = START + 5 * MINUTE
    results = []
    for minute in range(0, 40):
        now = START + minute * MINUTE
        results.append(watch.check(now, _facts(100.0 + KW * min(minute, 5) / 60, min(now, stuck_at))))
    assert DECISION_REPLAN not in results
    assert watch.last_check.decision == DECISION_STALE


def test_a_register_writing_the_same_value_finds_a_car_that_draws_nothing() -> None:
    watch = PlanWatch()
    results = _register_run(watch, kw=0.0, period_min=5, phase_min=0, window=WINDOW, minutes=30, writes_same=True)
    assert DECISION_REPLAN in results


def test_the_window_end_waits_for_the_register_to_post() -> None:
    watch = PlanWatch()
    _run(watch, KW / 2, range(0, 120), held="balancing")
    after = END + timedelta(seconds=2)
    lagging = START + 118 * MINUTE
    assert watch.check(after, _facts(100.0 + KW / 2 * 118 / 60, lagging, window=None)) is None
    assert watch.watching, "judged once the register posts"
    posted = END + 3 * MINUTE
    assert watch.check(posted, _facts(100.0 + KW, posted, window=None)) == DECISION_END_REPLAN


def test_the_window_end_is_judged_at_the_latest_after_the_wait() -> None:
    watch = PlanWatch()
    _run(watch, KW, range(0, 120))
    last = START + 119 * MINUTE
    for minute in range(0, 21):
        now = END + timedelta(seconds=2) + minute * MINUTE
        result = watch.check(now, _facts(100.0 + KW * 119 / 60, last, window=None))
        if (now - END).total_seconds() < END_WAIT_S:
            assert result is None and watch.watching
    assert not watch.watching
    assert watch.last_check.decision == DECISION_END_OK


def test_a_charger_that_is_not_charging_is_not_judged() -> None:
    watch = PlanWatch()
    for minute in range(0, 60):
        now = START + minute * MINUTE
        assert watch.check(now, _facts(100.0, now, charging=False)) is None
    assert watch.last_check.decision == DECISION_NOT_CHARGING


def test_replans_that_change_nothing_back_off() -> None:
    watch = PlanWatch()
    replans = []
    for minute in range(0, 120):
        now = START + minute * MINUTE
        if watch.check(now, _facts(100.0, now)) == DECISION_REPLAN:
            replans.append(minute)
            watch.note_replan_result(changed=False)
    gaps = [later - earlier for earlier, later in zip(replans, replans[1:])]
    assert gaps == sorted(gaps) and gaps[-1] >= 4 * gaps[0]
    assert len(replans) <= 6


def test_the_log_tells_once_per_window() -> None:
    watch = PlanWatch()
    assert watch.tell(WINDOW)
    assert not watch.tell(WINDOW)
    assert watch.tell((END, END + timedelta(hours=1)))


def test_a_replan_judges_the_window_against_the_new_plan_from_then_on() -> None:
    watch = PlanWatch()
    results = dict(_run(watch, KW / 2, range(0, 9)))
    assert results[7] == DECISION_REPLAN
    assert watch.last_check.decision == DECISION_ON_PLAN
    assert watch.last_check.expected_kwh < BEHIND_KWH


def test_a_shortfall_confirmed_soon_after_a_replan_waits_for_the_five_minutes() -> None:
    watch = PlanWatch()
    decisions = []
    for minute in range(0, 12):
        now = START + minute * MINUTE
        watch.check(now, _facts(100.0, now))
        decisions.append(watch.last_check.decision)
    assert decisions[4] == DECISION_REPLAN
    assert decisions[8] == DECISION_RECENT
    assert decisions[9] == DECISION_REPLAN


def test_a_dst_night_counts_real_hours() -> None:
    """2026-10-25: Stockholm's clocks go from 03:00 back to 02:00, so 01:30 to 03:30 local is three hours."""
    zone = ZoneInfo("Europe/Stockholm")
    start = datetime(2026, 10, 25, 1, 30, tzinfo=zone)
    end = datetime(2026, 10, 25, 3, 30, tzinfo=zone)
    assert end - start == timedelta(hours=2)  # wall clock; the instants are three hours apart
    window = (start.astimezone(timezone.utc), end.astimezone(timezone.utc))
    assert window[1] - window[0] == timedelta(hours=3)
    watch = PlanWatch()
    begin = window[0]
    for minute in range(0, 180):
        now = begin + minute * MINUTE
        assert watch.check(now, WindowFacts(window=window, power_kw=KW, reading_kwh=KW * minute / 60, reading_at=now)) is None
    after = window[1] + timedelta(seconds=2)
    assert watch.check(after, WindowFacts(window=None, power_kw=KW, reading_kwh=KW * 2.9)) is None
    assert watch.last_check.decision == DECISION_END_OK
    assert abs(watch.last_check.expected_kwh - KW * 3) < 1e-6


def test_next_check_follows_the_windows() -> None:
    watch = PlanWatch()
    before = START - timedelta(hours=1)
    assert next_check_at(before, watch, [WINDOW]) == START + timedelta(seconds=2)
    assert next_check_at(START + MINUTE, watch, [WINDOW]) == START + 2 * MINUTE
    assert next_check_at(END + MINUTE, watch, [WINDOW]) is None
    watch.check(END - timedelta(seconds=30), _facts(100.0))
    assert next_check_at(END - timedelta(seconds=30), watch, []) == END + timedelta(seconds=2)
