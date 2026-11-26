"""The battery counted on the AC side, on a real November (`tests/fixtures/battery/sigen_november_quarters.json`).

A Sigenergy plant whose battery power is read at the battery (DC) and whose battery ran on its own AI mode. Counted as
read, the inverter's loss sat in the house and the battery's own way came out above its perfect plan in hindsight
(`plans/research_integrations/sigen_ai_above_perfect_2026-11-24.md`). Counted on the AC side, with the loss learnt from
the plant's own load, it must come out below on (nearly) every whole day and at about 77-88 % of it overall.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.spotnav.battery.conversion_loss import ConversionLoss, ac_kw, ac_round_trip, sun_round_trip
from custom_components.spotnav.battery.hindsight import OWN, end_value_next_day, facit_day
from custom_components.spotnav.battery.plan_model import BatteryParams
from custom_components.spotnav.battery.plan_shadow import Bucket, PriceSlot
from custom_components.spotnav.battery.review_baselines import no_battery_cost

FIXTURE = Path(__file__).parent / "fixtures" / "battery" / "sigen_november_quarters.json"
TZ = ZoneInfo("Europe/Stockholm")
Q = timedelta(minutes=15)
#: The plant: 40.3 kWh usable from 0 %, 13.2 kW both ways, a 20 A fuse; the round trip its own counters showed (DC).
USABLE_KWH, POWER_KW, CAP_KW, DC_ROUND_TRIP = 40.3, 13.2, 13.856, 0.967
DAYS = [date(2026, 11, day) for day in range(16, 24)]


def _buy(spot: float) -> float:
    return (spot * 100 * 1.05 + 56) * 1.25


def _sell(spot: float) -> float:
    return spot * 100 + 6.6


@pytest.fixture(scope="module")
def data() -> dict:
    raw = json.loads(FIXTURE.read_text())
    return {"t0": datetime.fromisoformat(raw["t0"]), "rows": raw["rows"]}


def _learnt(data: dict) -> ConversionLoss:
    """The loss as the shell learns it: a reading a minute, each quarter hour's means held."""
    learner = ConversionLoss()
    for index, (grid, battery, _car, pv, load, _soc, _spot) in enumerate(data["rows"]):
        start = data["t0"] + index * Q
        for minute in range(15):
            learner.observe(start + timedelta(minutes=minute), battery, grid, pv, load)
    learner.observe(data["t0"] + len(data["rows"]) * Q, None, None, None, None)
    return learner


def _bounds(day: date) -> tuple[datetime, datetime]:
    after = day + timedelta(days=1)
    return (
        datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(timezone.utc),
        datetime(after.year, after.month, after.day, tzinfo=TZ).astimezone(timezone.utc),
    )


def _day(data: dict, day: date, loss: tuple[float, float]) -> tuple[list[Bucket], list[float]]:
    start, end = _bounds(day)
    first = int((start - data["t0"]) / Q)
    buckets, socs = [], []
    for index in range(first, first + int((end - start) / Q)):
        grid, battery, car, _pv, _load, soc, spot = data["rows"][index]
        if soc is not None:
            socs.append(soc)
        if grid is None or battery is None or spot is None:
            continue
        buckets.append(
            Bucket(
                start=data["t0"] + index * Q,
                buy=_buy(spot),
                sell=_sell(spot),
                cap_kw=CAP_KW,
                import_kwh=max(0.0, grid) / 4,
                export_kwh=max(0.0, -grid) / 4,
                car_kwh=(car or 0.0) / 4,
                battery_kwh=battery / 4,
                battery_ac_kwh=ac_kw(battery, loss, grid) / 4,
                covered_s=900.0,
            )
        )
    return buckets, socs


def _around(data: dict, day: date) -> list[PriceSlot]:
    """The prices the value of the energy left at [day]'s end reads: the day after, and before it the day and the one
    before (`plan_runtime._prices_around`)."""
    return [slot for offset in (-1, 0, 1) for slot in _prices(data, day + timedelta(days=offset))]


