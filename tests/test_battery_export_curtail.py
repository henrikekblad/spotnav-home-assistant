"""No solar export at a negative sell price: the rule, the plan's export cap, the review's figure, and the export
limit run by the executor against a fake Sigenergy plant (negative and positive transitions, the restore, a change
by someone else, `unknown`, the shared day cap, a battery in its own optimiser, the verification's two modes)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.commands import (
    ACTION_CURTAIL_EXPORT,
    EXPORT_CONTROLS,
    EXPORT_LIMIT_TABLE,
    SIGENERGY_EXPORT_CONTROL,
    control_for,
    export_control_for,
    export_table,
)
from custom_components.spotnav.battery.executor import DAILY_WRITE_CAP, ExecState, Executor, preview
from custom_components.spotnav.battery.export_curtail import (
    MAX_GAP_S,
    TALLY_DAYS,
    CurtailLook,
    CurtailShadow,
    curtail_slots,
    curtails,
    would_save,
)
from custom_components.spotnav.battery.facts import BatteryFacts, Management
from custom_components.spotnav.battery.ledger import READBACK_DEADLINE_S
from custom_components.spotnav.battery.monitor import Capability
from custom_components.spotnav.battery.plan import plan_battery, sell_prices
from custom_components.spotnav.battery.plan_model import BatteryParams, PlanRequest, PlanSlot
from custom_components.spotnav.battery.plan_shadow import (
    BUCKET,
    Bucket,
    DayReview,
    PriceSlot,
    build_slots,
    review_day,
    review_totals,
)
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S
from custom_components.spotnav.battery.verify import Sample, Verification
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, inputs, written

UTC = timezone.utc
TZ = "Europe/Stockholm"
MIDNIGHT = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
BATTERY = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=10, max_soc_pct=100)


# --- the rule ------------------------------------------------------------------------------------------------------


def test_a_slot_curtails_when_what_an_exported_kwh_earns_is_below_zero_credit_included() -> None:
    # With a 5 öre credit per exported kWh the spot must be below -5 öre.
    sells = sell_prices([-0.04, -0.05, -0.06, 0.10], 5.0)
    assert [curtails(sell) for sell in sells] == [False, False, True, False]
    assert curtails(None) is False and curtails(float("nan")) is False
    assert would_save(2.0, -10.0) == pytest.approx(20.0)
    assert would_save(2.0, 10.0) == 0.0 and would_save(-1.0, -10.0) == 0.0


def test_the_plan_never_sells_at_a_negative_price_and_stores_the_sun_first() -> None:
    start = MIDNIGHT
    hour = timedelta(hours=1)
    # Six sunny hours at noon: 6 kW of sun on a 1 kW house; the middle two sell below zero.
    sells = [20.0, 20.0, -30.0, -30.0, 20.0, 20.0]
    slots = tuple(
        PlanSlot(start + hour * i, start + hour * (i + 1), buy=100.0, sell=sell, house_kw=1.0, solar_kw=6.0)
        for i, sell in enumerate(sells)
    )
    capped = curtail_slots(slots)
    assert [slot.export_cap_kw for slot in capped] == [None, None, 0.0, 0.0, None, None]
    plan = plan_battery(PlanRequest(slots=capped, battery=BATTERY, soc_now_pct=10.0))
    decisions = plan.plan.decisions
    for decision in decisions[2:4]:
        assert decision.grid_kw >= -1e-9, "nothing is exported at a negative price"
        assert decision.cost >= -1e-9
    # The battery takes the sun in the curtail hours (it is worth something later, the export costs money) ...
    assert decisions[2].battery_kw > 4.0
    # ... and what does not fit is curtailed, never sold.
    assert sum(decision.curtailed_kwh for decision in decisions) > 0
    # Without the cap the same plan would pay to export in those hours.
    free = plan_battery(PlanRequest(slots=slots, battery=replace(BATTERY, usable_kwh=1.0), soc_now_pct=100.0))
    assert any(d.grid_kw < -0.1 and d.cost > 0 for d in free.plan.decisions[2:4])


def test_build_slots_caps_the_export_of_every_slot_that_sells_below_zero() -> None:
    step = timedelta(hours=1)
    prices = [PriceSlot(MIDNIGHT + step * i, MIDNIGHT + step * (i + 1), 100.0, sell) for i, sell in enumerate((5.0, -1.0))]
    house = {MIDNIGHT + step * i: 1.0 for i in range(2)}
    slots, _estimates = build_slots(MIDNIGHT, prices, house, None, [], None, TZ)
    assert [slot.export_cap_kw for slot in slots] == [None, 0.0]


def test_the_review_counts_what_was_exported_below_zero_and_what_it_cost() -> None:
    buckets: list[Bucket] = []
    for index in range(96):
        hour = index // 4
        bucket = Bucket(MIDNIGHT + BUCKET * index, buy=100.0, sell=-20.0 if hour in (12, 13) else 30.0, covered_s=900)
        bucket.export_kwh = 0.5  # 2 kW all day
        bucket.soc_start = bucket.soc_end = 50.0
        buckets.append(bucket)
    review = review_day(date(2026, 11, 13), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1))
    assert review is not None
    assert review.curtail_kwh == pytest.approx(4.0)
    assert review.curtail_saving == pytest.approx(80.0)
    stored = review.as_dict()
    assert stored["curtail"] == {"kwh": 4.0, "saving": 80.0}
    assert DayReview.from_dict(stored).as_dict() == stored
    old = {key: value for key, value in stored.items() if key != "curtail"}
    assert DayReview.from_dict(old).curtail_kwh == 0.0, "a review from before the rule has none"
    assert review_totals([review])["curtail"] == {"kwh": 4.0, "saving": 80.0}


# --- the shadow ------------------------------------------------------------------------------------------------------


def _look(at: datetime, sell: float | None, grid_kw: float | None = -3.0, blocker: str | None = None) -> CurtailLook:
    return CurtailLook(at, at.date().isoformat(), sell, grid_kw, blocker)


def test_the_shadow_enters_and_leaves_with_the_price_and_counts_the_export_it_would_have_stopped() -> None:
    shadow = CurtailShadow()
    now = T0
    assert shadow.step(_look(now, 10.0)) is None
    now += TICK
    assert shadow.step(_look(now, -10.0)) == "enter"
    for _ in range(36):  # six minutes at 3 kW export
        now += TICK
        assert shadow.step(_look(now, -10.0)) is None
    now += TICK
    assert shadow.step(_look(now, 5.0)) == "leave"
    count = shadow.days[T0.date().isoformat()]
    assert count.kwh == pytest.approx(3.0 * 360 / 3600)
    assert count.saving == pytest.approx(count.kwh * 10.0)
    assert count.blocked_kwh == 0.0
    # A gap is not counted; an import is not an export.
    now += TICK
    shadow.step(_look(now, -10.0))
    now += timedelta(seconds=MAX_GAP_S + 1)
    shadow.step(_look(now, -10.0))
    now += TICK
    shadow.step(_look(now, -10.0, grid_kw=2.0))
    assert shadow.days[T0.date().isoformat()].kwh == pytest.approx(count.kwh)
    restored = CurtailShadow.from_dict(shadow.as_dict())
    assert restored.as_dict() == shadow.as_dict()
    assert CurtailShadow.from_dict("x").as_dict() == CurtailShadow().as_dict()


def test_export_the_control_could_not_have_stopped_is_counted_apart() -> None:
    shadow = CurtailShadow()
    shadow.step(_look(T0, -10.0, blocker="self_managed:EMS work mode:ai_mode"))
    shadow.step(_look(T0 + TICK, -10.0, blocker="self_managed:EMS work mode:ai_mode"))
    count = shadow.days[T0.date().isoformat()]
    assert count.blocked_kwh == pytest.approx(count.kwh) and count.kwh > 0
    assert shadow.totals()["blocked_saving"] == pytest.approx(count.saving, abs=1e-3)


def test_the_shadow_keeps_a_bounded_number_of_days() -> None:
    shadow = CurtailShadow()
    for day in range(TALLY_DAYS + 5):
        at = T0 + timedelta(days=day)
        shadow.step(_look(at, -10.0))
        shadow.step(_look(at + TICK, -10.0))
    assert len(shadow.days) == TALLY_DAYS


# --- the export limit on the write path --------------------------------------------------------------------------------


@dataclass
class FakePlant(FakeBattery):
    plant_max_kw: float | None = 13.2

    def facts(self, power_kw: float) -> BatteryFacts:
        return replace(super().facts(power_kw), plant_max_kw=self.plant_max_kw)


def _plant(limit: str = "unknown", **kwargs) -> tuple[Executor, FakePlant]:
    return Executor(SIGENERGY_EXPORT_CONTROL), FakePlant(SIGENERGY_EXPORT_CONTROL, {"export_limit": limit}, **kwargs)


def _run(executor: Executor, plant: FakePlant, start: datetime, looks: int, action: str | None, **overrides):
    now = start
    results = []
    failed: tuple[str, ...] = ()
    for _ in range(looks):
        result = executor.step(inputs(plant, now, action, failed=failed, **overrides))
        results.append(result)
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        now += TICK
    return now, results


def test_the_export_control_is_sigenergys_grid_export_limitation_and_no_other_brand_has_one() -> None:
    assert EXPORT_CONTROLS == (SIGENERGY_EXPORT_CONTROL,)
    assert export_control_for("sigenergy") is SIGENERGY_EXPORT_CONTROL
    assert control_for("sigenergy") is not SIGENERGY_EXPORT_CONTROL
    for brand in ("solaredge", "huawei", "solax", "victron", "goodwe", "enphase", None):
        assert export_control_for(brand) is None
    role = SIGENERGY_EXPORT_CONTROL.role("export_limit")
    assert role.match.matches("number", "01J_plant_plant_grid_point_maximum_export_limitation")
    assert not role.match.matches("number", "01J_plant_plant_pcs_maximum_export_limitation")
    assert SIGENERGY_EXPORT_CONTROL.wanted(ACTION_CURTAIL_EXPORT, 0.0) == {"export_limit": 0.0}
    assert export_table()[0]["writes"] == {"export_limit": 0.0}
    mapped = {row["brand"] for row in EXPORT_LIMIT_TABLE if row["mapped"]}
    assert mapped == {control.brand for control in EXPORT_CONTROLS}
    assert {row["brand"] for row in EXPORT_LIMIT_TABLE} >= {
        "sigenergy", "solaredge", "huawei", "solax", "sungrow", "fronius", "victron", "goodwe"
    }


def test_a_negative_price_sets_the_limit_to_zero_and_a_positive_one_restores_the_value_found() -> None:
    executor, plant = _plant("11.5")
    now, results = _run(executor, plant, T0, 4, ACTION_CURTAIL_EXPORT)
    assert written(results) == [("export_limit", 0.0)]
    assert plant.states["export_limit"] == "0"
    assert executor.state.phase == "active" and events(results, "confirmed")
    assert executor.state.restore_values == {"export_limit": 11.5}
    _now, results = _run(executor, plant, now, 4, "self_consume")
    assert written(results) == [("export_limit", 11.5)]
    assert events(results, "hand_back")[0]["reason"] == "curtail_ended"
    assert plant.states["export_limit"] == "11.5"
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_the_export_limit_needs_no_battery_reading_or_fuse_headroom() -> None:
    executor, plant = _plant("11.5")
    _now, results = _run(
        executor, plant, T0, 4, ACTION_CURTAIL_EXPORT, power_kw=float("nan"), battery_age_s=None, headroom_a=None
    )
    assert written(results) == [("export_limit", 0.0)]
    assert executor.state.phase == "active"


def test_a_never_set_limit_is_restored_as_the_plant_maximum_and_unknown_is_never_written_back() -> None:
    executor, plant = _plant("unknown")
    now, _results = _run(executor, plant, T0, 4, ACTION_CURTAIL_EXPORT)
    assert executor.state.restore_values == {"export_limit": 13.2}
    plant.states["export_limit"] = "unknown"  # looks like the value found, proves nothing after SpotNav's write
    _now, results = _run(executor, plant, now, 4, "self_consume")
    assert written(results) == [("export_limit", 13.2)]
    assert all(value != "unknown" for _role, value in written(results))
    # Without the plant's maximum there is nothing to restore: it is not started.
    executor, plant = _plant("unknown", plant_max_kw=None)
    _now, results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "no_restore_value:export_limit"


def test_an_unknown_read_back_is_never_a_confirmed_write() -> None:
    executor, plant = _plant("11.5", read_only=True)
    plant.states["export_limit"] = "unknown"
    plant.plant_max_kw = 13.2
    looks = int(READBACK_DEADLINE_S / TICK.total_seconds()) + 4
    _now, results = _run(executor, plant, T0, looks, ACTION_CURTAIL_EXPORT)
    assert written(results)[0] == ("export_limit", 0.0)
    assert events(results, "confirmed") == [] and events(results, "external") == []
    assert events(results, "not_taken")[0]["reason"] == "export_limit"
    assert executor.state.blocked is not None and executor.state.blocked.startswith("write_not_taken")


def test_a_change_by_someone_else_is_given_up_and_never_written_back_over() -> None:
    executor, plant = _plant("11.5")
    now, _results = _run(executor, plant, T0, 4, ACTION_CURTAIL_EXPORT)
    plant.states["export_limit"] = "5"  # the app sets its own limit
    looks = int(MIN_DIVERGENCE_S / TICK.total_seconds()) + 6
    _now, results = _run(executor, plant, now, looks, ACTION_CURTAIL_EXPORT)
    assert events(results, "external")[0]["reason"] == "export_limit"
    assert written(results) == []
    assert plant.states["export_limit"] == "5"
    assert executor.state.phase == "blocked" and executor.state.blocked.startswith("changed_outside_spotnav")


def test_a_plant_in_its_own_optimiser_is_only_watched() -> None:
    executor, plant = _plant("11.5", work_mode_override="ai_mode")
    _now, results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "self_managed:EMS work mode:ai_mode"
    # Taken while the plant is free, the battery control's own Remote EMS later does not hand it back (a plant
    # already in Remote EMS is not taken: SpotNav cannot tell whose it is).
    executor, plant = _plant("11.5")
    now, _results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT)
    plant.work_mode_override = "remote_ems"
    _now, results = _run(executor, plant, now, 3, ACTION_CURTAIL_EXPORT)
    assert events(results, "hand_back") == [] and executor.state.phase == "active"
    # An outside controller marked on the facts: watched, whatever the mode reads.
    executor, plant = _plant("11.5")
    outside = Management("externally_controlled", "outside:setpoint", {"EMS work mode": "max_self_consumption"})
    _now, results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT, management=outside)
    assert written(results) == []


def test_the_day_write_cap_is_shared_with_the_battery_control() -> None:
    executor, plant = _plant("11.5")
    _now, results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT, other_writes_today=DAILY_WRITE_CAP)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "daily_write_cap"
    executor, plant = _plant("11.5")
    now, _results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT)
    assert executor.state.writes_today == 1
    _now, results = _run(executor, plant, now, 4, ACTION_CURTAIL_EXPORT, other_writes_today=DAILY_WRITE_CAP)
    assert events(results, "hand_back")[0]["reason"] == "daily_write_cap"
    assert plant.states["export_limit"] == "11.5", "the restore is never capped"


def test_a_restart_restores_the_stored_limit_first() -> None:
    executor, plant = _plant("11.5")
    _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT)
    stored = ExecState.from_dict(executor.state.as_dict())
    again = Executor(SIGENERGY_EXPORT_CONTROL, state=stored)
    assert again.restart(T0 + timedelta(minutes=5))[0]["reason"] == "restart"
    _now, results = _run(again, plant, T0 + timedelta(minutes=5), 4, ACTION_CURTAIL_EXPORT)
    assert ("export_limit", 11.5) in written(results)


def test_the_preview_shows_the_write_and_the_hand_back_without_writing() -> None:
    _executor, plant = _plant("11.5")
    shown = preview(SIGENERGY_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT))
    assert shown.blocker is None
    assert [(op.role, op.value) for op in shown.writes] == [("export_limit", 0.0)]
    assert [(op.role, op.value) for op in shown.restores] == [("export_limit", 11.5)]
    assert plant.writes == []
    plant.missing = {"export_limit"}
    assert preview(SIGENERGY_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT)).blocker == (
        "control_not_found:export_limit"
    )


def test_the_verification_judges_the_export_limit_on_the_grid_power() -> None:
    executor, plant = _plant("11.5")
    now = T0
    verification = Verification(executor, now, firmware="x")
    assert [result.mode for result in verification.results] == ["curtail", "normal"]
    grid_kw = -3.0
    capability = Capability(rated_charge_kw=None, rated_discharge_kw=None)
    failed: tuple[str, ...] = ()
    for _ in range(80):
        sample = Sample(grid_kw, 5.0)
        action, rate = verification.command(now, sample, None)
        result = executor.step(inputs(plant, now, action, rate, failed=failed))
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        grid_kw = 0.0 if plant.states["export_limit"] == "0" else -3.0
        verification.after(now, result, Sample(grid_kw, 5.0), capability)
        if verification.done:
            break
        now += timedelta(seconds=30)
    report = verification.report()
    assert report["modes"]["curtail"]["outcome"] == "obeyed"
    assert report["modes"]["normal"]["outcome"] == "restored"
    assert plant.states["export_limit"] == "11.5"
