"""The import target's pure rules (`site/import_target.py`): the hour's budget, sharing it, and the departure."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.site.demand_tracker import RunningHour
from custom_components.spotnav.site.import_target import (
    ACTION_FREE,
    ACTION_HELD,
    ACTION_PAUSED,
    ACTION_UNHELD,
    departure_at_risk,
    departure_shortfall_kwh,
    hour_budget,
    lowering_floor_a,
    MIN_REMAINING_S,
    NOT_APPLIED_BATTERY_GAP,
    NOT_APPLIED_GAP,
    NOT_APPLIED_HOUSE_UNKNOWN,
    NOT_APPLIED_NO_HOUR,
    NOT_APPLIED_NO_READING,
    plan_remaining_kwh,
    share_budget,
    TargetMember,
)

HOUR = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)
FUSE_W = 25 * 3 * 230.0


def _running(minutes: float, kwh: float, *, gap_s: float = 0.0, grid_w: float | None = 1000.0) -> RunningHour:
    """An hour `minutes` in, `kwh` imported over the time measured (`gap_s` of it missing)."""
    span = minutes * 60.0
    return RunningHour(
        start=HOUR,
        import_wh=kwh * 1000.0,
        covered_s=span - gap_s,
        last_at=HOUR + timedelta(seconds=span),
        last_grid_w=grid_w,
    )


def _budget(minutes: float, kwh: float, *, house_w: float = 1000.0, target_kw: float = 6.0, **kwargs):
    now = HOUR + timedelta(minutes=minutes)
    budget, reason = hour_budget(
        target_kw=target_kw, running=_running(minutes, kwh, **kwargs), now=now, house_w=house_w, ceiling_w=FUSE_W
    )
    assert reason is None and budget is not None
    return budget


# -- the budget


def test_at_the_start_of_the_hour_the_chargers_get_the_target_less_the_house() -> None:
    budget = _budget(0.5, 0.0)
    # 6 kWh over the 59.5 minutes left, less the house's kilowatt.
    assert budget.allowed_w == pytest.approx(6000.0 * 60 / 59.5 - 1000.0)
    assert not budget.house_over


def test_what_was_imported_so_far_is_taken_off_the_budget() -> None:
    # Half the hour gone, 4 kWh in: 2 kWh for the 30 minutes left, so 4 kW, less the house.
    budget = _budget(30, 4.0)
    assert budget.allowed_w == pytest.approx(3000.0)
    assert budget.hour_kw == pytest.approx(8.0)
    assert budget.so_far_kwh == pytest.approx(4.0)


def test_a_short_gap_counts_at_the_hours_own_mean_never_as_nothing() -> None:
    # 30 minutes in, 3 kWh over the 25 measured: the five missing count at 7.2 kW, so 3.6 kWh so far.
    budget = _budget(30, 3.0, gap_s=300.0)
    assert budget.so_far_kwh == pytest.approx(3.6)


def test_a_budget_spent_leaves_the_chargers_nothing_and_never_less() -> None:
    assert _budget(40, 7.0).allowed_w == 0.0


def test_the_last_minutes_are_bounded_by_the_least_time_left_and_the_fuse() -> None:
    # Ten seconds left with 3 kWh unused: spread over a minute at least, and never above the fuse's ceiling.
    budget = _budget(59 + 50 / 60, 3.0)
    assert budget.remaining_s == MIN_REMAINING_S
    assert budget.allowed_w == FUSE_W
    # Nothing unused: nothing, however little is left.
    assert _budget(59 + 50 / 60, 6.0).allowed_w == 0.0


def test_a_surplus_leaves_the_chargers_more() -> None:
    # The sun exports 2 kW beyond the house: the chargers may take it as well as the target.
    budget = _budget(0.5, 0.0, house_w=-2000.0)
    assert budget.allowed_w == pytest.approx(6000.0 * 60 / 59.5 + 2000.0)


def test_a_house_alone_over_the_target_is_said() -> None:
    # 30 minutes in at 6.5 kW from the house alone: the hour ends above 6 kW without any charger.
    budget = _budget(30, 3.25, house_w=6500.0)
    assert budget.house_over
    assert budget.allowed_w == 0.0
    assert not _budget(30, 2.0, house_w=3000.0).house_over


@pytest.mark.parametrize(
    ("running", "house_w", "reason"),
    [
        (None, 1000.0, NOT_APPLIED_NO_HOUR),
        (_running(30, 2.0, grid_w=None), 1000.0, NOT_APPLIED_NO_READING),
        (_running(30, 2.0, gap_s=601.0), 1000.0, NOT_APPLIED_GAP),
        (_running(30, 2.0), None, NOT_APPLIED_HOUSE_UNKNOWN),
    ],
)
def test_nothing_is_held_when_the_hour_cannot_be_known(running, house_w, reason) -> None:
    budget, why = hour_budget(
        target_kw=6.0, running=running, now=HOUR + timedelta(minutes=30), house_w=house_w, ceiling_w=FUSE_W
    )
    assert budget is None and why == reason


def test_the_first_reading_of_an_hour_at_its_very_start_counts_nothing_so_far() -> None:
    running = RunningHour(start=HOUR, last_at=HOUR, last_grid_w=2000.0)
    budget, why = hour_budget(target_kw=6.0, running=running, now=HOUR, house_w=1000.0, ceiling_w=FUSE_W)
    assert why is None and budget is not None
    assert budget.so_far_kwh == 0.0 and budget.allowed_w == pytest.approx(5000.0)


def test_an_hour_already_over_is_not_the_running_one() -> None:
    budget, why = hour_budget(
        target_kw=6.0, running=_running(30, 2.0), now=HOUR + timedelta(minutes=61), house_w=0.0, ceiling_w=FUSE_W
    )
    assert budget is None and why == NOT_APPLIED_NO_HOUR


def test_a_discharging_battery_counts_as_import_over_its_own_measured_time() -> None:
    now = HOUR + timedelta(minutes=30)
    budget, _ = hour_budget(
        target_kw=6.0,
        running=_running(30, 1.0),
        now=now,
        house_w=1000.0,
        ceiling_w=FUSE_W,
        extra_wh=1000.0,
        extra_covered_s=1800.0,
    )
    assert budget is not None and budget.so_far_kwh == pytest.approx(2.0)
    missing, why = hour_budget(
        target_kw=6.0,
        running=_running(30, 1.0),
        now=now,
        house_w=1000.0,
        ceiling_w=FUSE_W,
        extra_wh=0.0,
        extra_covered_s=1000.0,
    )
    assert missing is None and why == NOT_APPLIED_BATTERY_GAP


# -- sharing the budget


def _car(name: str = "car", *, wants: float = 16.0, phases: int = 3, **kwargs) -> TargetMember:
    return TargetMember(charger_entry_id=name, phases=phases, min_a=6.0, wants_a=wants, **kwargs)


def test_a_charger_that_fits_is_free_and_one_that_does_not_is_held_to_whole_amps() -> None:
    assert share_budget(12000.0, [_car()])["car"].action == ACTION_FREE
    share = share_budget(5000.0, [_car()])["car"]
    # 5000 W over three phases at 230 V is 7.2 A: held to 7 A.
    assert (share.action, share.cap_a, share.room_a) == (ACTION_HELD, 7.0, 7.0)


def test_below_the_minimum_the_hour_cannot_end_under_the_target_so_the_charger_is_paused() -> None:
    # 6 A on three phases is 4140 W: 4100 W cannot carry it to the end of the hour.
    assert share_budget(4140.0, [_car()])["car"].action == ACTION_HELD
    share = share_budget(4100.0, [_car()])["car"]
    assert (share.action, share.cap_a) == (ACTION_PAUSED, 0.0)


def test_a_charger_paused_this_hour_stays_paused_whatever_the_budget() -> None:
    share = share_budget(50000.0, [_car(paused_this_hour=True)])["car"]
    assert share.action == ACTION_PAUSED


def test_a_switch_only_charger_runs_only_while_its_whole_power_fits() -> None:
    heater = _car("heater", wants=10.0, phases=1, switch_only=True)
    assert share_budget(2300.0, [heater])["heater"].action == ACTION_FREE
    assert share_budget(2299.0, [heater])["heater"].action == ACTION_PAUSED


def test_two_chargers_share_in_the_sites_order() -> None:
    first, second = _car("first"), _car("second")
    shares = share_budget(13000.0, [first, second])
    # The first takes its 16 A (11 040 W); the 1960 W left is below the second's minimum.
    assert shares["first"].action == ACTION_FREE
    assert shares["second"].action == ACTION_PAUSED
    shares = share_budget(16000.0, [first, second])
    assert shares["second"].action == ACTION_HELD and shares["second"].cap_a == 7.0


def test_a_charger_the_target_may_not_hold_is_counted_at_what_it_draws() -> None:
    person = _car("person", wants=10.0, holdable=False)
    plan = _car("plan")
    shares = share_budget(10000.0, [person, plan])
    assert shares["person"].action == ACTION_UNHELD and shares["person"].cap_a is None
    # 10 000 - 6900 W leaves 3100 W: below the plan's minimum.
    assert shares["plan"].action == ACTION_PAUSED


def test_a_charger_that_asks_for_nothing_is_free_and_takes_nothing() -> None:
    shares = share_budget(5000.0, [_car("idle", wants=0.0), _car("busy")])
    assert shares["idle"].action == ACTION_FREE and shares["idle"].room_a == 7.0
    assert shares["busy"].cap_a == 7.0


# -- the departure


def test_the_plan_still_expects_its_windows_time_left_before_the_departure() -> None:
    now = HOUR
    windows = [(HOUR - timedelta(hours=1), HOUR + timedelta(hours=1)), (HOUR + timedelta(hours=3), HOUR + timedelta(hours=5))]
    assert plan_remaining_kwh(windows, 11.0, now, HOUR + timedelta(hours=4)) == pytest.approx(22.0)


def test_the_shortfall_is_what_the_held_power_leaves_short_of_the_departure() -> None:
    now = HOUR + timedelta(minutes=30)
    common = {"now": now, "hour_end": HOUR + timedelta(hours=1), "held_kw": 4.8, "later_kw": 5.0}
    # Three and a half hours left: 2.4 kWh this half hour and 15 kWh after.
    assert departure_shortfall_kwh(need_kwh=17.0, departure=HOUR + timedelta(hours=4), **common) == pytest.approx(-0.4)
    assert departure_shortfall_kwh(need_kwh=18.0, departure=HOUR + timedelta(hours=4), **common) == pytest.approx(0.6)
    # A departure within the hour counts this hour's held power only.
    assert departure_shortfall_kwh(need_kwh=2.0, departure=HOUR + timedelta(minutes=50), **common) == pytest.approx(0.4)
    # A departure gone, or nothing needed: nothing to say.
    assert departure_shortfall_kwh(need_kwh=50.0, departure=now, **common) is None
    assert departure_shortfall_kwh(need_kwh=0.0, departure=HOUR + timedelta(hours=4), **common) is None


# -- never raising what the fuse carries


def test_a_lowering_keeps_every_phase_an_import() -> None:
    floor = lowering_floor_a(
        phases=("L1", "L2", "L3"),
        measured_a={"L1": 16.0, "L2": 16.0, "L3": 16.0},
        signed_power_w={"L1": 4600.0, "L2": 2300.0, "L3": 4000.0},
        voltage_v={"L1": 230.0, "L2": 230.0, "L3": 230.0},
    )
    # L2 imports 10 A in all: the car may come down by those 10 A, to 6 A, and no further.
    assert floor == pytest.approx(6.0)


def test_a_phase_exporting_allows_no_lowering_and_an_unknown_one_none_at_all() -> None:
    common = {"phases": ("L1",), "measured_a": {"L1": 10.0}, "voltage_v": {"L1": 230.0}}
    assert lowering_floor_a(signed_power_w={"L1": -500.0}, **common) == pytest.approx(10.0)
    assert lowering_floor_a(signed_power_w={"L1": None}, **common) is None


def test_on_a_signed_current_the_drop_also_stays_within_the_margin() -> None:
    floor = lowering_floor_a(
        phases=("L1",),
        measured_a={"L1": 16.0},
        signed_power_w={"L1": 4600.0},
        voltage_v={"L1": 230.0},
        margin_a={"L1": 3.0},
    )
    assert floor == pytest.approx(13.0)


# -- the hour tracker keeps a battery's discharge beside the hour


def test_the_tracker_keeps_a_discharge_beside_the_hour_and_reads_a_record_without_one() -> None:
    from custom_components.spotnav.site.demand_tracker import HourTracker

    tracker = HourTracker(tz="Europe/Stockholm")
    tracker.observe(HOUR, 1000.0, 0.0, 2000.0)
    tracker.observe(HOUR + timedelta(seconds=60), 1000.0, 0.0, None)
    tracker.observe(HOUR + timedelta(seconds=120), 1000.0, 0.0, 2000.0)
    running = tracker.running
    assert running is not None
    # One minute at 2 kW of discharge; the minute it could not be read is not counted, nor its time.
    assert running.discharge_wh == pytest.approx(2000.0 / 60)
    assert running.discharge_covered_s == pytest.approx(60.0)
    assert running.import_wh == pytest.approx(2000.0 / 60)
    kept = running.as_dict()
    assert RunningHour.from_dict(kept).discharge_covered_s == pytest.approx(60.0)
    older = {key: value for key, value in kept.items() if not key.startswith("discharge")}
    restored = RunningHour.from_dict(older)
    assert restored is not None and restored.discharge_wh == 0.0 and restored.import_wh == pytest.approx(2000.0 / 60, abs=1e-3)


def test_a_departure_is_at_risk_past_a_margin_and_safe_again_only_with_room_to_spare() -> None:
    assert not departure_at_risk(0.4, at_risk=False)
    assert departure_at_risk(0.6, at_risk=False)
    # Once at risk it stays so until the held power has half a kWh to spare.
    assert departure_at_risk(0.0, at_risk=True)
    assert departure_at_risk(-0.4, at_risk=True)
    assert not departure_at_risk(-0.6, at_risk=True)
    assert not departure_at_risk(None, at_risk=True)


def test_a_window_the_target_held_is_planned_again_at_its_end_once_it_is_behind() -> None:
    from custom_components.spotnav.planning.plan_watch import (
        DECISION_END_REPLAN,
        HELD_IMPORT_TARGET,
        PlanWatch,
        WindowFacts,
    )

    def night(held: str) -> tuple[str | None, float]:
        watch = PlanWatch()
        start, end = HOUR, HOUR + timedelta(hours=1)
        decision = None
        shortfall = 0.0
        # The plan counts on 10 kW; the car delivers 9 (90 %, above the 80 % share).
        for minute in range(0, 61, 1):
            at = start + timedelta(minutes=minute)
            facts = WindowFacts(
                window=(start, end) if at < end else None,
                power_kw=10.0,
                reading_kwh=9.0 * minute / 60.0,
                reading_at=at,
                held=held,
            )
            decision = watch.check(at, facts) or decision
            if minute == 50:
                shortfall = watch.shortfall_kwh()
        return decision, shortfall

    decision, shortfall = night(HELD_IMPORT_TARGET)
    assert decision == DECISION_END_REPLAN
    assert shortfall == pytest.approx(50 / 60, abs=0.05)
    assert night("balancing_limit")[0] is None, "any other hold of SpotNav's keeps the 80 % share"


# -- the plan's choice when the car cannot be ready under the target (`if_not_ready`)


def test_the_plans_choice_is_stored_only_once_chosen_and_kept_by_a_write_that_leaves_it_out() -> None:
    from dataclasses import replace

    from custom_components.spotnav.api.settings import replacement_mutator
    from custom_components.spotnav.api.webhook import _for_app
    from custom_components.spotnav.planning.auto_settings import AutoSettings

    default = AutoSettings()
    assert default.if_not_ready == "ask" and "if_not_ready" not in default.as_dict()
    held = replace(default, if_not_ready="hold")
    assert held.as_dict()["if_not_ready"] == "hold"
    assert AutoSettings.from_stored(held.as_dict()).if_not_ready == "hold"
    # An older client's full write leaves it out: what was chosen is kept.
    kept = replacement_mutator(replace(default, amps=10), keep_if_not_ready=True)(held)
    assert kept.if_not_ready == "hold" and kept.amps == 10
    assert replacement_mutator(replace(default, if_not_ready="release"))(held).if_not_ready == "release"
    with pytest.raises(Exception):
        replace(default, if_not_ready="sometimes").validated()
    # An app released before it never sees it.
    body = {"settings": {"amps": 10, "if_not_ready": "hold"}}
    assert "if_not_ready" not in _for_app(body, {"action": "dashboard"})["settings"]
    assert _for_app(body, {"action": "dashboard", "reads": ["if_not_ready"]})["settings"]["if_not_ready"] == "hold"

