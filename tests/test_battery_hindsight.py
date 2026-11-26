"""The day against a perfect plan in hindsight, the battery's work and its cells' temperature (`battery/hindsight.py`).
Pure: no Home Assistant. Prices in öre per kWh, power in kW, energy in kWh."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.hindsight import (
    BASIS_DAY,
    BASIS_NEXT_DAY,
    HOT_C,
    MIN_PERFECT_SAVING,
    OWN,
    RULES_MODE,
    RULES_NO_SELL,
    RULES_SELL,
    SELF_CONSUMPTION,
    WHOLE_DAY_SHARE,
    CellTemperature,
    Facit,
    cell_temperature,
    end_value_next_day,
    facit_day,
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


def _facit(buckets: list[Bucket], end_value: float, basis: str = BASIS_NEXT_DAY) -> Facit:
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
        day_quarters=96,
        now=MIDNIGHT + timedelta(days=1, hours=1),
    )
    assert made is not None
    return made


# ------------------------------------------------------------------------------------------------ the end value


def test_the_energy_left_is_worth_the_next_days_mean_buy_price_less_the_round_trip() -> None:
    end = MIDNIGHT + timedelta(days=1)
    # The next day: one dear hour at 500, the rest at 100. Its mean, not its dearest hours: a price known at the time.
    buy = [100.0] * 24
    buy[18] = 500.0
    value = end_value_next_day(_ahead(buy + [900.0] * 24, end), end, end + timedelta(days=1), BATTERY)
    assert value == pytest.approx((100 * 23 + 500) / 24 * BATTERY.round_trip_efficiency)
    # Weighed by time over the next day's own length: a 23-hour day of quarter hours.
    quarters = [PriceSlot(end + timedelta(minutes=15 * q), end + timedelta(minutes=15 * (q + 1)), 200.0, 140.0) for q in range(92)]
    assert end_value_next_day(quarters, end, end + timedelta(hours=23), BATTERY) == pytest.approx(200 * BATTERY.round_trip_efficiency)


def _tariff(hour: int) -> float:
    """The same cheap window every night: 02-05 cheap, 16-19 dear, a flat day rate."""
    hour %= 24
    return 17.3 if 2 <= hour < 5 else 34.4 if 16 <= hour < 19 else 25.78


def test_the_energy_left_is_worth_no_more_than_putting_it_back_as_the_plan_counts_it() -> None:
    from custom_components.spotnav.battery.plan import default_end_value
    from custom_components.spotnav.battery.plan_model import PlanSlot

    end = MIDNIGHT + timedelta(days=1)
    refill = 17.3 / BATTERY.charge_efficiency
    assert refill < 25.78 * BATTERY.round_trip_efficiency
    # With the reviewed day (and the one before it): the window's price, with the wear of the charge where given.
    ahead = _ahead([_tariff(hour) for hour in range(24)], end)
    two = _ahead([_tariff(hour) for hour in range(48)], end - timedelta(days=1))
    three = _ahead([_tariff(hour) for hour in range(72)], end - timedelta(days=2))
    assert end_value_next_day(two, end, end + timedelta(days=1), BATTERY) == pytest.approx(refill)
    assert end_value_next_day(three, end, end + timedelta(days=1), BATTERY) == pytest.approx(refill)
    worn = (17.3 + 1.2) / BATTERY.charge_efficiency
    assert end_value_next_day(three, end, end + timedelta(days=1), BATTERY, 1.2) == pytest.approx(worn)
    # The live plan's value on the same days of prices: the same figure.
    slots = tuple(PlanSlot(start=p.start, end=p.end, buy=p.buy, sell=p.sell, house_kw=1.0, solar_kw=0.0, car_kw=0.0) for p in ahead)
    earlier = tuple((p.start, p.end, p.buy) for p in three[:48])
    assert default_end_value(slots, BATTERY, 1.2, earlier) == pytest.approx(worn)
    # The next day alone is no refill to count on, nor is one where the reviewed day had no cheap hours (cheap hours
    # that come and go: the dearest of the days is counted on): the next day's mean less the round trip stands.
    mean = sum(_tariff(hour) for hour in range(24)) / 24
    assert end_value_next_day(ahead, end, end + timedelta(days=1), BATTERY) == pytest.approx(mean * BATTERY.round_trip_efficiency)
    mixed = [*_ahead([25.78] * 24, end - timedelta(days=1)), *ahead]
    assert end_value_next_day(mixed, end, end + timedelta(days=1), BATTERY) == pytest.approx(mean * BATTERY.round_trip_efficiency)


def test_the_days_own_end_value_and_its_perfect_plan_follow_the_same_rule() -> None:
    # Two days on the tariff, an idle battery at 50 %: the review's own end value is the window's refill too.
    buckets = _day(battery=[0.0] * 24, soc=[50.0] * 25, house=0.4)
    for index, bucket in enumerate(buckets):
        bucket.buy, bucket.sell = _tariff(index // 4), 5.0
    before = [Bucket(bucket.start - timedelta(days=1), buy=bucket.buy, sell=bucket.sell) for bucket in buckets]
    at = MIDNIGHT + timedelta(days=1, hours=1)
    review = review_day(DAY, [*before, *buckets], BATTERY, TZ, at, (), MODE_BALANCED)
    assert review is not None
    assert review.end_value == pytest.approx(17.3 / BATTERY.charge_efficiency)
    worn = review_day(DAY, [*before, *buckets], BATTERY, TZ, at, (), MODE_BALANCED, wear=1.2)
    assert worn is not None and worn.end_value == pytest.approx((17.3 + 1.2) / BATTERY.charge_efficiency)
    # The day alone: its median less the round trip, as before.
    alone = review_day(DAY, buckets, BATTERY, TZ, at, (), MODE_BALANCED)
    assert alone is not None and alone.end_value == pytest.approx(25.78 * BATTERY.round_trip_efficiency)
    # Its perfect plan carries the house through the day rate and meets the next window nearly empty, where the
    # day rate times the round trip had it end the day full, bought at the day rate.
    from custom_components.spotnav.battery.hindsight import measured_slot, perfect_plans

    slots = [measured_slot(bucket) for bucket in buckets]
    made = perfect_plans(slots, BATTERY, 50.0, 1.2, review.end_value, (MODE_BALANCED,), 10.0)[MODE_BALANCED]
    full = perfect_plans(slots, BATTERY, 50.0, 1.2, 25.78 * BATTERY.round_trip_efficiency, (MODE_BALANCED,), 10.0)[MODE_BALANCED]
    assert made.end_soc_pct < 30
    assert full.end_soc_pct > 80


def test_without_the_next_days_prices_there_is_no_value_yet() -> None:
    end = MIDNIGHT + timedelta(days=1)
    assert end_value_next_day(_ahead([100.0] * 12, end), end, end + timedelta(days=1), BATTERY) is None
    assert end_value_next_day([], end, end + timedelta(days=1), BATTERY) is None
    # The day itself does not stand in for the next.
    assert end_value_next_day(_ahead([100.0] * 24, MIDNIGHT), end, end + timedelta(days=1), BATTERY) is None


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
    assert made.mode == MODE_BALANCED and made.basis == BASIS_NEXT_DAY and made.whole


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
    assert low.basis == BASIS_NEXT_DAY


def test_a_way_above_the_perfect_plan_or_below_no_battery_is_clamped_and_says_so() -> None:
    assert share_of(800.0, 1000.0) == (pytest.approx(80.0), False)
    assert share_of(1030.0, 1000.0) == (100.0, True)
    assert share_of(-200.0, 1000.0) == (0.0, True)
    assert share_of(1000.4, 1000.0) == (100.0, False)  # rounding, not worth a word


def test_a_perfect_saving_under_five_kronor_a_day_is_too_small_to_compare() -> None:
    assert MIN_PERFECT_SAVING == 500.0
    assert share_of(300.0, 499.0) == (None, False)
    assert share_of(300.0, 500.0) == (pytest.approx(60.0), False)
    assert share_of(50.0, 0.0) == (None, False)
    # A week: 5 kr a day over its days.
    assert share_of(1500.0, 3000.0, MIN_PERFECT_SAVING * 7) == (None, False)
    assert share_of(1500.0, 3500.0, MIN_PERFECT_SAVING * 7)[0] == pytest.approx(1500 / 3500 * 100)


def test_a_day_the_battery_sold_is_measured_against_max_profits_perfect_plan() -> None:
    # The battery's own optimiser charged in the cheap night and sold 4 kW over the house in the first dear hours.
    battery = [1.5] * 6 + [-4.0] * 2 + [-1.0] + [0.0] * 15
    soc = [10.0]
    for hour in range(24):
        soc.append(soc[-1] + battery[hour] * 10)
    made = _facit(_day(battery=battery, soc=soc), end_value=100.0)
    own = made.candidates[OWN]
    assert own.work.to_grid_kwh > 5
    # Selling was possible: every way against Max profit's perfect plan, the most permissive rules, not the site's
    # Balanced; the battery's own control lands below it rather than clamped at 100 %.
    assert made.rules == RULES_SELL and made.mode == MODE_BALANCED
    assert made.best == made.perfect[MODE_MAX_PROFIT] and made.saving == pytest.approx(made.perfect[MODE_MAX_PROFIT].saving)
    assert all(item.perfect == pytest.approx(made.perfect[MODE_MAX_PROFIT].saving) for item in made.candidates.values())
    assert own.share is not None and 0 < own.share < 100 and not own.clamped
    assert own.saving < made.perfect[MODE_MAX_PROFIT].saving
    again = Facit.from_dict(made.as_dict())
    assert again is not None and again.as_dict() == made.as_dict()


def test_a_site_that_could_not_sell_keeps_a_perfect_plan_that_does_not_sell() -> None:
    # Nothing went to the grid all day and no way sold: the perfect plan may not sell either.
    ran = [(ACTION_GRID_CHARGE, 1.5)] * 6 + [(ACTION_DISCHARGE_HOUSE, 0.0)] * 18
    made = _facit(_day(battery=[0.0] * 24, soc=[10.0] * 25, ran=ran), end_value=100.0)
    assert made.rules == RULES_NO_SELL and made.best is not None
    assert made.best.work.to_grid_kwh == pytest.approx(0.0)
    assert made.saving == pytest.approx(made.best.saving) and made.best.saving <= made.perfect[MODE_MAX_PROFIT].saving + 1e-6
    assert all(item.perfect == pytest.approx(made.best.saving) for item in made.candidates.values())
    assert all(item.share is not None and item.share <= 100 for item in made.candidates.values())


def test_a_day_stored_before_the_yardstick_is_measured_again_where_a_way_sold() -> None:
    battery = [1.5] * 6 + [-4.0] * 2 + [-1.0] + [0.0] * 15
    soc = [10.0]
    for hour in range(24):
        soc.append(soc[-1] + battery[hour] * 10)
    made = _facit(_day(battery=battery, soc=soc), end_value=100.0)
    old = made.as_dict()
    del old["rules"], old["best"]
    for item in old["candidates"].values():
        item["perfect"] = old["perfect"][MODE_BALANCED]["saving"]  # the site's mode's, as it was
    again = Facit.from_dict(old)
    assert again is not None and again.rules == RULES_SELL and again.best is not None
    assert again.best.as_dict() == made.perfect[MODE_MAX_PROFIT].as_dict()
    assert again.candidates[OWN].share == pytest.approx(made.candidates[OWN].share, abs=0.1)
    # No way sold and Max profit's own plan sold nothing: that plan is the no-sell one with the true wear, not the
    # site's mode's (Balanced: twice the wear). Stored with `rules: "mode"` it is read the same.
    ran = [(ACTION_GRID_CHARGE, 1.5)] * 6 + [(ACTION_DISCHARGE_HOUSE, 0.0)] * 18
    quiet = _facit(_day(battery=[0.0] * 24, soc=[10.0] * 25, ran=ran), end_value=100.0).as_dict()
    assert quiet["perfect"][MODE_MAX_PROFIT]["work"]["to_grid_kwh"] < 0.1
    quiet["rules"], quiet["best"] = RULES_MODE, None
    for raw in (quiet, {k: v for k, v in quiet.items() if k not in ("rules", "best")}):
        kept = Facit.from_dict(raw)
        assert kept is not None and kept.rules == RULES_NO_SELL and kept.best is not None
        assert kept.saving == pytest.approx(kept.perfect[MODE_MAX_PROFIT].saving)
    # Max profit's plan sold where no way did: kept as it was, against the site's mode's perfect plan.
    sold = dict(quiet)
    sold["perfect"] = {k: dict(v) for k, v in quiet["perfect"].items()}
    sold["perfect"][MODE_MAX_PROFIT]["work"] = {**quiet["perfect"][MODE_MAX_PROFIT]["work"], "to_grid_kwh": 2.0}
    kept = Facit.from_dict(sold)
    assert kept is not None and kept.rules == RULES_MODE and kept.best is None
    assert kept.saving == pytest.approx(kept.perfect[MODE_BALANCED].saving)


def test_a_partial_day_gets_its_figures_and_no_share() -> None:
    ran = [(ACTION_GRID_CHARGE, 1.5)] * 6 + [(ACTION_DISCHARGE_HOUSE, 0.0)] * 18
    whole = _day(battery=[0.0] * 24, soc=[10.0] * 25, ran=ran)
    # 86 of 96 quarter hours measured: under 90 %.
    buckets = whole[:40] + whole[50:]
    assert len(buckets) < WHOLE_DAY_SHARE * 96 <= len(whole[:44] + whole[50:])
    review = review_day(DAY, buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1), [MODE_BALANCED], MODE_BALANCED)
    assert review is not None
    made = facit_day(
        buckets,
        BATTERY,
        start_soc_pct=review.start_soc_pct,
        actual_cost=review.actual.cost,
        actual_end_soc_pct=review.actual.end_soc_pct,
        mode=review.mode,
        wear=WEAR,
        end_value=100.0,
        basis=BASIS_NEXT_DAY,
        no_battery_cost=no_battery_cost(buckets),
        day_quarters=96,
        now=MIDNIGHT + timedelta(days=1, hours=1),
    )
    assert made is not None and not made.whole and (made.measured, made.quarters) == (86, 96)
    assert made.saving is not None and made.saving > MIN_PERFECT_SAVING
    assert all(item.share is None for item in [*made.perfect.values(), *made.candidates.values()])
    again = Facit.from_dict(made.as_dict())
    assert again is not None and not again.whole and again.as_dict() == made.as_dict()


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


def test_the_sites_export_alone_does_not_make_selling_possible() -> None:
    """The sell rule follows what a battery sent to the grid, not the meter: a house that exports a little on its
    own (the sun's surplus, a meter's few watt-hours) while no battery sold keeps the no-sell yardstick."""
    ran = [(ACTION_GRID_CHARGE, 1.5)] * 6 + [(ACTION_DISCHARGE_HOUSE, 0.0)] * 18
    buckets = _day(battery=[0.0] * 24, soc=[10.0] * 25, ran=ran)
    for bucket in buckets[48:52]:
        bucket.export_kwh = 0.2  # 0.8 kWh exported with the battery idle
    made = _facit(buckets, end_value=100.0)
    assert made.candidates[OWN].work.to_grid_kwh == pytest.approx(0.0)
    assert made.rules == RULES_NO_SELL
