"""The battery plan's demand-charge side (`battery/demand_plan.py`) and the nightly review's demand cost: what each run's
day peak adds to the month's demand charge against the month's measured peaks before the day."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.demand_plan import (
    DemandBasis,
    ceilings_minor,
    day_demand_cost,
    demand_basis,
    demand_house,
    needed,
    shave_rows,
)
from custom_components.spotnav.battery.plan_model import ACTION_SELF_CONSUME, ACTION_SHAVE, PlanSlot, SlotDecision
from custom_components.spotnav.battery.plan_shadow import DayReview, build_slots, review_day, review_totals
from custom_components.spotnav.planning.auto_settings import DemandComponent
from custom_components.spotnav.planning.demand_charge import HourMean
from custom_components.spotnav.planning.demand_cost import HourCeiling
from custom_components.spotnav.planning.planner import HighLoadWindow

from tests.test_battery_plan_shadow import BATTERY, CHEAP_NIGHT, MIDNIGHT, TZ, _day, _house, _prices

UTC = timezone.utc
GBG_WINDOW = HighLoadWindow(days="weekdays", from_month=11, to_month=3, from_hour=7, to_hour=20)
GBG_TID = DemandComponent(top_n=3, window=GBG_WINDOW, price_winter=135.0, price_summer=0.0)
GBG_STD = DemandComponent(top_n=3, window=None, price_winter=49.0, price_summer=49.0)
#: Friday 13 November 2026, the reviewed day (`MIDNIGHT` is its start in Stockholm).
DAY = date(2026, 11, 13)
#: 45 kr per kW of a day's peak (135 kr over the mean of three days), in öre.
LAMBDA_TID = 4500.0


def _quarters(kw_by_hour: dict[int, float], base: float = 0.0) -> list[tuple[datetime, float]]:
    """The day's 96 quarter hours at [base] kW, the local hours in [kw_by_hour] at theirs."""
    return [
        (MIDNIGHT + timedelta(minutes=15 * index), kw_by_hour.get(index // 4, base)) for index in range(96)
    ]


def _basis(before: dict[date, float], part: DemandComponent = GBG_TID) -> DemandBasis:
    return DemandBasis(TZ, ((part, 13500.0 / part.top_n if part is GBG_TID else 4900.0 / part.top_n, before),))


# ------------------------------------------------------------------------------------------------ a day's cost


def test_a_day_peak_above_the_months_third_costs_the_change_in_the_mean() -> None:
    before = {date(2026, 11, 2): 5.0, date(2026, 11, 3): 4.0, date(2026, 11, 4): 3.0}
    basis = _basis(before)
    # 3.5 kW at 17:00 replaces the 3rd highest day (3.0 kW): (5 + 4 + 3.5) - (5 + 4 + 3) = 0.5 kW at 45 kr.
    assert day_demand_cost(basis, _quarters({17: 3.5}, base=1.0)) == pytest.approx(0.5 * LAMBDA_TID)
    # Under the 3rd highest: nothing.
    assert day_demand_cost(basis, _quarters({17: 2.9}, base=1.0)) == pytest.approx(0.0)
    # Above the highest: 6 - 3 kW.
    assert day_demand_cost(basis, _quarters({17: 6.0}, base=1.0)) == pytest.approx(3.0 * LAMBDA_TID)


def test_an_hour_is_the_mean_of_its_quarters() -> None:
    basis = _basis({})
    # One quarter at 8 kW and three at 0 in the 17:00 hour: a 2 kW hour; with fewer than three days it all counts.
    quarters = [(start, 8.0 if start == MIDNIGHT + timedelta(hours=17) else 0.0) for start, _kw in _quarters({})]
    assert day_demand_cost(basis, quarters) == pytest.approx(2.0 * LAMBDA_TID)


def test_hours_outside_the_window_cost_nothing() -> None:
    # 10 kW at 03:00 and at 21:00 on a weekday: outside 07-20, so never a peak.
    assert day_demand_cost(_basis({}), _quarters({3: 10.0, 21: 10.0})) == pytest.approx(0.0)


def test_an_all_hours_part_counts_the_night_too() -> None:
    basis = _basis({}, GBG_STD)
    assert day_demand_cost(basis, _quarters({3: 6.0}, base=1.0)) == pytest.approx(6.0 * 4900.0 / 3)


# ------------------------------------------------------------------------------------------------ the basis


def _hour(start: datetime, kw: float, **kwargs) -> HourMean:
    return HourMean(start=start, kw=kw, house_kw=kw, **kwargs)


def test_the_basis_takes_the_months_measured_day_peaks_before_the_day_at_their_floor() -> None:
    nov2 = datetime(2026, 11, 2, 16, tzinfo=UTC)  # Monday 17:00 Stockholm
    hours = [
        _hour(nov2, 4.0, floor_kw=3.0),  # a gap in the hour: counted at its floor
        _hour(nov2 + timedelta(days=1), 2.0),
        _hour(MIDNIGHT + timedelta(hours=17), 9.0),  # the reviewed day itself: not before it
        _hour(datetime(2026, 10, 30, 16, tzinfo=UTC), 7.0),  # last month
    ]
    basis = demand_basis((GBG_TID,), hours, DAY, TZ)
    assert basis is not None and basis.priced
    [(_part, marginal, peaks)] = basis.parts
    assert marginal == pytest.approx(LAMBDA_TID)
    assert peaks == {date(2026, 11, 2): 3.0, date(2026, 11, 3): 2.0}


def test_no_basis_without_a_price_this_month() -> None:
    summer = date(2026, 7, 14)
    assert demand_basis((GBG_TID,), [], summer, TZ) is None
    assert demand_basis((DemandComponent(top_n=3),), [], DAY, TZ) is None
    assert demand_basis((), [], DAY, TZ) is None


# ------------------------------------------------------------------------------------------------ the review


def test_the_review_adds_each_runs_demand_cost_and_the_full_bill() -> None:
    # The house draws 1 kW all day; the plan charged the battery at 1.5 kW in the six cheap night hours (outside the
    # window), and nothing happened in the window: every run's peak is the house's 1 kW, under the month's 3rd.
    ran = [(ACTION_SELF_CONSUME, 0.0)] * 24
    buckets = _day(buy=CHEAP_NIGHT, house=1.0, battery=[0.0] * 24, soc=[10.0] * 25, ran=ran)
    now = MIDNIGHT + timedelta(days=1, hours=1)
    quiet = _basis({date(2026, 11, 2): 5.0, date(2026, 11, 3): 4.0, date(2026, 11, 4): 3.0})
    review = review_day(DAY, buckets, BATTERY, TZ, now, demand=quiet)
    assert review is not None and review.demand == {"actual": 0.0, "plan": 0.0, "self_consumption": 0.0}
    # Fewer than three days measured: the day's own 1 kW peak enters the mean for every run.
    review = review_day(DAY, buckets, BATTERY, TZ, now, demand=_basis({}))
    assert review is not None
    assert review.demand == pytest.approx({"actual": LAMBDA_TID, "plan": LAMBDA_TID, "self_consumption": LAMBDA_TID})
    assert review.bill_savings == pytest.approx({"actual": 0.0, "plan": 0.0})
    as_dict = review.as_dict()
    assert as_dict["demand"]["cost"]["actual"] == pytest.approx(LAMBDA_TID)
    assert DayReview.from_dict(as_dict).as_dict() == as_dict
    totals = review_totals([review])
    assert totals["demand"]["days"] == 1 and totals["demand"]["cost"]["plan"] == pytest.approx(LAMBDA_TID)


def test_a_battery_that_covers_the_evening_peak_saves_on_the_demand_charge() -> None:
    # 4 kW in the window's 17:00 hour; the battery covered 3 kW of it, so the meter saw 1 kW.
    battery = [0.0] * 17 + [-3.0] + [0.0] * 6
    soc = [50.0] * 18 + [20.0] * 7
    buckets = _day(buy=[100.0] * 24, house=1.0, battery=battery, soc=soc, ran=None)
    for bucket in buckets[17 * 4 : 18 * 4]:
        bucket.import_kwh, bucket.export_kwh, bucket.battery_kwh = 0.25, 0.0, -0.75
    review = review_day(DAY, buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1), demand=_basis({}))
    assert review is not None and review.demand is not None
    assert review.demand["actual"] == pytest.approx(LAMBDA_TID)
    # Self-consumption from the same 50 % spends the battery on the night's 1 kW and meets the evening's 4 kW empty.
    assert review.demand["self_consumption"] == pytest.approx(4.0 * LAMBDA_TID)
    assert review.bill_savings is not None and review.bill_savings["actual"] > 3.0 * LAMBDA_TID - 2000


def test_a_review_without_a_basis_reads_as_before() -> None:
    buckets = _day(buy=CHEAP_NIGHT, house=1.0, battery=[0.0] * 24, soc=[10.0] * 25, ran=None)
    review = review_day(DAY, buckets, BATTERY, TZ, MIDNIGHT + timedelta(days=1, hours=1))
    assert review is not None and review.demand is None and review.bill_savings is None
    assert "demand" not in review.as_dict() and "demand" not in review_totals([review])


# ------------------------------------------------------------------------------------------------ the variant's inputs


def test_the_slots_take_the_hours_hard_cap_and_soft_ceiling() -> None:
    prices = _prices([100.0] * 4)
    house = _house(4, kw=2.0)
    hour = MIDNIGHT + timedelta(hours=1)
    plain, _ = build_slots(MIDNIGHT, prices, house, None, [], 13.8, TZ)
    same, _ = build_slots(MIDNIGHT, prices, house, None, [], 13.8, TZ, hour_caps={}, ceilings={})
    assert plain == same
    slots, _ = build_slots(
        MIDNIGHT, prices, house, None, [], 13.8, TZ,
        hour_caps={hour: 6.0, MIDNIGHT + timedelta(hours=2): 20.0},
        ceilings={hour: (8.0, LAMBDA_TID), MIDNIGHT: (3.0, LAMBDA_TID), MIDNIGHT + timedelta(hours=3): (2.0, 0.0)},
    )
    assert [slot.import_cap_kw for slot in slots] == [13.8, 6.0, 13.8, 13.8]
    # A soft ceiling never above the hour's hard cap; a ceiling at price 0 is none.
    assert [slot.soft_cap_kw for slot in slots] == [3.0, 6.0, None, None]
    assert [slot.peak_price for slot in slots] == [LAMBDA_TID, LAMBDA_TID, 0.0, 0.0]
    # The day a slot's peak counts toward: the ceiling's own, else the hour's local day; none without a ceiling.
    assert [slot.peak_day for slot in slots] == [DAY, DAY, None, None]
    eve = DAY - timedelta(days=1)
    counted, _ = build_slots(MIDNIGHT, prices, house, None, [], 13.8, TZ, ceilings={MIDNIGHT: (3.0, LAMBDA_TID, eve)})
    assert counted[0].peak_day == eve


def test_the_ceiling_in_minor_units_and_the_house_at_p90_in_counted_hours() -> None:
    hour = MIDNIGHT + timedelta(hours=17)
    ceilings = ceilings_minor({hour: HourCeiling(2.4, 45.0), MIDNIGHT: HourCeiling(1.0, 0.0)})
    assert ceilings == {hour: (2.4, 4500.0, None)}

    class Band:
        def bounds(self, value: float, start: datetime) -> tuple[float, float]:
            return value - 0.5, value + 0.7

    house = {MIDNIGHT: 1.0, hour: 2.0}
    assert demand_house(house, Band(), ceilings) == {MIDNIGHT: 1.0, hour: pytest.approx(2.7)}
    assert demand_house(house, None, ceilings) == house
    assert needed(ceilings, None, 13.8)
    assert not needed({}, {hour: 13.8}, 13.8) and needed({}, {hour: 6.0}, 13.8)


def test_shave_rows_list_only_the_shaves() -> None:
    slot = PlanSlot(MIDNIGHT, MIDNIGHT + timedelta(hours=1), 100.0, 40.0, 3.0, soft_cap_kw=2.0, peak_price=LAMBDA_TID)
    shave = SlotDecision(MIDNIGHT, slot.end, ACTION_SHAVE, -1.0, 2.0, 50.0, 200.0)
    other = SlotDecision(MIDNIGHT, slot.end, ACTION_SELF_CONSUME, -3.0, 0.0, 50.0, 0.0)
    assert shave_rows([shave, other], [slot, slot]) == [[MIDNIGHT.isoformat(), 1.0, 2.0, 2.0, 4500.0]]