def _prices(data: dict, day: date) -> list[PriceSlot]:
    start, end = _bounds(day)
    first = int((start - data["t0"]) / Q)
    out = []
    for index in range(first, min(len(data["rows"]), first + int((end - start) / Q))):
        spot = data["rows"][index][6]
        if spot is not None:
            moment = data["t0"] + index * Q
            out.append(PriceSlot(moment, moment + Q, _buy(spot), _sell(spot)))
    return out


def _month(
    data: dict, loss: tuple[float, float], round_trip: float, sun_round_trip: float | None = None
) -> dict[date, tuple[float, float]]:
    """Per day `(own's saving, the yardstick's)` against no battery (minor units)."""
    battery = BatteryParams(
        USABLE_KWH,
        POWER_KW,
        POWER_KW,
        min_soc_pct=0.0,
        round_trip_efficiency=round_trip,
        sun_round_trip_efficiency=sun_round_trip,
        discharge_loss=0.0 if sun_round_trip is None else loss[1],
    )
    out = {}
    for day in DAYS:
        buckets, socs = _day(data, day, loss)
        _start, end = _bounds(day)
        _end, next_end = _bounds(day + timedelta(days=1))
        value = end_value_next_day(_around(data, day), end, next_end, battery)
        assert value is not None
        facit = facit_day(
            buckets,
            battery,
            start_soc_pct=socs[0],
            actual_cost=sum(b.import_kwh * b.buy - b.export_kwh * b.sell for b in buckets),  # type: ignore[operator]
            actual_end_soc_pct=socs[-1],
            mode="max_profit",
            wear=15.0,
            end_value=value,
            basis="next_day",
            no_battery_cost=no_battery_cost(buckets),
            day_quarters=96,
            now=end,
        )
        assert facit is not None and facit.yardstick is not None
        out[day] = (facit.candidates[OWN].saving, facit.yardstick.saving)
    return out


def test_the_loss_is_learnt_from_the_plants_own_load(data: dict) -> None:
    learner = _learnt(data)
    charge, discharge = learner.loss(True)
    assert learner.source(True) == "learned"
    assert charge == pytest.approx(0.036, abs=0.01)
    assert discharge == pytest.approx(0.066, abs=0.015)
    # The fit's constant: the inverter's standby, about 180 W on these nights.
    assert learner.idle(True) == pytest.approx(0.18, abs=0.04)
    assert learner.loss(False) == (0.0, 0.0)
    again = ConversionLoss.from_dict(json.loads(json.dumps(learner.as_dict())))
    assert again.loss(True) == learner.loss(True) and again.idle(True) == learner.idle(True)


def test_counted_as_read_the_battery_beats_its_perfect_plan(data: dict) -> None:
    """The bias the AC side removes: the loss in the house, the assumed 90 % round trip."""
    days = _month(data, (0.0, 0.0), 0.90)
    own, perfect = sum(v[0] for v in days.values()), sum(v[1] for v in days.values())
    assert own > perfect


def test_on_the_ac_side_the_battery_stays_below_its_perfect_plan(data: dict) -> None:
    loss = _learnt(data).loss(True)
    round_trip = ac_round_trip(DC_ROUND_TRIP, loss)
    assert round_trip == pytest.approx(0.87, abs=0.015)
    days = _month(data, loss, round_trip)
    below = [day for day, (own, perfect) in days.items() if own < perfect]
    assert len(below) >= 7
    # The one day allowed above is only just above (within the loss model's few kronor).
    assert all(own - perfect < 500 for own, perfect in days.values())
    share = sum(v[0] for v in days.values()) / sum(v[1] for v in days.values())
    assert 0.77 <= share <= 0.92


