"""The day against a perfect plan in hindsight, the battery's work and its cells' temperature (`battery/hindsight.py`).
Pure: no Home Assistant. Prices in öre per kWh, power in kW, energy in kWh."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.hindsight import (
    BASIS_AHEAD,
    BASIS_DAY,
    HOT_C,
    OWN,
    SELF_CONSUMPTION,
    CellTemperature,
    Facit,
    cell_temperature,
    end_value_ahead,
    facit_day,
    house_profile,
    share_of,
    work_of,
)
from custom_components.spotnav.battery.plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_GRID_CHARGE,
    ACTION_SELF_CONSUME,
    MODE_BALANCED,
    MODE_MAX_PROFIT,
    BatteryParams,
)
from custom_components.spotnav.battery.plan_shadow import BUCKET, Bucket, DayReview, PriceSlot, review_day
from custom_components.spotnav.battery.review_baselines import no_battery_cost

UTC = timezone.utc
TZ = "Europe/Stockholm"
MIDNIGHT = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
DAY = date(2026, 11, 13)
BATTERY = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=10, max_soc_pct=100)
CHEAP_NIGHT = [20.0] * 6 + [300.0] * 18
WEAR = 15.0


def _day(*, battery: list[float], soc: list[float], ran: list[tuple[str, float]] | None = None, house: float = 1.0) -> list[Bucket]:
    """One local day of quarter hours, one figure per hour; `ran` is Balanced's action per hour planned beside it."""
    out = []
    for index in range(96):
        hour = index // 4
        grid = house + battery[hour]
        bucket = Bucket(MIDNIGHT + BUCKET * index, buy=CHEAP_NIGHT[hour], sell=CHEAP_NIGHT[hour] - 60, covered_s=900)
        bucket.import_kwh = max(0.0, grid) * 0.25
        bucket.export_kwh = max(0.0, -grid) * 0.25
        bucket.battery_kwh = battery[hour] * 0.25
        bucket.soc_start = soc[hour]
        bucket.soc_end = soc[hour + 1] if index % 4 == 3 else soc[hour]
        if ran is not None:
            bucket.ran_action, bucket.ran_kw = ran[hour]
            bucket.ran_modes = {MODE_BALANCED: ran[hour], MODE_MAX_PROFIT: ran[hour]}
        out.append(bucket)
    return out


def _ahead(buy: list[float], start: datetime) -> list[PriceSlot]:
    return [PriceSlot(start + timedelta(hours=h), start + timedelta(hours=h + 1), price, price - 60) for h, price in enumerate(buy)]


def _facit(buckets: list[Bucket], end_value: float, basis: str = BASIS_AHEAD) -> Facit:
    review = review_day(DAY, buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1), [MODE_BALANCED, MODE_MAX_PROFIT], MODE_BALANCED)
    assert review is not None
    made = facit_day(
        buckets,
        BATTERY,
        start_soc_pct=review.start_soc_pct,
        actual_cost=review.actual.cost,
        actual_end_soc_pct=review.actual.end_soc_pct,
        mode=review.mode,
        wear=WEAR,
        end_value=end_value,
        basis=basis,
        no_battery_cost=no_battery_cost(buckets),
        now=MIDNIGHT + timedelta(days=1, hours=1),
    )
    assert made is not None
    return made


# ------------------------------------------------------------------------------------------------ the end value


def test_the_energy_left_is_worth_the_dearest_hours_a_full_battery_covers_less_the_round_trip() -> None:
    end = MIDNIGHT + timedelta(days=1)
    # 48 h: one dear hour at 500, the rest at 100. The house draws 1 kW all day.
    buy = [100.0] * 48
    buy[18] = 500.0
    profile = {index: 1.0 for index in range(96)}
    value = end_value_ahead(_ahead(buy, end), end, BATTERY, profile)
    # A full battery gives 9 kWh x the one-way efficiency; the house takes 1 kWh an hour: the dear hour and the
    # rest at 100.
    ac = 9 * BATTERY.one_way_efficiency
    assert value == pytest.approx((500 * 1 + 100 * (ac - 1)) / ac * BATTERY.round_trip_efficiency)


