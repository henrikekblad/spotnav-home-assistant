"""The battery plan's shadow policy (`battery/plan_shadow.py`): its inputs, when it plans again, the reasons per
slot, the quarter hours measured and the night's review. Pure: no Home Assistant.

Prices in öre per kWh, power in kW, energy in kWh.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.facts import BatteryFacts
from custom_components.spotnav.battery.plan import plan_battery
from custom_components.spotnav.battery.plan_model import (
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    BatteryParams,
    PlanRequest,
    PlanSlot,
)
from custom_components.spotnav.battery.plan_shadow import (
    ASSUMED_MIN_SOC_PCT,
    BUCKET,
    DID_DISCHARGE,
    DID_EXPORT_DISCHARGE,
    DID_GRID_CHARGE,
    DID_IDLE,
    DID_STORE_SUN,
    EST_HORIZON_CUT,
    EST_MIN_SOC,
    EST_NO_SOLAR,
    KEEP_MIN_MINOR,
    MAX_GAP_S,
    NO_BATTERY,
    NO_CAPACITY,
    NO_RATED_POWER,
    NO_SOC,
    REASON_AT_CEILING,
    REASON_CHARGE_FOR_LATER,
    REASON_CODES,
    REASON_COVER_HOUSE,
    REASON_HOLD_FOR_LATER,
    REASON_KEPT_RUNNING,
    REASON_OVER_CAP,
    SOC_DEVIATION_PCT,
    SOC_REPLAN_MIN_S,
    TRIGGER_FORECAST,
    TRIGGER_HOUR,
    TRIGGER_INPUTS,
    TRIGGER_PRICES,
    TRIGGER_SOC,
    TRIGGER_START,
    Bucket,
    DayReview,
    PlanActuals,
    review_totals,
    PlannedSlot,
    PriceSlot,
    TriggerMemory,
    battery_params,
    bucket_start,
    build_slots,
    car_kw_in,
    days_to_review,
    expected_soc_at,
    explain,
    import_cap_kw,
    keep_min_gain,
    plan_view,
    planned_slots,
    price_slots,
    replan_trigger,
    review_day,
    slot_at,
    soc_by_hour_end,
)
from custom_components.spotnav.planning.planner import FiscalChoice, effective_minor_per_kwh

UTC = timezone.utc
TZ = "Europe/Stockholm"
#: Midnight in Stockholm on a winter day (UTC+1).
MIDNIGHT = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
BATTERY = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=10, max_soc_pct=100)


def _facts(**kwargs) -> BatteryFacts:
    base = {
        "battery_entity_id": "sensor.battery_power",
        "soc_pct": 50.0,
        "capacity_kwh": 10.0,
        "rated_charge_kw": 5.0,
        "rated_discharge_kw": 5.0,
    }
    return BatteryFacts(**(base | kwargs))


def _prices(buy: list[float], *, start: datetime = MIDNIGHT, minutes: int = 60, sell: float = 40.0) -> list[PriceSlot]:
    step = timedelta(minutes=minutes)
    return [PriceSlot(start + step * i, start + step * (i + 1), price, sell) for i, price in enumerate(buy)]


def _house(hours: int, kw: float = 1.0, start: datetime = MIDNIGHT) -> dict[datetime, float]:
    return {start + timedelta(hours=i): kw for i in range(hours)}


# ------------------------------------------------------------------------------------------------ inputs


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"battery_entity_id": None}, NO_BATTERY),
        ({"soc_pct": None}, NO_SOC),
        ({"capacity_kwh": None}, NO_CAPACITY),
        ({"capacity_kwh": 0.0}, NO_CAPACITY),
        ({"rated_charge_kw": None}, NO_RATED_POWER),
        ({"rated_discharge_kw": None}, NO_RATED_POWER),
        ({"rated_charge_kw": 0.0, "rated_discharge_kw": 0.0}, NO_RATED_POWER),
    ],
)
def test_a_missing_battery_fact_gives_no_plan_and_says_which(change, reason) -> None:
    params, why, estimates = battery_params(_facts(**change))
    assert params is None and why == reason and estimates == ()


def test_the_battery_from_its_facts_with_the_floor_and_round_trip_assumed() -> None:
    params, why, estimates = battery_params(_facts(capacity_kwh=40.3, rated_charge_kw=20.6, rated_discharge_kw=23.04))
    assert why is None and params is not None
    assert (params.usable_kwh, params.max_charge_kw, params.max_discharge_kw) == (40.3, 20.6, 23.04)
    assert params.min_soc_pct == ASSUMED_MIN_SOC_PCT
    assert EST_MIN_SOC in estimates


def test_prices_are_the_planners_with_fees_and_spot_to_sell() -> None:
    fiscal = FiscalChoice(
        tax_enabled=True, tax_minor_per_kwh=42.8, transfer_enabled=True, transfer_minor_per_kwh=20.0,
        vat_enabled=True, vat_percent=25.0,
    )
    start = MIDNIGHT
    rows = [(start + timedelta(hours=1), start + timedelta(hours=2), 0.80), (start, start + timedelta(hours=1), 0.50)]
    slots = price_slots(rows, fiscal)
    assert [slot.start for slot in slots] == [start, start + timedelta(hours=1)]
    assert slots[0].buy == pytest.approx(effective_minor_per_kwh(0.50, fiscal, start))
    assert slots[0].buy == pytest.approx((50 + 42.8 + 20) * 1.25)
    assert [slot.sell for slot in slots] == pytest.approx([50.0, 80.0])


def test_the_import_cap_is_the_fuse_less_its_margin_and_the_import_target_where_lower() -> None:
    assert import_cap_kw(20, 1, 400, None) == pytest.approx(19 * math.sqrt(3) * 400 / 1000)
    assert import_cap_kw(20, None, 400, None) == pytest.approx(13.856, abs=1e-3)
    # An IT network (230 V between phases).
    assert import_cap_kw(25, 0, 230, None) == pytest.approx(25 * math.sqrt(3) * 230 / 1000)
    # The import target's held level (less its margin) is lower.
    assert import_cap_kw(20, 1, 400, 5.0) == pytest.approx(4.9)
    assert import_cap_kw(None, None, 400, 5.0) == pytest.approx(4.9)
    assert import_cap_kw(None, None, 400, None) is None


def test_the_cars_plans_count_for_their_share_of_a_slot() -> None:
    start = MIDNIGHT
    windows = [(start + timedelta(minutes=30), start + timedelta(hours=3), 11.0)]
    assert car_kw_in(start, start + timedelta(hours=1), windows) == pytest.approx(5.5)
    assert car_kw_in(start + timedelta(hours=1), start + timedelta(hours=2), windows) == pytest.approx(11.0)
    assert car_kw_in(start + timedelta(hours=3), start + timedelta(hours=4), windows) == 0.0


def test_slots_start_now_carry_load_sun_car_and_cap() -> None:
    now = MIDNIGHT + timedelta(minutes=20)
    prices = _prices([50, 60, 70], minutes=60)
    solar = {MIDNIGHT + timedelta(hours=1): 2500.0}
    windows = [(MIDNIGHT + timedelta(hours=2), MIDNIGHT + timedelta(hours=3), 7.0)]
    slots, estimates = build_slots(now, prices, _house(3, 1.5), solar, windows, 13.0, TZ)
    assert [slot.start for slot in slots] == [now, MIDNIGHT + timedelta(hours=1), MIDNIGHT + timedelta(hours=2)]
    assert slots[0].end == MIDNIGHT + timedelta(hours=1)
    assert [slot.house_kw for slot in slots] == [1.5, 1.5, 1.5]
    assert [slot.solar_kw for slot in slots] == [0.0, 2.5, 0.0]
    assert [slot.car_kw for slot in slots] == [0.0, 0.0, 7.0]
    assert {slot.import_cap_kw for slot in slots} == {13.0}
    assert estimates == ()


def test_the_horizon_ends_where_the_house_forecast_ends_and_without_sun_it_says_so() -> None:
    prices = _prices([50] * 6)
    slots, estimates = build_slots(MIDNIGHT, prices, _house(4), None, [], None, TZ)
    assert len(slots) == 4
    assert EST_HORIZON_CUT in estimates and EST_NO_SOLAR in estimates


def test_no_house_figure_for_the_first_slot_gives_no_slots() -> None:
    slots, _estimates = build_slots(MIDNIGHT, _prices([50] * 3), {}, None, [], None, TZ)
    assert slots == ()


def test_quarter_hour_prices_share_their_hours_house_and_sun() -> None:
    prices = _prices([50, 51, 52, 53, 54], minutes=15)
    slots, _ = build_slots(MIDNIGHT, prices, _house(2, 2.0), {MIDNIGHT: 1000.0}, [], None, TZ)
    assert len(slots) == 5
    assert [slot.solar_kw for slot in slots] == [1.0, 1.0, 1.0, 1.0, 0.0]


def test_a_25_hour_day_plans_through_its_repeated_hour() -> None:
    # 2026-10-25 in Stockholm: 01:00 UTC (03:00 CEST) and 02:00 UTC (02:00 CET) are both a local 02:00-03:00.
    start = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    prices = _prices([50] * 25, start=start)
    house = {start + timedelta(hours=i): 1.0 for i in range(25)}
    slots, estimates = build_slots(start, prices, house, None, [], None, TZ)
    assert len(slots) == 25 and EST_HORIZON_CUT not in estimates
    result = plan_battery(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=50))
    assert len(result.plan.decisions) == 25


def test_the_gain_needed_to_change_a_running_action() -> None:
    assert keep_min_gain(10.0, 24) == KEEP_MIN_MINOR
    # 2 % of a day's cost: a 48-hour horizon costing 20 000 öre is 10 000 a day.
    assert keep_min_gain(20_000.0, 48) == pytest.approx(200.0)
    assert keep_min_gain(-20_000.0, 48) == pytest.approx(200.0)


# ------------------------------------------------------------------------------------------------ triggers


def _keys(**change):
    keys = {
        "prices_key": ("SE4", 1),
        "forecast_key": 3,
        "hour": MIDNIGHT,
        "inputs_key": (True, 10.0),
        "soc_now": 50.0,
        "expected_soc": 50.0,
    }
    return keys | change


def _memory() -> TriggerMemory:
    return TriggerMemory(
        planned_at=MIDNIGHT, prices_key=("SE4", 1), forecast_key=3, hour=MIDNIGHT, inputs_key=(True, 10.0)
    )


@pytest.mark.parametrize(
    ("change", "trigger"),
    [
        ({}, None),
        ({"prices_key": ("SE4", 2)}, TRIGGER_PRICES),
        ({"forecast_key": 4}, TRIGGER_FORECAST),
        ({"hour": MIDNIGHT + timedelta(hours=1)}, TRIGGER_HOUR),
        ({"inputs_key": (True, 12.0)}, TRIGGER_INPUTS),
        ({"soc_now": 50.0 + SOC_DEVIATION_PCT + 1}, TRIGGER_SOC),
        ({"soc_now": 50.0 + SOC_DEVIATION_PCT - 1}, None),
        ({"soc_now": None, "expected_soc": 10.0}, None),
    ],
)
def test_what_plans_again(change, trigger) -> None:
    assert replan_trigger(_memory(), MIDNIGHT, **_keys(**change)) == trigger


def test_the_first_look_plans() -> None:
    assert replan_trigger(TriggerMemory(), MIDNIGHT, **_keys()) == TRIGGER_START


def test_a_soc_off_the_plan_plans_again_at_most_every_so_often() -> None:
    memory = _memory()
    memory.soc_replan_at = MIDNIGHT
    keys = _keys(soc_now=80.0)
    assert replan_trigger(memory, MIDNIGHT + timedelta(seconds=SOC_REPLAN_MIN_S - 1), **keys) is None
    assert replan_trigger(memory, MIDNIGHT + timedelta(seconds=SOC_REPLAN_MIN_S), **keys) == TRIGGER_SOC


def test_the_soc_the_plan_expects_between_its_slots() -> None:
    slots = (
        PlannedSlot(MIDNIGHT, MIDNIGHT + timedelta(hours=1), ACTION_GRID_CHARGE, 5.0, 60.0),
        PlannedSlot(MIDNIGHT + timedelta(hours=1), MIDNIGHT + timedelta(hours=2), ACTION_SELF_CONSUME, -1.0, 50.0),
    )
    assert expected_soc_at(slots, 40.0, MIDNIGHT + timedelta(minutes=30)) == pytest.approx(50.0)
    assert expected_soc_at(slots, 40.0, MIDNIGHT + timedelta(minutes=90)) == pytest.approx(55.0)
    assert expected_soc_at(slots, 40.0, MIDNIGHT + timedelta(hours=3)) is None
    assert slot_at(slots, MIDNIGHT + timedelta(minutes=61)) is slots[1]
    assert PlannedSlot.from_row(slots[0].as_row()) == slots[0]
    assert PlannedSlot.from_row(["x", "y", "boost", 1, 2]) is None


# ------------------------------------------------------------------------------------------------ reasons


def test_each_slot_says_why_in_stable_codes() -> None:
    slots, _ = build_slots(MIDNIGHT, _prices([20, 20, 300, 300]), _house(4, 3.0), None, [], 30.0, TZ)
    result = plan_battery(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=10, end_value=0))
    reasons = explain(result, slots)
    codes = [[reason["code"] for reason in slot] for slot in reasons]
    assert all(code in REASON_CODES for slot in codes for code in slot)
    first = reasons[0][0]
    assert first["code"] == REASON_CHARGE_FOR_LATER
    assert first["params"]["price"] == 20 and first["params"]["later_price"] == 300
    assert first["params"]["later_start"] == (MIDNIGHT + timedelta(hours=2)).isoformat()
    assert REASON_COVER_HOUSE in codes[2]


def test_a_hold_names_the_dear_slot_it_keeps_the_energy_for() -> None:
    slots, _ = build_slots(MIDNIGHT, _prices([20, 20, 20, 300, 300, 300]), _house(6, 3.0), None, [], None, TZ)
    result = plan_battery(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=100, end_value=0))
    reasons = explain(result, slots)
    assert reasons[0][0]["code"] == REASON_HOLD_FOR_LATER
    assert REASON_AT_CEILING in [reason["code"] for reason in reasons[0]]


def test_over_the_cap_and_a_kept_action_are_said() -> None:
    slots, _ = build_slots(MIDNIGHT, _prices([100, 100]), _house(2, 8.0), None, [], 2.0, TZ)
    request = PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=10, end_value=0)
    result = plan_battery(request, keep=(ACTION_HOLD, 0.0), keep_min_gain=1e9)
    reasons = explain(result, slots)
    assert reasons[0][0]["code"] == REASON_KEPT_RUNNING
    assert REASON_OVER_CAP in [reason["code"] for reason in reasons[0]]
    view = plan_view(result, slots)
    assert view["kept"] is True
    row = view["slots"][0]
    assert {"buy", "sell", "house_kw", "baseline_cost", "baseline_soc_end_pct", "reasons"} <= set(row)


def test_the_planned_soc_at_each_hours_end_for_the_forecast_log() -> None:
    now = MIDNIGHT + timedelta(minutes=20)
    slots, _ = build_slots(now, _prices([50] * 8, minutes=15), _house(2), None, [], None, TZ)
    result = plan_battery(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=50))
    socs = soc_by_hour_end(result.plan.decisions, TZ)
    assert list(socs) == [MIDNIGHT, MIDNIGHT + timedelta(hours=1)]
    assert socs[MIDNIGHT] == pytest.approx(result.plan.decisions[2].soc_end_pct)


# ------------------------------------------------------------------------------------------------ actuals


def _ran(action: str = ACTION_SELF_CONSUME, kw: float = 0.0, soc: float = 50.0) -> PlannedSlot:
    return PlannedSlot(MIDNIGHT, MIDNIGHT + timedelta(days=2), action, kw, soc)


def test_a_quarter_hour_adds_up_its_readings() -> None:
    actuals = PlanActuals()
    actuals.open(MIDNIGHT, price=PriceSlot(MIDNIGHT, MIDNIGHT + BUCKET, 100, 40), cap_kw=13.0, ran=_ran())
    for second in range(0, 900 + 1, 10):
        grid = 4.0 if second < 450 else -2.0
        actuals.observe(MIDNIGHT + timedelta(seconds=second), grid, 1.0, 0.5, 40.0 + second / 900)
    bucket = actuals.buckets[MIDNIGHT]
    assert bucket.covered_s == pytest.approx(900)
    assert bucket.import_kwh == pytest.approx(4.0 * 0.125)
    assert bucket.export_kwh == pytest.approx(2.0 * 0.125)
    assert bucket.car_kwh == pytest.approx(0.25) and bucket.battery_kwh == pytest.approx(0.125)
    assert bucket.soc_start == pytest.approx(40.0) and bucket.soc_end == pytest.approx(40.0 + 890 / 900)
    assert (bucket.buy, bucket.sell, bucket.cap_kw, bucket.ran_action) == (100, 40, 13.0, ACTION_SELF_CONSUME)
    # The house less the sun: the grid less the car and the battery.
    assert bucket.net_house_kw == pytest.approx((0.5 - 0.25 - 0.25 - 0.125) * 4)


def test_a_gap_or_an_unknown_reading_counts_nothing() -> None:
    actuals = PlanActuals()
    actuals.open(MIDNIGHT, price=None, cap_kw=None, ran=None)
    actuals.observe(MIDNIGHT, 2.0, 0.0, 0.0, None)
    actuals.observe(MIDNIGHT + timedelta(seconds=MAX_GAP_S + 1), None, 0.0, 0.0, None)
    actuals.observe(MIDNIGHT + timedelta(seconds=MAX_GAP_S + 11), 2.0, 0.0, None, None)
    actuals.observe(MIDNIGHT + timedelta(seconds=MAX_GAP_S + 21), 2.0, 0.0, 0.0, None)
    bucket = actuals.buckets[MIDNIGHT]
    assert bucket.covered_s == 0 and bucket.import_kwh == 0
    assert bucket.covered is False and bucket.net_house_kw is None and bucket.actual_action() is None


@pytest.mark.parametrize(
    ("grid", "battery", "did"),
    [
        (12.0, 10.0, DID_GRID_CHARGE),
        (-1.0, 3.0, DID_STORE_SUN),
        (0.0, -2.0, DID_DISCHARGE),
        (-5.0, -6.0, DID_EXPORT_DISCHARGE),
        (2.0, 0.05, DID_IDLE),
    ],
)
def test_what_the_battery_really_did_for_the_benchmark(grid, battery, did) -> None:
    bucket = Bucket(MIDNIGHT, covered_s=900)
    hours = 0.25
    bucket.import_kwh = max(0.0, grid) * hours
    bucket.export_kwh = max(0.0, -grid) * hours
    bucket.battery_kwh = battery * hours
    assert bucket.actual_action() == did
    assert bucket.view()["actual"] == did


def test_first_planned_actions_wait_for_their_quarter_hours() -> None:
    actuals = PlanActuals()
    slots = (
        PlannedSlot(MIDNIGHT, MIDNIGHT + timedelta(minutes=30), ACTION_GRID_CHARGE, 4.0, 70.0),
        PlannedSlot(MIDNIGHT + timedelta(minutes=30), MIDNIGHT + timedelta(hours=1), ACTION_HOLD, 0.0, 70.0),
    )
    actuals.note_first(slots, 50.0)
    later = (PlannedSlot(MIDNIGHT, MIDNIGHT + timedelta(hours=1), ACTION_SELF_CONSUME, -1.0, 40.0),)
    actuals.note_first(later, 50.0)
    assert actuals.firsts[MIDNIGHT] == (ACTION_GRID_CHARGE, pytest.approx(60.0))
    assert actuals.firsts[MIDNIGHT + timedelta(minutes=45)][0] == ACTION_HOLD
    bucket = actuals.open(MIDNIGHT, price=None, cap_kw=None, ran=later[0])
    assert (bucket.first_action, bucket.ran_action) == (ACTION_GRID_CHARGE, ACTION_SELF_CONSUME)
    assert MIDNIGHT not in actuals.firsts


def _measured_hour(actuals: PlanActuals, start: datetime, *, grid: float, car: float, battery: float, soc: float) -> None:
    for quarter in range(4):
        moment = start + BUCKET * quarter
        actuals.open(moment, price=PriceSlot(moment, moment + BUCKET, 100, 40), cap_kw=None, ran=None)
        for second in range(0, 900, 10):
            actuals.observe(moment + timedelta(seconds=second), grid, car, battery, soc)
    actuals.observe(start + timedelta(hours=1), grid, car, battery, soc)


def test_a_completed_hour_becomes_a_house_row_net_of_the_battery() -> None:
    actuals = PlanActuals()
    # Sigenergy grid-charging at night: 12 kW from the grid, 10 of them into the battery, no car.
    _measured_hour(actuals, MIDNIGHT, grid=12.0, car=0.0, battery=10.0, soc=55.0)
    assert actuals.close_hours(MIDNIGHT + timedelta(minutes=30), TZ) == []
    assert actuals.close_hours(MIDNIGHT + timedelta(hours=1, seconds=5), TZ) == [MIDNIGHT]
    assert actuals.rows[MIDNIGHT] == pytest.approx(2.0)
    assert actuals.soc_at_hour_end(MIDNIGHT, TZ) == 55.0


def test_the_record_survives_a_restart_and_drops_what_is_broken_or_old() -> None:
    actuals = PlanActuals()
    _measured_hour(actuals, MIDNIGHT, grid=3.0, car=1.0, battery=-0.5, soc=40.0)
    actuals.close_hours(MIDNIGHT + timedelta(hours=1, seconds=1), TZ)
    actuals.note_first((PlannedSlot(MIDNIGHT + timedelta(hours=2), MIDNIGHT + timedelta(hours=3), ACTION_HOLD, 0, 40),), 40)
    raw = actuals.as_dict()
    raw["buckets"].append(["nonsense"])
    raw["buckets"].append([MIDNIGHT.isoformat(), None, None, None, 1, 1, 1, 1, -5, None, None, None, None, None, None, None, None, None])
    raw["firsts"].append(["x", "boost", 1])
    restored = PlanActuals.from_dict(raw, MIDNIGHT + timedelta(hours=1, seconds=1))
    assert restored.buckets.keys() == actuals.buckets.keys()
    assert restored.buckets[MIDNIGHT].as_row() == actuals.buckets[MIDNIGHT].as_row()
    assert restored.rows == pytest.approx(actuals.rows)
    assert restored.firsts.keys() == actuals.firsts.keys()
    aged = PlanActuals.from_dict(raw, MIDNIGHT + timedelta(days=50))
    assert aged.buckets == {} and aged.rows == {} and aged.firsts == {}
    assert PlanActuals.from_dict(None, MIDNIGHT).buckets == {}


def test_bucket_starts_are_utc_quarter_hours() -> None:
    assert bucket_start(MIDNIGHT + timedelta(minutes=29, seconds=59)) == MIDNIGHT + timedelta(minutes=15)


# ------------------------------------------------------------------------------------------------ the review


def _day(
    *, buy: list[float], house: float, battery: list[float], soc: list[float], ran: list[tuple[str, float]] | None
) -> list[Bucket]:
    """One local day of quarter hours (96), one figure per hour repeated over its quarters."""
    buckets: list[Bucket] = []
    for index in range(96):
        hour = index // 4
        start = MIDNIGHT + BUCKET * index
        grid = house + battery[hour]
        bucket = Bucket(start, buy=buy[hour], sell=buy[hour] - 60, covered_s=900)
        bucket.import_kwh = max(0.0, grid) * 0.25
        bucket.export_kwh = max(0.0, -grid) * 0.25
        bucket.battery_kwh = battery[hour] * 0.25
        bucket.soc_start = soc[hour]
        bucket.soc_end = soc[hour + 1] if index % 4 == 3 else soc[hour]
        if ran is not None:
            bucket.ran_action, bucket.ran_kw = ran[hour]
            bucket.ran_soc = bucket.soc_end
        buckets.append(bucket)
    return buckets


CHEAP_NIGHT = [20.0] * 6 + [300.0] * 18


def test_a_day_with_an_idle_battery_scores_the_plan_against_self_consumption() -> None:
    # The battery did nothing (empty, at its floor); the plan would have charged in the 6 cheap hours.
    ran = [(ACTION_GRID_CHARGE, 1.5)] * 6 + [(ACTION_SELF_CONSUME, 0.0)] * 18
    buckets = _day(buy=CHEAP_NIGHT, house=1.0, battery=[0.0] * 24, soc=[10.0] * 25, ran=ran)
    review = review_day(date(2026, 11, 13), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1))
    assert isinstance(review, DayReview)
    assert review.quarters == 96 and review.covered == 96 and review.unplanned == 0
    assert review.actual.cost == pytest.approx(6 * 20 + 18 * 300)
    # Self-consumption from an empty battery and no sun is the same as what happened.
    assert review.self_consumption.cost == pytest.approx(review.actual.cost)
    assert review.actual_saving == pytest.approx(0.0, abs=1e-6)
    assert review.plan.cost < review.actual.cost - 1000  # it pays for the night's charge and saves in the dear hours
    assert review.saving > 1000
    assert review.plan_export is None and review.export_saving is None
    assert review.soc_mae == pytest.approx(0.0)
    as_dict = review.as_dict()
    assert as_dict["plan"]["net"] == pytest.approx(review.net(review.plan), abs=0.01)
    assert DayReview.from_dict(as_dict).as_dict() == as_dict
    assert DayReview.from_dict({"date": "x"}) is None


def test_a_battery_emptied_at_the_end_is_charged_for_it() -> None:
    # The battery discharged all day into the house from full: cheap money, but the stored energy is gone.
    battery = [-0.375] * 24
    soc = [100.0 - 90.0 * hour / 24 for hour in range(25)]
    buckets = _day(buy=[100.0] * 24, house=1.0, battery=battery, soc=soc, ran=None)
    review = review_day(date(2026, 11, 13), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1))
    assert review is not None
    assert review.unplanned == 96
    assert review.actual.end_soc_pct == pytest.approx(10.0)
    # Raw money flatters the battery that emptied itself; the net charges it for the 9 kWh gone.
    assert review.net(review.actual) == pytest.approx(review.actual.cost + 9 * review.end_value)
    # Self-consumption follows the same load from the same SoC: the two match within the round trip's loss.
    assert abs(review.actual_saving) < 0.15 * review.end_value * 9


def test_quarter_hours_without_meters_count_in_no_run_and_no_soc_gives_no_review() -> None:
    buckets = _day(buy=[100.0] * 24, house=1.0, battery=[0.0] * 24, soc=[50.0] * 25, ran=None)
    for bucket in buckets[:48]:
        bucket.covered_s = 100
    review = review_day(date(2026, 11, 13), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1))
    assert review is not None and review.covered == 48
    assert review.actual.cost == pytest.approx(12 * 100)
    for bucket in buckets:
        bucket.soc_start = bucket.soc_end = None
    assert review_day(date(2026, 11, 13), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1)) is None
    assert review_day(date(2026, 11, 14), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=2)) is None


def test_only_days_that_ended_are_reviewed_once() -> None:
    buckets = [Bucket(MIDNIGHT + timedelta(hours=h)) for h in (0, 23, 25)]
    now = MIDNIGHT + timedelta(days=1, hours=2)
    assert days_to_review(buckets, [], now, TZ) == [date(2026, 11, 13)]
    assert days_to_review(buckets, [date(2026, 11, 13)], now, TZ) == []


def test_planned_slots_round_trip_the_plan() -> None:
    slots, _ = build_slots(MIDNIGHT, _prices([20, 300]), _house(2), None, [], None, TZ)
    result = plan_battery(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=50))
    kept = planned_slots(result.plan.decisions)
    assert [slot.action for slot in kept] == [d.action for d in result.plan.decisions]
    assert isinstance(slots[0], PlanSlot)


def test_the_export_variant_is_reviewed_beside_the_plan_and_summed() -> None:
    # A dear evening with a high sell price: the variant that may sell from a full battery earns more.
    buy = [20.0] * 6 + [100.0] * 12 + [400.0] * 6
    ran = [(ACTION_GRID_CHARGE, 5.0)] * 6 + [(ACTION_HOLD, 0.0)] * 12 + [(ACTION_SELF_CONSUME, 0.0)] * 6
    buckets = _day(buy=buy, house=0.5, battery=[0.0] * 24, soc=[10.0] * 25, ran=ran)
    for bucket in buckets:
        bucket.ran_export_action, bucket.ran_export_kw = bucket.ran_action, bucket.ran_kw
        hour = (bucket.start - MIDNIGHT).total_seconds() // 3600
        if hour >= 18:
            bucket.ran_export_action = ACTION_EXPORT_DISCHARGE
    review = review_day(date(2026, 11, 13), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1))
    assert review is not None and review.plan_export is not None
    assert review.export_saving is not None and review.export_saving > review.saving
    totals = review_totals([review, review])
    assert totals["days"] == 2 and totals["export_days"] == 2
    assert totals["saving"] == pytest.approx(2 * review.saving, abs=0.02)
    assert totals["export_saving"] == pytest.approx(2 * review.export_saving, abs=0.02)
    assert totals["actual_saving"] == pytest.approx(0.0, abs=0.02)
    assert DayReview.from_dict(review.as_dict()).as_dict() == review.as_dict()
    assert review_totals([])["days"] == 0


def test_a_day_where_the_export_variant_did_not_run_has_no_export_figure() -> None:
    ran = [(ACTION_SELF_CONSUME, 0.0)] * 24
    buckets = _day(buy=[100.0] * 24, house=1.0, battery=[0.0] * 24, soc=[50.0] * 25, ran=ran)
    buckets[10].ran_export_action = ACTION_SELF_CONSUME
    review = review_day(date(2026, 11, 13), buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1))
    assert review is not None and review.plan_export is None


def test_a_sell_credit_is_added_to_every_exported_kwh() -> None:
    rows = [(MIDNIGHT, MIDNIGHT + timedelta(hours=1), 0.50)]
    assert price_slots(rows, FiscalChoice())[0].sell == pytest.approx(50.0)
    assert price_slots(rows, FiscalChoice(), sell_credit=4.0)[0].sell == pytest.approx(54.0)