def test_a_stored_review_from_before_the_ac_side_is_rebuilt_from_the_statistics(data: dict) -> None:
    """A day reviewed as read (DC) and stored; rebuilt from the grid's and the battery's five-minute means."""
    from custom_components.spotnav.battery.plan_shadow import BATTERY_AC_STATISTICS, review_day
    from custom_components.spotnav.battery.review_rebuild import FIVE_MIN, rebuilt_quarters, rebuilt_review

    day = date(2026, 11, 18)
    loss = _learnt(data).loss(True)
    battery_dc = BatteryParams(USABLE_KWH, POWER_KW, POWER_KW, min_soc_pct=0.0, round_trip_efficiency=0.90)
    stored_buckets, _socs = _day(data, day, (0.0, 0.0))
    for bucket in stored_buckets:
        bucket.battery_ac_kwh = None  # a quarter hour from before the AC side
        bucket.soc_start = bucket.soc_end = data["rows"][int((bucket.start - data["t0"]) / Q)][5]
    start, end = _bounds(day)
    _end, next_end = _bounds(day + timedelta(days=1))
    old = review_day(day, stored_buckets, battery_dc, "Europe/Stockholm", end, (), "max_profit")
    assert old is not None
    value = end_value_next_day(_around(data, day), end, next_end, battery_dc)
    old_facit = facit_day(
        stored_buckets, battery_dc, start_soc_pct=old.start_soc_pct, actual_cost=old.actual.cost,
        actual_end_soc_pct=old.actual.end_soc_pct, mode="max_profit", wear=15.0, end_value=value,  # type: ignore[arg-type]
        basis="next_day", no_battery_cost=old.no_battery_cost or 0.0, day_quarters=96, now=end,
    )
    from dataclasses import replace

    old = replace(old, facit=old_facit, battery_ac=None)

    grid: dict[datetime, float] = {}
    battery: dict[datetime, float] = {}
    for bucket in stored_buckets:
        for index in range(3):
            moment = bucket.start + index * FIVE_MIN
            grid[moment] = (bucket.import_kwh - bucket.export_kwh) * 4
            battery[moment] = bucket.battery_kwh * 4
    quarters, actual, loss_cost = rebuilt_quarters(start, end, grid, battery, FIVE_MIN, _prices(data, day), CAP_KW, loss)
    assert len(quarters) == len(stored_buckets) and actual == pytest.approx(old.actual.cost, rel=1e-6)
    battery_ac = replace(battery_dc, round_trip_efficiency=ac_round_trip(DC_ROUND_TRIP, loss))
    made = rebuilt_review(
        old, quarters, actual, loss_cost, battery_ac, _prices(data, day + timedelta(days=1)), end, next_end, 96, 15.0, end
    )
    assert made is not None and made.battery_ac == BATTERY_AC_STATISTICS
    assert made.facit is not None and old.facit is not None
    before = old.facit.candidates[OWN].saving - old.facit.yardstick.saving  # type: ignore[union-attr]
    after = made.facit.candidates[OWN].saving - made.facit.yardstick.saving  # type: ignore[union-attr]
    assert after < before and after < 0
    # The battery's own way against no battery and against self-consumption fall by the loss's cost; the plan's
    # figure against self-consumption stays.
    assert loss_cost > 100
    assert (old.battery_saving or 0.0) - (made.battery_saving or 0.0) == pytest.approx(loss_cost)
    assert made.actual_saving == pytest.approx(old.actual_saving - loss_cost)
    assert made.saving == pytest.approx(old.saving)
    # Its own work is kept: the rebuilt quarter hours do not hold the battery.
    assert made.facit.candidates[OWN].work == old.facit.candidates[OWN].work


