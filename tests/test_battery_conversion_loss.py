"""The inverter's loss on the battery's way in and out, and the battery counted on the AC side (`conversion_loss.py`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.conversion_loss import (
    DEFAULT_CHARGE_LOSS,
    DEFAULT_DISCHARGE_LOSS,
    ConversionLoss,
    ac_kw,
    ac_round_trip,
)
from custom_components.spotnav.battery.plan_shadow import Bucket, PlanActuals
from custom_components.spotnav.battery.review_baselines import no_battery_cost
from custom_components.spotnav.battery.round_trip import DC_CAP, PLAN_CAP, PLAUSIBLE, RoundTrip

T0 = datetime(2026, 11, 20, tzinfo=timezone.utc)


def test_a_dc_reading_is_turned_to_the_ac_side() -> None:
    assert ac_kw(10.0, (0.03, 0.05)) == pytest.approx(10.3)
    assert ac_kw(-10.0, (0.03, 0.05)) == pytest.approx(-9.5)
    assert ac_kw(0.0, (0.03, 0.05)) == 0.0
    # A charge from the grid at the way-in loss; the sun's over the DC bus at the way-out loss (it never crossed the
    # inverter, but would have gone out through it without the battery).
    assert ac_kw(5.0, (0.03, 0.05), grid_kw=6.0) == pytest.approx(5.15)
    assert ac_kw(5.0, (0.03, 0.05), grid_kw=-2.0) == pytest.approx(4.75)
    assert ac_kw(5.0, (0.03, 0.05), grid_kw=2.0) == pytest.approx(2 * 1.03 + 3 * 0.95)
    assert ac_round_trip(0.96, (0.03, 0.05)) == pytest.approx(0.96 * 0.95 / 1.03)


def test_the_loss_is_assumed_until_learnt_and_none_on_the_ac_side() -> None:
    learner = ConversionLoss()
    assert learner.loss(True) == (DEFAULT_CHARGE_LOSS, DEFAULT_DISCHARGE_LOSS) and learner.source(True) == "assumed"
    assert learner.loss(False) == (0.0, 0.0) and learner.source(False) == "ac"


def _feed(learner: ConversionLoss, quarters: list[tuple[float, float]], charge: float, discharge: float) -> None:
    """Quarter hours of `(battery DC kW, sun kW)`, a reading a minute; the plant's load leaves the loss out."""
    house = 1.0
    for index, (battery, sun) in enumerate(quarters):
        loss = charge * max(0.0, battery) + discharge * max(0.0, -battery)
        grid = house + battery + loss + 0.15 - sun
        for minute in range(15):
            learner.observe(T0 + timedelta(minutes=15 * index + minute), battery, grid, sun, house)
    learner.observe(T0 + timedelta(minutes=15 * len(quarters)), None, None, None, None)


def test_the_loss_is_learnt_from_dark_quarter_hours() -> None:
    learner = ConversionLoss()
    pattern = [(5.0, 0.0)] * 4 + [(-3.0, 0.0)] * 6 + [(0.0, 0.0)] * 2 + [(-2.0, 3.0)] * 4
    _feed(learner, pattern * 16, 0.04, 0.07)
    assert learner.source(True) == "learned"
    charge, discharge = learner.loss(True)
    assert charge == pytest.approx(0.04, abs=0.002) and discharge == pytest.approx(0.07, abs=0.002)


def test_too_few_quarter_hours_teach_nothing() -> None:
    learner = ConversionLoss()
    _feed(learner, [(5.0, 0.0)] * 10 + [(-3.0, 0.0)] * 10, 0.04, 0.07)
    assert learner.loss(True) == (DEFAULT_CHARGE_LOSS, DEFAULT_DISCHARGE_LOSS)


def test_the_house_and_the_day_with_no_battery_count_the_battery_on_the_ac_side() -> None:
    actuals = PlanActuals()
    bucket = actuals.open(T0, price=None, cap_kw=None, ran=None)
    bucket.buy, bucket.sell = 100.0, 50.0
    for second in range(0, 901, 60):
        # The grid as the meter sees it: the house's 1 kW and the battery's 5 kW DC charge plus 3 % loss.
        actuals.observe(T0 + timedelta(seconds=second), 1.0 + 5.15, 0.0, 5.0, 50.0, ac_kw(5.0, (0.03, 0.05)))
    assert bucket.battery_kwh == pytest.approx(5.0 / 4)
    assert bucket.battery_ac == pytest.approx(5.15 / 4)
    assert bucket.net_house_kw == pytest.approx(1.0)
    assert no_battery_cost([bucket]) == pytest.approx(1.0 / 4 * 100.0)
    # A quarter hour from before the AC side counts as read until it is turned.
    old = Bucket(T0, buy=100.0, sell=50.0, import_kwh=6.15 / 4, battery_kwh=5.0 / 4, covered_s=900.0)
    assert old.net_house_kw == pytest.approx(1.15)
    old.fill_ac((0.03, 0.05))
    assert old.net_house_kw == pytest.approx(1.0)