def test_the_house_bounds_what_a_dear_hour_can_take() -> None:
    end = MIDNIGHT + timedelta(days=1)
    buy = [100.0] * 48
    buy[18] = 500.0
    # An idle house: each quarter hour can take nothing, so no hour is worth more than another: the median.
    value = end_value_ahead(_ahead(buy, end), end, BATTERY, {index: 0.0 for index in range(96)})
    assert value == pytest.approx(100 * BATTERY.round_trip_efficiency)
    # No figure for the time of day: the battery's rated discharge (5 kW) takes the whole dear hour.
    full = end_value_ahead(_ahead(buy, end), end, BATTERY, {})
    ac = 9 * BATTERY.one_way_efficiency
    assert full == pytest.approx((500 * 5 + 100 * (ac - 5)) / ac * BATTERY.round_trip_efficiency)


def test_without_the_next_two_days_prices_there_is_no_value_yet() -> None:
    end = MIDNIGHT + timedelta(days=1)
    assert end_value_ahead(_ahead([100.0] * 24, end), end, BATTERY, {}) is None
    assert end_value_ahead([], end, BATTERY, {}) is None


def test_the_house_profile_is_by_quarter_hour_of_the_day() -> None:
    buckets = _day(battery=[0.0] * 24, soc=[10.0] * 25, house=2.0)
    profile = house_profile(buckets, MIDNIGHT)
    assert len(profile) == 96 and set(profile.values()) == {2.0}


# ------------------------------------------------------------------------------------------------ the facit


def test_an_idle_battery_gets_none_of_what_the_perfect_plan_saves() -> None:
    # The battery stayed at its floor; Balanced was planned beside it to charge in the 6 cheap hours.
    ran = [(ACTION_GRID_CHARGE, 1.5)] * 6 + [(ACTION_DISCHARGE_HOUSE, 0.0)] * 18
    buckets = _day(battery=[0.0] * 24, soc=[10.0] * 25, ran=ran)
    made = _facit(buckets, end_value=100.0)
    perfect = made.saving
    assert perfect is not None and perfect > 1000
    own = made.candidates[OWN]
    # Nothing done: no saving against no battery, so 0 % (not clamped: it is exactly nothing).
    assert own.saving == pytest.approx(0.0, abs=1e-6) and own.share == pytest.approx(0.0) and not own.clamped
    assert own.work.cycles == 0 and own.work.switches == 0
    # Self-consumption from an empty battery and no sun does nothing either.
    assert made.candidates[SELF_CONSUMPTION].share == pytest.approx(0.0)
    balanced = made.candidates[MODE_BALANCED]
    assert 0 < balanced.share <= 100 and balanced.perfect == pytest.approx(made.perfect[MODE_BALANCED].saving)
    # Its perfect plan charged from the grid in the night and gave it back in the day.
    work = made.perfect[MODE_BALANCED].work
    assert work.grid_charge_kwh > 5 and work.switches == 1 and work.cycles == pytest.approx(work.discharged_kwh / 10)
    assert made.mode == MODE_BALANCED and made.basis == BASIS_AHEAD


def test_energy_left_at_midnight_counts_for_every_way_at_the_same_value() -> None:
    # The battery charged 6 cheap hours and kept it all: the energy left is worth what the next days pay.
    battery = [1.5] * 6 + [0.0] * 18
    soc = [10.0]
    for hour in range(24):
        soc.append(soc[-1] + battery[hour] * 10)
    buckets = _day(battery=battery, soc=soc)
    low, high = _facit(buckets, end_value=10.0), _facit(buckets, end_value=400.0)
    # Kept at a low value it is a loss; at a high one a gain, by the energy times the difference.
    gained = (soc[-1] - soc[0]) / 100 * 10 * (400.0 - 10.0)
    assert high.candidates[OWN].saving - low.candidates[OWN].saving == pytest.approx(gained)
    # The perfect plan fills the battery for a dear tomorrow as well; the fake readings stored every kWh without the
    # model's loss on the way in, so what happened beats it by a little: clamped at 100 %, and said to be.
    assert high.saving > low.saving
    own = high.candidates[OWN]
    assert own.saving > high.saving and own.share == 100.0 and own.clamped
    assert low.basis == BASIS_AHEAD


