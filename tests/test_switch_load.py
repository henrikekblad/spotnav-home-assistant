"""The switched load's policy: settings, the rolling runtime's deadlines, the cheapest hours under the import cap, the
comfort floor and the fuse."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.loads.switch_load import (
    CONF_SWITCH_LOAD,
    FLOOR_HYSTERESIS_C,
    QUARTER,
    REASON_CHEAP,
    REASON_DEADLINE,
    REASON_FLOOR,
    REASON_FUSE,
    REASON_MET,
    REASON_NO_PRICES,
    WINDOW,
    DayTally,
    RunHistory,
    SwitchInputs,
    SwitchLoadConfig,
    deadlines,
    decide,
    no_slack,
    periods,
    plan_switch,
    priced_slots,
    quarter_start,
)
from custom_components.spotnav.planning.planner import FiscalChoice, PlanningSlot

UTC = timezone.utc
NOW = datetime(2026, 11, 14, 13, 0, tzinfo=UTC)
HEATER = SwitchLoadConfig("switch.water_heater", rated_kw=3.0, runtime_h=3.0)


def _hour(start: datetime) -> datetime:
    return start.replace(minute=0, second=0, microsecond=0)


def _slots(prices_by_hour: list[float], start: datetime = NOW) -> list[PlanningSlot]:
    """Four quarter hours per hourly price (major unit per kWh), from `start` on."""
    return [
        PlanningSlot(start + timedelta(hours=hour) + QUARTER * quarter, price, "test")
        for hour, price in enumerate(prices_by_hour)
        for quarter in range(4)
    ]


def _met(now: datetime = NOW, hours_ago: float = 0.0) -> RunHistory:
    """A history that began `hours_ago` before now (its need counts as met just before it began)."""
    return RunHistory(tracked_since=now - timedelta(hours=hours_ago))


def _plan(prices: list[float], *, history: RunHistory | None = None, room: dict | None = None, config=HEATER, now=NOW):
    return plan_switch(
        now=now,
        config=config,
        history=history if history is not None else _met(now),
        slots=_slots(prices, quarter_start(now)),
        fiscal=FiscalChoice(),
        room_kw_by_hour=room or {},
        hour_of=_hour,
    )


# ---- settings


def test_the_settings_round_trip_and_broken_ones_are_no_load() -> None:
    config = SwitchLoadConfig("switch.vvb", 2.0, 4.0, "sensor.tank", 45.0)

    assert SwitchLoadConfig.from_data(config.as_dict()) == config
    assert config.needed_quarters == 16 and config.daily_kwh == 8.0
    assert CONF_SWITCH_LOAD == "switch_load"
    for broken in (None, {}, {"entity_id": "light.x", "rated_kw": 2, "runtime_h": 2},
                   {"entity_id": "switch.x", "rated_kw": 0, "runtime_h": 2},
                   {"entity_id": "switch.x", "rated_kw": 2, "runtime_h": 25},
                   {"entity_id": "switch.x", "rated_kw": True, "runtime_h": 2}):
        assert SwitchLoadConfig.from_data(broken) is None


def test_a_floor_needs_both_a_sensor_and_a_temperature() -> None:
    half = SwitchLoadConfig.from_data({"entity_id": "switch.x", "rated_kw": 2, "runtime_h": 2, "floor_c": 45})

    assert half is not None and half.floor_c is None and half.temperature_entity is None


def test_the_load_is_single_phase_up_to_16_a_and_three_phase_above() -> None:
    assert HEATER.phases == 1 and HEATER.phase_amps(400.0) == pytest.approx(13.0, abs=0.05)
    big = SwitchLoadConfig("switch.x", 9.0, 2.0)
    assert big.phases == 3 and big.phase_amps(400.0) == pytest.approx(13.0, abs=0.05)


# ---- the rolling runtime


def test_a_new_history_is_due_within_a_day_not_at_once() -> None:
    steps = deadlines(_met(), NOW, 3.0)

    assert steps[0] == (NOW + WINDOW - timedelta(hours=3) + QUARTER, 1)
    assert steps[-1] == (NOW + WINDOW, 12)
    assert not no_slack(steps, NOW)


def test_yesterdays_run_sets_todays_deadlines() -> None:
    history = RunHistory(tracked_since=NOW - timedelta(days=3))
    history.note(NOW - timedelta(hours=11), NOW - timedelta(hours=8))  # 02:00-05:00

    steps = deadlines(history, NOW, 3.0)

    assert steps[0] == (NOW + timedelta(hours=13, minutes=15), 1)  # 02:15 tomorrow
    assert steps[-1] == (NOW + timedelta(hours=16), 12)  # 05:00 tomorrow


def test_a_need_already_run_has_no_deadline() -> None:
    history = RunHistory(tracked_since=NOW - timedelta(days=3))
    history.note(NOW - timedelta(hours=1), NOW)

    assert deadlines(history, NOW, 1.0)[0] == (NOW + timedelta(hours=23, minutes=15), 1)


def test_the_history_keeps_one_window_and_joins_touching_runs() -> None:
    history = RunHistory()
    history.note(NOW, NOW + timedelta(seconds=10))
    history.note(NOW + timedelta(seconds=10), NOW + timedelta(seconds=20))
    history.note(NOW - timedelta(days=2), NOW - timedelta(days=2) + QUARTER)

    assert history.tracked_since == NOW
    history.runs.sort()
    history.prune(NOW)
    assert history.runs == [(NOW, NOW + timedelta(seconds=20))]
    assert RunHistory.from_dict(history.as_dict()).as_dict() == history.as_dict()
    assert RunHistory.from_dict({"runs": [["x", "y"], [NOW.isoformat()]], "tracked_since": "2026-01-01T00:00"}).runs == []


# ---- the cheapest hours


def test_the_cheapest_hours_before_the_deadline_in_one_run() -> None:
    prices = [1.0] * 26
    prices[14:17] = [0.2, 0.1, 0.2]  # 03:00-06:00 tomorrow
    prices[25] = 0.05  # 14:00 tomorrow: after the last deadline (13:00)

    plan = _plan(prices)

    assert periods(plan.starts) == [(NOW + timedelta(hours=14), NOW + timedelta(hours=17))]
    assert plan.short == 0 and plan.deferred == 0
    assert plan.cost_minor(3.0) == pytest.approx((20 + 10 + 20) * 3.0)


def test_an_earlier_deadline_is_kept_when_the_cheap_hours_come_after_it() -> None:
    history = RunHistory(tracked_since=NOW - timedelta(days=3))
    history.note(NOW - timedelta(hours=11), NOW - timedelta(hours=8))  # 02:00-05:00 today: due again from 02:15
    prices = [1.0] * 24
    prices[15:18] = [0.1, 0.1, 0.1]  # 04:00-07:00 tomorrow: the last of it too late

    plan = _plan(prices, history=history)

    for moment, need in plan.steps:
        assert sum(1 for start in plan.starts if start + QUARTER <= moment) >= need
    assert len(plan.starts) == 12 and plan.short == 0


def test_an_hour_without_room_under_the_import_cap_is_never_chosen() -> None:
    prices = [1.0] * 24
    prices[14:17] = [0.1, 0.1, 0.1]
    room = {NOW + timedelta(hours=15): 2.0, NOW + timedelta(hours=14): 3.5}

    plan = _plan(prices, room=room)

    assert plan.blocked_hours == (NOW + timedelta(hours=15),)
    assert all(_hour(start) != NOW + timedelta(hours=15) for start in plan.starts)
    assert len(plan.starts) == 12


def test_a_deadline_after_the_prices_waits_for_them_and_says_so() -> None:
    plan = _plan([1.0] * 11)  # priced until 00:00

    assert plan.starts == frozenset() and plan.deferred == 12 and plan.priced_until == NOW + timedelta(hours=11)


def test_too_few_free_quarters_before_a_deadline_are_short() -> None:
    room = {NOW + timedelta(hours=hour): 0.0 for hour in range(24)}

    plan = _plan([1.0] * 24, room=room)

    assert plan.starts == frozenset() and plan.short == 12


def test_no_prices_means_no_plan_but_the_deadline_still_runs_it() -> None:
    history = RunHistory(tracked_since=NOW - timedelta(days=3))
    history.note(NOW - WINDOW + timedelta(hours=1), NOW - WINDOW + timedelta(hours=4))
    plan = plan_switch(
        now=NOW, config=HEATER, history=history, slots=[], fiscal=FiscalChoice(), room_kw_by_hour={}, hour_of=_hour
    )

    assert plan.priced_until is None and plan.deferred == 12
    # 1 h 15 min to the first deadline's need of 1 quarter: slack; at 3 h before the last, none.
    assert decide(SwitchInputs(NOW, HEATER, plan, history)).reason == REASON_NO_PRICES
    late = NOW + timedelta(hours=1, minutes=15)
    assert decide(SwitchInputs(late, HEATER, plan, history)).reason == REASON_DEADLINE


def test_priced_slots_stop_at_a_gap_and_need_the_running_quarter() -> None:
    slots = _slots([1.0, 1.0, 1.0])
    gap = slots[:4] + slots[8:]

    assert len(priced_slots(gap, NOW)) == 4
    assert priced_slots(slots[4:], NOW) == []
    assert len(priced_slots(slots, NOW + timedelta(minutes=20))) == 11


# ---- now


def test_on_in_a_chosen_quarter_and_off_between() -> None:
    prices = [1.0] * 24
    prices[0] = 0.0
    plan = _plan(prices, config=SwitchLoadConfig("switch.x", 3.0, 1.0))

    config = SwitchLoadConfig("switch.x", 3.0, 1.0)
    assert plan.starts == {NOW + QUARTER * index for index in range(4)}
    assert decide(SwitchInputs(NOW, config, plan, _met())).reason == REASON_CHEAP
    off = decide(SwitchInputs(NOW + timedelta(hours=2), config, plan, _met()))
    assert off.on is False and off.reason == REASON_MET


def test_the_floor_runs_it_at_any_price_until_above_the_band() -> None:
    config = SwitchLoadConfig("switch.x", 3.0, 1.0, "sensor.tank", 45.0)
    plan = _plan([1.0] * 24, config=config)

    cold = decide(SwitchInputs(NOW, config, plan, _met(), temperature_c=44.0))
    assert cold.on and cold.reason == REASON_FLOOR and cold.floor_active
    warming = decide(SwitchInputs(NOW, config, plan, _met(), temperature_c=45.5, floor_active=True))
    assert warming.on and warming.reason == REASON_FLOOR
    warm = decide(SwitchInputs(NOW, config, plan, _met(), temperature_c=45 + FLOOR_HYSTERESIS_C, floor_active=True))
    assert not warm.on and not warm.floor_active
    unknown = decide(SwitchInputs(NOW, config, plan, _met(), temperature_c=None, floor_active=True))
    assert not unknown.on and not unknown.floor_active


def test_the_fuse_comes_before_the_floor_and_counts_the_switch_already_on() -> None:
    config = SwitchLoadConfig("switch.x", 3.0, 1.0, "sensor.tank", 45.0)

    tight = decide(SwitchInputs(NOW, config, None, _met(), temperature_c=30.0, headroom_a=8.0))
    assert not tight.on and tight.reason == REASON_FUSE and tight.detail["wanted"] == REASON_FLOOR
    running = decide(SwitchInputs(NOW, config, None, _met(), temperature_c=30.0, headroom_a=1.0, switch_on=True))
    assert running.on


def test_a_met_need_says_so() -> None:
    history = RunHistory(tracked_since=NOW - timedelta(days=2))
    history.note(NOW - timedelta(hours=1), NOW)

    config = SwitchLoadConfig("switch.x", 3.0, 0.5)
    steps = deadlines(history, NOW, 0.5)
    assert steps == ((NOW + timedelta(hours=23, minutes=45), 1), (NOW + timedelta(hours=24), 2))
    plan = _plan([1.0] * 11, history=history, config=config)
    assert plan.starts == frozenset() and plan.deferred == 2
    assert decide(SwitchInputs(NOW, config, plan, history)).reason == REASON_MET
    assert decide(SwitchInputs(NOW, config, None, history)).reason == REASON_NO_PRICES


def test_a_day_tally_round_trips() -> None:
    tally = DayTally()
    tally.add(1800, 100.0, 3.0, shadow=True, actual=False)
    tally.add(900, None, 3.0, shadow=False, actual=True)

    assert tally.as_dict() == {"shadow_h": 0.5, "shadow_cost_minor": 150.0, "actual_h": 0.25, "actual_cost_minor": 0.0}
    assert DayTally.from_dict(tally.as_dict()).as_dict() == tally.as_dict()


def test_an_overdue_need_runs_now() -> None:
    history = RunHistory(tracked_since=NOW - timedelta(days=2))

    steps = deadlines(history, NOW, 1.0)

    assert steps == ((NOW + QUARTER, 4),) and no_slack(steps, NOW)
    assert decide(SwitchInputs(NOW, SwitchLoadConfig("switch.x", 3.0, 1.0), None, history)).reason == REASON_DEADLINE