def test_a_dc_round_trip_is_capped_only_at_the_believable_top() -> None:
    learner = RoundTrip()
    assert learner.cap == PLAN_CAP
    learner.efficiency = 0.95
    learner.set_cap(DC_CAP)
    assert DC_CAP == PLAUSIBLE[1] and learner.cap == DC_CAP
    assert RoundTrip.from_dict({**learner.as_dict(), "efficiency": 0.97}).efficiency == 0.97
    assert RoundTrip.from_dict({"efficiency": 0.97}).efficiency == PLAN_CAP


def test_a_review_from_before_the_ac_side_is_reviewed_again_while_its_quarter_hours_are_kept() -> None:
    from dataclasses import replace
    from datetime import date
    from types import SimpleNamespace

    from custom_components.spotnav.battery.facts import BatteryFacts
    from custom_components.spotnav.battery.learned import LearnedBattery
    from custom_components.spotnav.battery.plan_model import BatteryParams
    from custom_components.spotnav.battery.plan_runtime import BatteryPlanShadow
    from custom_components.spotnav.battery.plan_shadow import BATTERY_AC_MEASURED, review_day

    midnight = datetime(2026, 11, 12, 23, 0, tzinfo=timezone.utc)
    day = date(2026, 11, 13)
    battery = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=10, max_soc_pct=100)
    actuals = PlanActuals()
    for index in range(96):
        charge = 4.0 if index < 24 else -2.0 if index < 72 else 0.0
        actuals.buckets[midnight + timedelta(minutes=15 * index)] = Bucket(
            midnight + timedelta(minutes=15 * index), buy=100.0, sell=50.0, import_kwh=max(0.0, 1.0 + charge) / 4,
            battery_kwh=charge / 4, covered_s=900.0, soc_start=50.0, soc_end=50.0,
        )
    old = review_day(day, actuals.buckets.values(), battery, "Europe/Stockholm", midnight + timedelta(days=1))
    assert old is not None
    old = replace(old, battery_ac=None)
    shadow = object.__new__(BatteryPlanShadow)
    shadow.actuals, shadow.reviews, shadow.learned = actuals, [old], LearnedBattery()
    shadow.site = SimpleNamespace(demand=SimpleNamespace(tracker=SimpleNamespace(tz="Europe/Stockholm")))
    shadow.hass = None
    shadow._facts = BatteryFacts(battery_entity_id="sensor.battery", power_dc=True)
    shadow._review_again(midnight + timedelta(days=1, hours=2), battery)
    again = shadow.reviews[0]
    assert again.battery_ac == BATTERY_AC_MEASURED
    # The loss left the house: the day with no battery is cheaper, so the battery saved less against it.
    assert again.no_battery_cost is not None and old.no_battery_cost is not None
    assert again.no_battery_cost < old.no_battery_cost
    assert again.actual.cost == pytest.approx(old.actual.cost)
    # On the AC side as read (not marked DC): nothing is reviewed again.
    shadow.reviews = [old]
    shadow._facts = BatteryFacts(battery_entity_id="sensor.battery", power_dc=False)
    shadow._review_again(midnight + timedelta(days=1, hours=2), battery)
    assert shadow.reviews == [old]