def test_a_way_above_the_perfect_plan_or_below_no_battery_is_clamped_and_says_so() -> None:
    assert share_of(80.0, 100.0) == (pytest.approx(80.0), False)
    assert share_of(103.0, 100.0) == (100.0, True)
    assert share_of(-20.0, 100.0) == (0.0, True)
    assert share_of(100.04, 100.0) == (100.0, False)  # rounding, not worth a word
    assert share_of(50.0, 0.0) == (None, False)  # nothing to gain


def test_the_facit_survives_the_store_beside_its_review() -> None:
    ran = [(ACTION_GRID_CHARGE, 1.5)] * 6 + [(ACTION_SELF_CONSUME, 0.0)] * 18
    buckets = _day(battery=[0.0] * 24, soc=[10.0] * 25, ran=ran)
    review = review_day(DAY, buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1), [MODE_BALANCED], MODE_BALANCED)
    assert review is not None and review.facit is None and review.as_dict()["facit"] is None
    made = _facit(buckets, end_value=250.0, basis=BASIS_DAY)
    temperature = CellTemperature(21.0, 31.5, 15, 1440)
    from dataclasses import replace

    stored = replace(review, facit=made, cell_temperature=temperature).as_dict()
    again = DayReview.from_dict(stored)
    assert again is not None and again.as_dict() == stored
    assert again.facit is not None and again.facit.basis == BASIS_DAY
    # A broken facit is dropped, the review kept.
    broken = dict(stored, facit={"basis": "guess"}, cell_temperature="hot")
    kept = DayReview.from_dict(broken)
    assert kept is not None and kept.facit is None and kept.cell_temperature is None
    assert Facit.from_dict(None) is None


# ------------------------------------------------------------------------------------------------ work


def test_work_counts_cycles_switches_and_where_the_energy_came_from_and_went() -> None:
    steps = [
        (3.0, 4.0, 1.0),  # charging 3 kW while importing 4: all from the grid
        (0.1, 0.0, 1.0),  # idle: no switch
        (2.0, -1.0, 1.0),  # charging from the sun (exporting the rest)
        (-4.0, -1.0, 1.0),  # discharging 4, exporting 1: 1 kWh to the grid
        (-1.0, 0.5, 1.0),
        (1.0, 1.0, 1.0),
    ]
    work = work_of(steps, usable_kwh=10.0)
    assert work.discharged_kwh == pytest.approx(5.0) and work.cycles == pytest.approx(0.5)
    assert work.switches == 2
    assert work.grid_charge_kwh == pytest.approx(3.0 + 0.0 + 1.0)
    assert work.to_grid_kwh == pytest.approx(1.0)


# ------------------------------------------------------------------------------------------------ cell temperature


def test_the_days_cell_temperature_from_five_minute_statistics() -> None:
    start = MIDNIGHT
    rows = [(start + timedelta(minutes=5 * i), 20.0, 21.0) for i in range(280)]
    rows += [(start + timedelta(minutes=5 * (280 + i)), 31.0, 33.5) for i in range(8)]
    rows.append((start, None, None))
    made = cell_temperature(rows, 5)
    assert made is not None
    assert made.max_c == 33.5 and made.minutes_above == 40 and made.minutes == 288 * 5
    assert made.mean_c == pytest.approx((280 * 20 + 8 * 31) / 288)
    assert HOT_C == 30.0
    assert cell_temperature([], 5) is None
    assert CellTemperature.from_dict(made.as_dict()) == CellTemperature(round(made.mean_c, 1), 33.5, 40, 1440)