def test_the_rebuilt_loss_does_not_hang_on_the_quarter_hours_the_review_covered(data: dict) -> None:
    """The loss's cost comes from the statistics alone: a review that missed a few quarter hours (a restart) gets the
    same loss, and none of the battery's saving over the quarter hours it missed; an hour's mean nets a charge and a
    discharge in it, so hourly statistics give about the same loss, never much more."""
    from custom_components.spotnav.battery.plan_shadow import review_day
    from custom_components.spotnav.battery.review_rebuild import FIVE_MIN, HOUR, rebuilt_quarters, rebuilt_review

    day = date(2026, 11, 19)
    loss = _learnt(data).loss(True)
    start, end = _bounds(day)
    _end, next_end = _bounds(day + timedelta(days=1))
    buckets, _socs = _day(data, day, (0.0, 0.0))
    for bucket in buckets:
        bucket.battery_ac_kwh = None
        bucket.soc_start = bucket.soc_end = data["rows"][int((bucket.start - data["t0"]) / Q)][5]
    battery = BatteryParams(USABLE_KWH, POWER_KW, POWER_KW, min_soc_pct=0.0, round_trip_efficiency=0.87)
    grid: dict[datetime, float] = {}
    power: dict[datetime, float] = {}
    hourly_grid: dict[datetime, list[float]] = {}
    hourly_power: dict[datetime, list[float]] = {}
    for bucket in buckets:
        g, b = (bucket.import_kwh - bucket.export_kwh) * 4, bucket.battery_kwh * 4
        for index in range(3):
            grid[bucket.start + index * FIVE_MIN], power[bucket.start + index * FIVE_MIN] = g, b
        hour = bucket.start.replace(minute=0)
        hourly_grid.setdefault(hour, []).append(g)
        hourly_power.setdefault(hour, []).append(b)
    prices = _prices(data, day)
    quarters, actual, loss_cost = rebuilt_quarters(start, end, grid, power, FIVE_MIN, prices, CAP_KW, loss)
    _q, _a, hourly_loss = rebuilt_quarters(
        start, end, {h: sum(v) / len(v) for h, v in hourly_grid.items()},
        {h: sum(v) / len(v) for h, v in hourly_power.items()}, HOUR, prices, CAP_KW, loss,
    )
    assert 0 < hourly_loss <= loss_cost * 1.02 and hourly_loss == pytest.approx(loss_cost, rel=0.1)
    after = _prices(data, day + timedelta(days=1))
    made_by: dict[int, float] = {}
    for missing in (0, 6):
        kept = buckets[:40] + buckets[40 + missing:]
        old = review_day(day, kept, battery, "Europe/Stockholm", end, (), "max_profit")
        assert old is not None
        made = rebuilt_review(old, quarters, actual, loss_cost, battery, after, end, next_end, 96, 15.0, end)
        assert made is not None
        made_by[missing] = (old.battery_saving or 0.0) - (made.battery_saving or 0.0)
    assert made_by[6] == pytest.approx(loss_cost) and made_by[0] == pytest.approx(loss_cost)
    # Far more quarter hours read than the review covered: not rebuilt.
    half = review_day(day, buckets[:48], battery, "Europe/Stockholm", end, (), "max_profit")
    assert half is not None
    assert rebuilt_review(half, quarters, actual, loss_cost, battery, after, end, next_end, 96, 15.0, end) is None


def test_the_suns_own_round_trip_barely_moves_a_november(data: dict) -> None:
    """November's sun seldom leaves a surplus: pricing the sun's charge at its own round trip (about the DC one) leaves
    the battery's own way where it was against its perfect plan."""
    loss = _learnt(data).loss(True)
    round_trip = ac_round_trip(DC_ROUND_TRIP, loss)
    sun = sun_round_trip(round_trip, loss, DC_ROUND_TRIP)
    assert sun == pytest.approx(DC_ROUND_TRIP, abs=0.005)
    before = _month(data, loss, round_trip)
    after = _month(data, loss, round_trip, sun)
    share = sum(v[0] for v in after.values()) / sum(v[1] for v in after.values())
    share_before = sum(v[0] for v in before.values()) / sum(v[1] for v in before.values())
    assert 0.77 <= share <= 0.92
    assert share == pytest.approx(share_before, abs=0.02)
    # The perfect plan moves by the cells' frame (a battery counted as its cells hold, `plan_model.BatteryParams`), a
    # kronor or so a day either way, not by the sun.
    assert all(abs(after[day][1] - before[day][1]) < 150 for day in DAYS)
