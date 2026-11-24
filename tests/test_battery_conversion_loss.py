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
