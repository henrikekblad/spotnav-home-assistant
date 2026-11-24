"""A plant that goes silent for a while (one Sigenergy plant does not answer Modbus for about 90 s each night): its
entities read `unavailable`, or keep their last value while its readings age. The executor rides a short silence
out without a write and hands the battery back only after `SILENCE_GRACE_S` or when the fuse needs it; a write that
cannot be read back meanwhile is handed back as stale data, never blocked. The car's hold rides it out alike."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL
from custom_components.spotnav.battery.executor import (
    PEAK_COVER_A,
    RESTORE_HEADROOM_A,
    SILENCE_GRACE_S,
    ExecInputs,
    Executor,
)
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.gate import FAILING_ABORTS
from custom_components.spotnav.battery.hold import HoldInputs, HoldMemory, step as hold_step
from custom_components.spotnav.battery.ledger import READBACK_DEADLINE_S
from custom_components.spotnav.battery.plan_shadow import PlanActuals
from tests.battery_control_fakes import (
    LIMIT_HOLD_CONTROL,
    SIGENERGY_STATES,
    T0,
    TICK,
    FakeBattery,
    events,
    inputs,
    run,
)

SILENT_MANAGEMENT = Management("unknown", "unreadable", {"EMS work mode": "unavailable"})


def _sigenergy() -> tuple[Executor, FakeBattery]:
    return Executor(LIMIT_HOLD_CONTROL), FakeBattery(LIMIT_HOLD_CONTROL, dict(SIGENERGY_STATES))


def _running(action: str, rate_kw: float = 0.0) -> tuple[Executor, FakeBattery, datetime]:
    executor, battery = _sigenergy()
    now, _results = run(executor, battery, T0, 8, action, rate_kw)
    assert executor.state.phase == "active" and executor.state.pending is None
    return executor, battery, now


def _silent(battery: FakeBattery, now: datetime, action: str, rate_kw: float = 0.0, **overrides) -> ExecInputs:
    """A look while the plant answers nothing: every entity `unavailable`, no battery reading."""
    look = inputs(battery, now, action, rate_kw, management=SILENT_MANAGEMENT, **overrides)
    reads = {name: replace(read, state="unavailable") for name, read in look.reads.items()}
    return replace(look, facts=replace(look.facts, power_kw=None, soc_pct=None), battery_age_s=None, reads=reads)


def _quiet(executor: Executor, battery: FakeBattery, now: datetime, seconds: float, action: str, rate_kw: float = 0.0,
           **overrides):
    """Looks `TICK` apart for `seconds` of silence; no write reaches the plant (Home Assistant skips an entity
    service on an unavailable entity)."""
    results = []
    end = now + timedelta(seconds=seconds)
    while now < end:
        results.append(executor.step(_silent(battery, now, action, rate_kw, **overrides)))
        now += TICK
    return now, results


@pytest.mark.parametrize(("action", "rate_kw"), [("hold", 0.0), ("grid_charge", 3.0)])
def test_a_90_s_silence_is_ridden_out_without_a_write_or_a_hand_back(action: str, rate_kw: float) -> None:
    executor, battery, now = _running(action, rate_kw)
    writes = len(battery.writes)
    now, results = _quiet(executor, battery, now, 90.0, action, rate_kw)
    assert [op for result in results for op in result.ops] == []
    assert not events(results, "hand_back")
    assert executor.state.phase == "active" and executor.state.action == action
    # The plant answers again: nothing to write, the action goes on.
    _now, results = run(executor, battery, now, 6, action, rate_kw)
    assert len(battery.writes) == writes and not events(results, "hand_back")
    assert executor.state.phase == "active" and executor.state.silent_since is None


def test_a_silence_past_the_grace_is_handed_back_as_stale_data_without_a_block() -> None:
    executor, battery, now = _running("grid_charge", 3.0)
    now, results = _quiet(executor, battery, now, SILENCE_GRACE_S + 10.0, "grid_charge", 3.0)
    assert [event["reason"] for event in events(results, "hand_back")] == ["stale_data"]
    assert executor.state.blocked is None
    # Back: the hand-back's restore runs, the control is idle and may start again.
    _now, _results = run(executor, battery, now, 12, "self_consume")
    assert executor.state.phase == "idle" and battery.states["remote_ems"] == "off"


def test_the_fuse_still_hands_back_at_once_during_a_silence() -> None:
    # A hold whose headroom falls under the peak cover's mark: handed back, never lifted by a write on stale data.
    executor, battery, now = _running("hold")
    _now, results = _quiet(executor, battery, now, 10.0, "hold", headroom_a=PEAK_COVER_A - 0.1)
    assert events(results, "hand_back")[0]["reason"] == "stale_data"
    assert not events(results, "peak_cover")
    # A grid charge whose rate no longer fits counting none of its own charge back.
    executor, battery, now = _running("grid_charge", 6.0)
    _now, results = _quiet(executor, battery, now, 10.0, "grid_charge", 6.0, headroom_a=8.0)
    assert events(results, "hand_back")[0]["reason"] == "stale_data"
    # The site's measurement lost (a plant that is also the site's meter): at once, as before.
    executor, battery, now = _running("grid_charge", 3.0)
    _now, results = _quiet(executor, battery, now, 10.0, "grid_charge", 3.0, measurement_ok=False)
    assert events(results, "hand_back")[0]["reason"] == "stale_data"


def test_a_fuse_hand_back_during_a_silence_writes_every_role_at_once() -> None:
    # Under `RESTORE_HEADROOM_A` the fuse's reason wins over the stale reading: all roles in the first look.
    for action, rate_kw in (("hold", 0.0), ("grid_charge", 3.0)):
        executor, battery, now = _running(action, rate_kw)
        result = executor.step(_silent(battery, now, action, rate_kw, headroom_a=RESTORE_HEADROOM_A - 0.1))
        assert [event["reason"] for event in result.events if event["kind"] == "hand_back"] == ["fuse_headroom"]
        assert len(result.ops) > 1 and all(op.restore for op in result.ops)
    # In the middle of a ridden-out silence too.
    executor, battery, now = _running("hold")
    now, results = _quiet(executor, battery, now, 40.0, "hold")
    assert not events(results, "hand_back")
    result = executor.step(_silent(battery, now, "hold", headroom_a=RESTORE_HEADROOM_A - 0.1))
    assert [event["reason"] for event in result.events if event["kind"] == "hand_back"] == ["fuse_headroom"]
    assert len(result.ops) > 1


def test_a_sale_is_ridden_out_only_at_the_rate_its_export_cap_allows_now() -> None:
    executor, battery, now = _running("export_discharge", 3.0)
    now, results = _quiet(executor, battery, now, 60.0, "export_discharge", 3.0)
    assert not events(results, "hand_back") and [op for result in results for op in result.ops] == []
    # The sun's export grows: the plan's rate under the export cap falls, and nothing may be written on stale data.
    _now, results = _quiet(executor, battery, now, 10.0, "export_discharge", 1.5)
    assert events(results, "hand_back")[0]["reason"] == "stale_data"
    assert not events(results, "rate")


def test_a_new_action_during_a_silence_hands_back_instead_of_writing() -> None:
    executor, battery, now = _running("grid_charge", 3.0)
    _now, results = _quiet(executor, battery, now, 10.0, "hold")
    assert events(results, "hand_back")[0]["reason"] == "stale_data"
    assert not events(results, "change")


def _enter_first_write(action: str = "hold") -> tuple[Executor, FakeBattery, datetime]:
    """The first write of an enter is made; the plant goes silent before its read-back."""
    executor, battery = _sigenergy()
    result = executor.step(inputs(battery, T0, action))
    assert [op.role for op in result.ops] == ["discharge_limit"]
    return executor, battery, T0 + TICK


def test_a_write_awaiting_its_read_back_waits_through_a_short_silence() -> None:
    executor, battery, now = _enter_first_write()
    now, results = _quiet(executor, battery, now, 40.0, "hold")
    assert [op for result in results for op in result.ops] == []
    # The plant answers within the read-back's own window; the write landed after all.
    battery.states["discharge_limit"] = "0"
    _now, results = run(executor, battery, now, 6, "hold")
    assert events(results, "confirmed")[0]["reason"] == "discharge_limit"
    assert not events(results, "hand_back") and executor.state.phase == "active"


def test_a_write_not_read_back_before_the_plant_answers_is_stale_data_not_a_block() -> None:
    executor, battery, now = _enter_first_write()
    now, results = _quiet(executor, battery, now, READBACK_DEADLINE_S + 30.0, "hold")
    not_taken = events(results, "not_taken")
    assert not_taken and not_taken[0]["silent"] is True
    assert [event["reason"] for event in events(results, "hand_back")] == ["stale_data"]
    assert executor.state.blocked is None
    # Not a finding against the verification either: the routine tries again later.
    assert not "stale_data".startswith(FAILING_ABORTS)


def test_a_write_whose_readings_stopped_ageing_while_unanswered_is_stale_data_not_a_block() -> None:
    # The integration keeps its entities with their last values: the write does not land and the battery's reading
    # has not been reported since it was made.
    executor, battery, now = _enter_first_write()
    results = []
    for index in range(8):
        age = 5.0 + (index + 1) * TICK.total_seconds()
        results.append(executor.step(inputs(battery, now, "hold", battery_age_s=age)))
        now += TICK
    assert [event["reason"] for event in events(results, "hand_back")] == ["stale_data"]
    assert executor.state.blocked is None


def test_a_write_that_raises_while_the_plant_is_silent_is_stale_data_not_a_block() -> None:
    executor, battery, now = _enter_first_write()
    result = executor.step(replace(_silent(battery, now, "hold"), failed=("discharge_limit",)))
    assert [event["reason"] for event in result.events if event["kind"] == "hand_back"] == ["stale_data"]
    assert executor.state.blocked is None


def test_a_dropped_write_on_a_plant_that_still_reports_still_blocks() -> None:
    executor, battery = _sigenergy()
    battery.drops.add("discharge_limit")
    _now, _results = run(executor, battery, T0, 10, "hold")
    assert (executor.state.blocked or "").startswith("write_not_taken:discharge_limit")


# --- the car's hold (shadow, and the request control merges) -----------------------------------------------------


def _hold_inputs(now: datetime, battery: FakeBattery, **overrides) -> HoldInputs:
    base = HoldInputs(
        now=now, grid_charging=True, facts=battery.facts(0.0), battery_age_s=5.0, measurement_ok=True,
        headroom_a=20.0, reads=battery.reads(), charger_ids=("c",), car_kw=7.0,
    )
    return replace(base, **overrides)


@pytest.mark.parametrize(("seconds", "released"), [(90.0, False), (SILENCE_GRACE_S + 10.0, True)])
def test_the_car_s_hold_rides_out_a_short_silence(seconds: float, released: bool) -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    memory = HoldMemory(held=True, held_since=T0, last_change=T0, entities={"discharge_limit": "number.x"})
    now = T0 + TICK
    decisions = []
    end = now + timedelta(seconds=seconds)
    while now < end:
        silent = _hold_inputs(now, battery, battery_age_s=None)
        silent = replace(silent, facts=replace(silent.facts, power_kw=None, management=SILENT_MANAGEMENT))
        memory, decision = hold_step(memory, silent)
        decisions.append(decision)
        now += TICK
    reasons = [decision.reason for decision in decisions if decision is not None]
    assert memory.held is not released
    assert reasons == (["battery_reading_stale"] if released else [])
    if not released:
        memory, decision = hold_step(memory, _hold_inputs(now, battery))
        assert memory.held and decision is None and memory.stale_since is None


def test_the_car_s_hold_is_released_at_once_when_the_fuse_needs_it_during_a_silence() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    memory = HoldMemory(held=True, held_since=T0, last_change=T0, entities={"discharge_limit": "number.x"})
    silent = _hold_inputs(T0 + TICK, battery, battery_age_s=None, headroom_a=PEAK_COVER_A - 0.1)
    memory, decision = hold_step(memory, replace(silent, facts=replace(silent.facts, power_kw=None)))
    assert not memory.held and decision is not None and decision.reason == "battery_reading_stale"


# --- the night's review --------------------------------------------------------------------------------------------


def test_a_90_s_gap_leaves_the_quarter_hour_measured_for_the_review() -> None:
    actuals = PlanActuals()
    start = datetime.fromisoformat("2026-11-20T04:00:00+01:00")
    actuals.open(start, price=None, cap_kw=None, ran=None)
    now = start
    while now <= start + timedelta(minutes=15):
        silent = start + timedelta(seconds=60) <= now < start + timedelta(seconds=150)
        actuals.observe(now, 1.0, 0.0, None if silent else 2.0, None if silent else 50.0)
        now += TICK
    bucket = actuals.buckets[start]
    assert bucket.covered
    assert bucket.covered_s < 15 * 60