async def test_an_older_review_is_rebuilt_from_the_statistics_once() -> None:
    from dataclasses import replace
    from datetime import date
    from types import SimpleNamespace

    from custom_components.spotnav.battery.facts import BatteryFacts
    from custom_components.spotnav.battery.learned import LearnedBattery
    from custom_components.spotnav.battery.plan_runtime import BatteryPlanShadow
    from custom_components.spotnav.battery.plan_shadow import BATTERY_AC_STATISTICS, PriceSlot, review_day

    midnight = datetime(2026, 11, 12, 23, 0, tzinfo=timezone.utc)
    day = date(2026, 11, 13)
    facts = BatteryFacts(
        battery_entity_id="sensor.battery", power_dc=True, soc_pct=50.0, capacity_kwh=10.0, rated_charge_kw=5.0,
        rated_discharge_kw=5.0, reserve_soc_pct=10.0,
    )
    buckets = []
    grid: dict[datetime, float] = {}
    power: dict[datetime, float] = {}
    for index in range(96):
        start = midnight + timedelta(minutes=15 * index)
        charge = 4.0 if index < 24 else -2.0 if index < 72 else 0.0
        buckets.append(Bucket(start, buy=100.0 if index < 24 else 300.0, sell=50.0, import_kwh=max(0.0, 1.0 + charge) / 4,
                              battery_kwh=charge / 4, covered_s=900.0, soc_start=50.0, soc_end=50.0))
        for five in range(3):
            grid[start + timedelta(minutes=5 * five)] = max(0.0, 1.0 + charge)
            power[start + timedelta(minutes=5 * five)] = charge
    from custom_components.spotnav.battery.plan_shadow import battery_params

    params, _reason, _estimates = battery_params(facts)
    assert params is not None
    old = replace(review_day(day, buckets, params, "Europe/Stockholm", midnight + timedelta(days=1)), battery_ac=None)  # type: ignore[arg-type]
    prices = [PriceSlot(b.start, b.end, b.buy, b.sell) for b in buckets]  # type: ignore[arg-type]
    after = [replace(p, start=p.start + timedelta(days=1), end=p.end + timedelta(days=1)) for p in prices]

    async def job(func, *args):  # noqa: ANN001, ANN202
        return func(*args)

    shadow = object.__new__(BatteryPlanShadow)
    shadow.reviews, shadow.learned, shadow._facts, shadow._cap_kw = [old], LearnedBattery(), facts, None
    shadow.site = SimpleNamespace(demand=SimpleNamespace(tracker=SimpleNamespace(tz="Europe/Stockholm")))
    shadow.hass = SimpleNamespace(async_add_executor_job=job)
    shadow._rebuild_running, shadow._rebuild_tried, shadow._closed = False, set(), False
    shadow._wear = lambda: (15.0, False)
    shadow._sell_margin = lambda: 0.0
    shadow._save = lambda: None
    shadow._prices_after = lambda d: prices if d == day - timedelta(days=1) else after

    async def read(_start: datetime, _end: datetime):  # noqa: ANN202
        return grid, power, timedelta(minutes=5)

    shadow._async_read_day_power = read
    await shadow._async_rebuild(day, midnight + timedelta(days=3))
    made = shadow.reviews[0]
    assert made.battery_ac == BATTERY_AC_STATISTICS and made.facit is not None
    assert made.battery_saving is not None and old.battery_saving is not None
    assert made.battery_saving < old.battery_saving
    assert made.saving == pytest.approx(old.saving)
    assert not shadow._rebuild_running


def test_a_summer_midday_charged_by_the_sun_does_not_lower_the_house() -> None:
    """The sun charges the battery 5 kW over the DC bus while 1 kW goes out: the house's 1 kW must not read lower for
    a loss on the way in the sun's charge never had."""
    actuals = PlanActuals()
    bucket = actuals.open(T0, price=None, cap_kw=None, ran=None)
    loss = (0.03, 0.05)
    sun_ac = 7.0  # the sun's AC worth: 5 kW into the battery (x 0.95) and the rest to the house and the grid
    for second in range(0, 901, 60):
        grid = 1.0 + 5.0 * 0.95 - sun_ac
        actuals.observe(T0 + timedelta(seconds=second), grid, 0.0, 5.0, 50.0, ac_kw(5.0, loss, grid))
    assert bucket.export_kwh > 0
    assert (bucket.net_house_kw or 0.0) + sun_ac == pytest.approx(1.0)


def test_a_fit_outside_the_believable_loss_is_not_used_and_the_round_trip_has_a_floor() -> None:
    from dataclasses import replace

    from custom_components.spotnav.battery.conversion_loss import LOSS_RANGE
    from custom_components.spotnav.battery.facts import BatteryFacts
    from custom_components.spotnav.battery.learned import AC_ROUND_TRIP_FLOOR, LearnedBattery, with_learned

    assert LOSS_RANGE[1] <= 0.08
    learner = ConversionLoss()
    pattern = [(5.0, 0.0)] * 4 + [(-3.0, 0.0)] * 6 + [(0.0, 0.0)] * 2
    _feed(learner, pattern * 16, 0.12, 0.12)  # a cell heater on a cold night's charge, say
    assert learner.source(True) == "assumed"
    # A loss at the edge still leaves the plan's round trip above the floor; one forced past it is held there.
    learned = LearnedBattery()
    learned.round_trip.efficiency = 0.96
    learned.conversion.charge_loss, learned.conversion.discharge_loss = 0.08, 0.08
    facts = BatteryFacts(battery_entity_id="sensor.battery", power_dc=True)
    assert with_learned(facts, learned).round_trip_efficiency == round(0.96 * 0.92 / 1.08, 3)
    learned.conversion.charge_loss, learned.conversion.discharge_loss = 0.15, 0.15
    assert with_learned(facts, learned).round_trip_efficiency == AC_ROUND_TRIP_FLOOR
    # A battery lossy in itself is not raised by it: at worst it is held where the assumed loss puts it.
    learned.round_trip.efficiency = 0.75
    assert with_learned(replace(facts), learned).round_trip_efficiency == round(0.75 * 0.95 / 1.03, 3)
