"""The house battery plan (`battery/plan.py`): a pure dynamic programme over the stored energy, in shadow.

Prices are in öre per kWh, power in kW. Each test builds its own slots; nothing touches Home Assistant.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.plan import (
    _apply,
    _candidates,
    action_power,
    buy_prices,
    default_end_value,
    objective,
    plan_battery,
    run_slot,
    self_consumption,
    sell_prices,
)
from custom_components.spotnav.battery.plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
    ACTION_SELF_CONSUME,
    BatteryParams,
    PlanInputError,
    PlanRequest,
    PlanSlot,
)
from custom_components.spotnav.planning.planner import FiscalChoice, effective_minor_per_kwh

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
BATTERY = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=10, max_soc_pct=100)


def _slots(
    buy: list[float],
    *,
    sell: list[float] | None = None,
    house: float | list[float] = 1.0,
    solar: float | list[float] = 0.0,
    car: float | list[float] = 0.0,
    minutes: int = 60,
    import_cap: float | None = None,
    export_cap: float | None = None,
) -> tuple[PlanSlot, ...]:
    def at(value: float | list[float], index: int) -> float:
        return value[index] if isinstance(value, list) else value

    step = timedelta(minutes=minutes)
    return tuple(
        PlanSlot(
            start=T0 + step * i,
            end=T0 + step * (i + 1),
            buy=price,
            sell=(sell[i] if sell is not None else price - 60),
            house_kw=at(house, i),
            solar_kw=at(solar, i),
            car_kw=at(car, i),
            import_cap_kw=import_cap,
            export_cap_kw=export_cap,
        )
        for i, price in enumerate(buy)
    )


def _request(slots: tuple[PlanSlot, ...], **kwargs) -> PlanRequest:
    kwargs.setdefault("battery", BATTERY)
    kwargs.setdefault("soc_now_pct", 50)
    return PlanRequest(slots=slots, **kwargs)


# ----------------------------------------------------------------- the plan's choices


def test_flat_prices_leave_the_battery_to_itself() -> None:
    # A stored kWh worth well below the flat price (with the round trip and both ways' wear): spending it pays.
    result = plan_battery(_request(_slots([100] * 24), end_value=60))
    assert {d.action for d in result.plan.decisions} == {ACTION_SELF_CONSUME}
    assert result.plan.cost == pytest.approx(result.baseline.cost)
    assert result.savings == pytest.approx(0, abs=1e-6)


def test_charges_in_the_cheap_hours_for_the_dear_ones() -> None:
    buy = [20] * 4 + [300] * 4
    result = plan_battery(_request(_slots(buy, house=2.0), soc_now_pct=10, end_value=0))
    actions = [d.action for d in result.plan.decisions]
    assert ACTION_GRID_CHARGE in actions[:4]
    assert ACTION_GRID_CHARGE not in actions[4:]
    assert result.plan.cost < result.baseline.cost
    assert result.savings > 0


def test_holds_in_a_cheap_hour_rather_than_spend_the_battery_on_it() -> None:
    # Full battery: discharging at 20 öre wastes what the 300-öre hours need.
    buy = [20] * 3 + [300] * 3
    result = plan_battery(_request(_slots(buy, house=3.0), soc_now_pct=100, end_value=0))
    assert [d.action for d in result.plan.decisions[:3]] == [ACTION_HOLD] * 3
    assert all(d.battery_kw < 0 for d in result.plan.decisions[3:])


def test_a_small_spread_does_not_churn_the_battery() -> None:
    # 100 -> 105 öre does not pay the round trip's 10 % and the wear.
    buy = [100, 105] * 6
    result = plan_battery(_request(_slots(buy), soc_now_pct=10, end_value=0))
    assert ACTION_GRID_CHARGE not in {d.action for d in result.plan.decisions}


def test_cycle_cost_is_weighed_but_never_counted_as_money() -> None:
    result = plan_battery(_request(_slots([20] * 3 + [300] * 3, house=2.0), soc_now_pct=10, cycle_cost=5))
    assert result.plan.wear > 0
    money = sum(d.cost for d in result.plan.decisions)
    assert result.plan.cost == pytest.approx(money)


def test_a_dear_sell_price_sells_the_sun_instead_of_storing_it() -> None:
    # Surplus sun sells at 200 öre now; stored, it would only save 50-öre imports later.
    buy = [250, 250, 50, 50, 50, 50]
    sell = [200, 200, 0, 0, 0, 0]
    solar = [4.0, 4.0, 0, 0, 0, 0]
    result = plan_battery(_request(_slots(buy, sell=sell, solar=solar), soc_now_pct=10, end_value=0))
    first = result.plan.decisions[:2]
    assert all(d.battery_kw == pytest.approx(0) for d in first)
    assert all(d.grid_kw == pytest.approx(-3.0) for d in first)
    assert {d.action for d in first} <= {ACTION_DISCHARGE_HOUSE}


def test_export_discharge_only_behind_its_flag() -> None:
    buy = [300] * 4
    sell = [400] * 4
    off = plan_battery(_request(_slots(buy, sell=sell, house=0.5), soc_now_pct=100, end_value=0))
    assert ACTION_EXPORT_DISCHARGE not in {d.action for d in off.plan.decisions}
    assert all(d.grid_kw >= -1e-9 for d in off.plan.decisions)
    on = plan_battery(
        _request(_slots(buy, sell=sell, house=0.5), soc_now_pct=100, end_value=0, allow_export_discharge=True)
    )
    assert ACTION_EXPORT_DISCHARGE in {d.action for d in on.plan.decisions}
    assert on.plan.cost < off.plan.cost


def test_end_value_decides_how_full_the_horizon_ends() -> None:
    buy = [50] * 6
    empty = plan_battery(_request(_slots(buy), soc_now_pct=50, end_value=0))
    full = plan_battery(_request(_slots(buy), soc_now_pct=50, end_value=200))
    assert empty.plan.end_soc_pct == pytest.approx(BATTERY.min_soc_pct, abs=0.5)
    assert full.plan.end_soc_pct == pytest.approx(BATTERY.max_soc_pct, abs=0.5)


def test_default_end_value_is_the_median_buy_price_times_the_round_trip() -> None:
    # Four hours of prices: no day whose cheap hours say what a refill costs.
    slots = _slots([10, 20, 30, 1000])
    assert default_end_value(slots, BATTERY) == pytest.approx(25 * 0.9)
    assert plan_battery(_request(slots)).end_value == pytest.approx(25 * 0.9)


# ----------------------------------------------------------------- the end value: what a refill costs


def _flux(hour: int) -> float:
    """A tariff with the same cheap window every night (pence): 02-05 cheap, 16-19 dear, a flat day rate."""
    hour %= 24
    return 17.3 if 2 <= hour < 5 else 34.4 if 16 <= hour < 19 else 25.78


def _earlier(buy: list[float]) -> tuple[tuple[datetime, datetime, float], ...]:
    """Hourly prices up to T0, the last one ending there."""
    first = T0 - timedelta(hours=len(buy))
    return tuple((first + timedelta(hours=i), first + timedelta(hours=i + 1), price) for i, price in enumerate(buy))


def test_a_cheap_window_every_night_holds_the_end_value_at_what_a_refill_costs() -> None:
    day = _slots([_flux(hour) for hour in range(24)])
    before = _earlier([_flux(hour) for hour in range(24)])
    refill = 17.3 / BATTERY.charge_efficiency
    # The day rate times the round trip (23.2) is more than putting the energy back in the window costs (18.2).
    assert refill < 25.78 * 0.9
    assert default_end_value(day, BATTERY, earlier=before) == pytest.approx(refill)
    # The wear of the charge is part of what a refill costs.
    worn = (17.3 + 2.4) / BATTERY.charge_efficiency
    assert default_end_value(day, BATTERY, 2.4, before) == pytest.approx(worn)
    assert plan_battery(_request(day, cycle_cost=2.4, earlier_prices=before)).end_value == pytest.approx(worn)
    # One day's cheap hours are no refill to count on: the median stands, as it does without the days before.
    assert default_end_value(day, BATTERY) == pytest.approx(25.78 * 0.9)
    # A horizon from 10:00 holds no cheap window: the hours of the day that have passed say what it costs.
    rest = day[10:]
    assert default_end_value(rest, BATTERY) == pytest.approx(25.78 * 0.9)
    passed = (*before, *((slot.start, slot.end, slot.buy) for slot in day[:10]))
    assert default_end_value(rest, BATTERY, earlier=passed) == pytest.approx(refill)
    assert plan_battery(_request(rest, earlier_prices=passed, cycle_cost=0)).end_value == pytest.approx(refill)
    # Midway through a price slot: the running slot's passed part counts with the rest, so the day is whole.
    half = (replace_start(day[10]), *day[11:])
    cut = (*passed, (day[10].start, half[0].start, day[10].buy))
    assert default_end_value(half, BATTERY, earlier=cut) == pytest.approx(refill)


def replace_start(slot: PlanSlot) -> PlanSlot:
    return PlanSlot(
        start=slot.start + timedelta(minutes=30), end=slot.end, buy=slot.buy, sell=slot.sell, house_kw=slot.house_kw,
        solar_kw=slot.solar_kw, car_kw=slot.car_kw,
    )


def test_the_refill_counted_on_is_the_dearest_of_the_last_days() -> None:
    day = _slots([_flux(hour) for hour in range(24)])
    # Cheap hours that differ from day to day (spot prices): yesterday's night was no cheaper than the day rate.
    spot = _earlier([_flux(hour) for hour in range(24)] + [25.78] * 24)
    assert default_end_value(day, BATTERY, earlier=spot) == pytest.approx(25.78 * 0.9)
    # The same window three days running: the window's price.
    tariff = _earlier([_flux(hour) for hour in range(48)])
    assert default_end_value(day, BATTERY, earlier=tariff) == pytest.approx(17.3 / BATTERY.charge_efficiency)
    # Days older than the three are not read, nor is a part of a day.
    old = _earlier([25.78] * 24 + [_flux(hour) for hour in range(48)])
    assert default_end_value(day, BATTERY, earlier=old) == pytest.approx(17.3 / BATTERY.charge_efficiency)
    part = _earlier([25.78] * 12 + [_flux(hour) for hour in range(24)])
    assert default_end_value(day, BATTERY, earlier=part) == pytest.approx(17.3 / BATTERY.charge_efficiency)
    # A refill for nothing or less is worth nothing, never less; a negative median stays the lesser of the two.
    free = _slots([-5.0] * 4 + [30.0] * 20)
    assert default_end_value(free, BATTERY, earlier=_earlier([-5.0] * 4 + [30.0] * 20)) == 0.0
    assert default_end_value(_slots([-5.0] * 24), BATTERY, earlier=_earlier([-5.0] * 24)) == pytest.approx(-5.0 * 0.9)


def _flux_day(with_earlier: bool) -> tuple[float, float, float]:
    """A winter day on the tariff, planned again every hour to midnight in Balanced, from an empty battery: the kWh
    bought at the day rate, the level at midnight (%) and the day's cost."""
    from custom_components.spotnav.battery.plan import plan_mode
    from custom_components.spotnav.battery.plan_model import MODE_BALANCED, MODE_DEFS

    def house(hour: int) -> float:
        return 0.25 if hour < 6 else 0.6 if 17 <= hour < 22 else 0.35

    day = _slots([_flux(hour) for hour in range(24)], sell=[5.0] * 24, house=[house(hour) for hour in range(24)])
    before = _earlier([_flux(hour) for hour in range(48)])
    soc = BATTERY.min_soc_pct
    day_rate = cost = 0.0
    for hour in range(24):
        passed = tuple((slot.start, slot.end, slot.buy) for slot in day[:hour])
        request = _request(day[hour:], soc_now_pct=soc, earlier_prices=(*before, *passed) if with_earlier else ())
        first = plan_mode(request, MODE_DEFS[MODE_BALANCED], 1.2, sell_margin=10.0).plan.decisions[0]
        soc, cost = first.soc_end_pct, cost + first.cost
        if day[hour].buy == 25.78:
            day_rate += max(0.0, first.grid_kw)
    return day_rate, soc, cost


def test_a_cheap_window_every_night_is_not_held_through_the_day() -> None:
    """The field case: a full battery was held all day and the house bought at the day rate, for energy left at
    midnight that the next night's window would have put back for less."""
    held_kwh, held_soc, held_cost = _flux_day(with_earlier=False)
    kwh, soc, cost = _flux_day(with_earlier=True)
    # Without the days before, a horizon after 05:00 shows no cheap window: the old value, the old way.
    assert held_kwh > 5 and held_soc > 70
    # With them the battery carries the house through the day and meets the next window empty.
    assert kwh < 2 and soc < 25
    # Cheaper even before the energy left is counted, and still when it is, at the window's price.
    left = (held_soc - soc) / 100 * BATTERY.usable_kwh * 17.3 / BATTERY.charge_efficiency
    assert cost < held_cost - left - 20


def test_energy_is_still_carried_over_when_tomorrow_is_dear() -> None:
    # Today on the tariff, tomorrow dear throughout (no cheap window ahead): from 13:00, with the days before.
    buy = [_flux(hour) for hour in range(13, 24)] + [60.0 + 10 * (hour % 3) for hour in range(24)]
    slots = _slots(buy, house=1.0)
    before = _earlier([_flux(hour) for hour in range(-11, 13)])
    result = plan_battery(_request(slots, soc_now_pct=100, earlier_prices=before, cycle_cost=1.2))
    # The dearest of the days sets the refill: tomorrow has none below 60, so the median stands.
    assert result.end_value == pytest.approx(default_end_value(slots, BATTERY))
    assert result.end_value > 2 * 17.3
    # The battery is not emptied on today's day rate: it enters the dear day full, and gives all it has there.
    midnight = result.plan.decisions[10]
    assert midnight.soc_end_pct > 90
    assert result.plan.end_soc_pct == pytest.approx(BATTERY.min_soc_pct, abs=0.5)
    # And a horizon that ends on dear hours with dear days behind it keeps what it has (the median, as before).
    dear = _slots([40.0] * 20 + [30.0] * 4)
    kept = plan_battery(_request(dear, soc_now_pct=80, earlier_prices=_earlier([40.0] * 48), cycle_cost=0))
    assert kept.end_value == pytest.approx(40 * 0.9)


def test_savings_count_the_energy_each_run_leaves() -> None:
    result = plan_battery(_request(_slots([20] * 4 + [300] * 4, house=2.0), soc_now_pct=10, end_value=100))
    battery = result.battery
    baseline_net = result.baseline.cost - result.baseline.end_kwh(battery) * 100
    plan_net = result.plan.cost - result.plan.end_kwh(battery) * 100
    assert result.savings == pytest.approx(baseline_net - plan_net)


def test_the_car_is_a_fixed_load_the_plan_carries() -> None:
    slots = _slots([20] * 2 + [300] * 2, house=1.0, car=[7.0, 7.0, 0, 0], import_cap=10.0)
    result = plan_battery(_request(slots, soc_now_pct=10, end_value=0))
    for decision in result.plan.decisions[:2]:
        assert decision.grid_kw <= 10.0 + 1e-9
        assert decision.battery_kw <= 2.0 + 1e-9  # the cap leaves the battery 10 - 8 kW


def test_quarter_hour_slots() -> None:
    result = plan_battery(_request(_slots([20] * 8 + [300] * 8, house=2.0, minutes=15), soc_now_pct=10, end_value=0))
    assert len(result.plan.decisions) == 16
    assert result.savings > 0


# ----------------------------------------------------------------- bounds


def test_never_imports_above_the_cap() -> None:
    slots = _slots([20] * 4 + [300] * 4, house=2.0, import_cap=4.0)
    result = plan_battery(_request(slots, soc_now_pct=10, end_value=0))
    assert all(d.grid_kw <= 4.0 + 1e-9 for d in result.plan.decisions)
    assert any(d.action == ACTION_GRID_CHARGE for d in result.plan.decisions)
    assert not any(d.over_cap for d in result.plan.decisions)


def test_a_hold_that_would_break_the_cap_discharges_the_excess() -> None:
    slot = _slots([100], house=6.0, import_cap=4.0)[0]
    outcome = _apply(slot, BATTERY, 5.0, ACTION_HOLD, 0.0, 0.0)
    assert outcome.grid_kw == pytest.approx(4.0)
    assert outcome.battery_kw == pytest.approx(-2.0)
    assert not outcome.over_cap


def test_a_house_above_the_cap_is_marked_not_planned() -> None:
    slots = _slots([100] * 2, house=12.0, import_cap=4.0)
    result = plan_battery(_request(slots, soc_now_pct=100, end_value=0))
    first = result.plan.decisions[0]
    assert first.over_cap
    assert first.battery_kw == pytest.approx(-BATTERY.max_discharge_kw)


def test_export_above_its_cap_is_curtailed() -> None:
    slots = _slots([100], solar=10.0, house=0.0, export_cap=3.0)
    result = plan_battery(_request(slots, soc_now_pct=100, end_value=0))
    decision = result.plan.decisions[0]
    assert decision.grid_kw == pytest.approx(-3.0)
    assert decision.curtailed_kwh == pytest.approx(7.0)


@pytest.mark.parametrize("seed", range(8))
def test_random_horizons_keep_every_bound(seed: int) -> None:
    rng = random.Random(seed)
    n = 48
    buy = [rng.uniform(-20, 400) for _ in range(n)]
    sell = [b - rng.uniform(0, 80) for b in buy]
    house = [rng.uniform(0.2, 4) for _ in range(n)]
    solar = [max(0.0, rng.uniform(-3, 6)) for _ in range(n)]
    battery = BatteryParams(
        usable_kwh=rng.uniform(5, 40),
        max_charge_kw=rng.uniform(2, 12),
        max_discharge_kw=rng.uniform(2, 12),
        min_soc_pct=rng.uniform(0, 30),
        max_soc_pct=rng.uniform(80, 100),
        round_trip_efficiency=rng.uniform(0.8, 0.95),
    )
    cap = rng.uniform(5, 15)
    slots = _slots(buy, sell=sell, house=house, solar=solar, minutes=rng.choice([15, 60]), import_cap=cap, export_cap=cap)
    request = _request(slots, battery=battery, soc_now_pct=rng.uniform(battery.min_soc_pct, battery.max_soc_pct),
                       allow_export_discharge=rng.random() < 0.5, end_value=rng.uniform(0, 200))
    result = plan_battery(request)
    for decision in result.plan.decisions:
        assert battery.min_soc_pct - 1e-6 <= decision.soc_end_pct <= battery.max_soc_pct + 1e-6
        assert decision.grid_kw <= cap + 1e-6 or decision.over_cap
        assert decision.grid_kw >= -cap - 1e-6
        assert -battery.max_discharge_kw - 1e-6 <= decision.battery_kw <= battery.max_charge_kw + 1e-6
    # The plan is never worse than doing nothing, end values counted (a little slack for the grid's steps).
    assert result.savings >= -0.01 * abs(result.baseline.cost) - 1


def test_matches_a_brute_force_search_on_a_small_horizon() -> None:
    """Every sequence of open actions over 4 slots, against the plan: the plan is within the grid's rounding."""
    buy = [30, 250, 40, 300]
    sell = [0, 150, 0, 200]
    solar = [0, 3, 0, 0]
    request = _request(
        _slots(buy, sell=sell, house=2.0, solar=solar, import_cap=8.0),
        soc_now_pct=30,
        end_value=60,
        soc_steps=400,
    )
    battery = request.battery
    end_value = 60

    best = float("inf")
    start = battery.usable_kwh * 0.30

    def walk(index: int, soc: float, total: float) -> None:
        nonlocal best
        if index == len(request.slots):
            best = min(best, total - soc * end_value)
            return
        slot = request.slots[index]
        for action, power in _candidates(slot, battery, soc, request):
            outcome = _apply(slot, battery, soc, action, power, request.cycle_cost)
            walk(index + 1, outcome.soc_end_kwh, total + outcome.cost + outcome.wear)

    walk(0, start, 0.0)
    result = plan_battery(request)
    planned = result.plan.cost + result.plan.wear - result.plan.end_kwh(battery) * end_value
    assert planned <= best + 2.0
    assert planned >= best - 1e-6


# ----------------------------------------------------------------- prices


def test_buy_prices_are_the_planners_own() -> None:
    fiscal = FiscalChoice(
        tax_enabled=True, tax_minor_per_kwh=36.0, transfer_enabled=True, transfer_minor_per_kwh=25.0,
        vat_enabled=True, vat_percent=25.0,
    )
    starts = [T0, T0 + timedelta(hours=1)]
    assert buy_prices([1.0, -0.1], starts, fiscal) == [
        effective_minor_per_kwh(1.0, fiscal, T0),
        effective_minor_per_kwh(-0.1, fiscal, starts[1]),
    ]
    assert buy_prices([1.0], [T0], fiscal)[0] == pytest.approx((100 + 36 + 25) * 1.25)


def test_sell_prices_add_a_fixed_credit() -> None:
    assert sell_prices([1.0, -0.2]) == [100.0, -20.0]
    assert sell_prices([1.0], 5.0) == [105.0]
    assert sell_prices([1.0], -3.0) == [97.0]


# ----------------------------------------------------------------- the baseline and the output


def test_baseline_is_self_consumption_with_the_same_bounds() -> None:
    slots = _slots([100] * 4, house=[3, 3, 0, 0], solar=[0, 0, 4, 4])
    run = self_consumption(_request(slots, soc_now_pct=50))
    assert [d.action for d in run.decisions] == [ACTION_SELF_CONSUME] * 4
    assert run.decisions[0].battery_kw == pytest.approx(-3.0)
    assert run.decisions[2].battery_kw == pytest.approx(4.0)


def test_as_dict_for_diagnostics() -> None:
    result = plan_battery(_request(_slots([20, 300], house=2.0), soc_now_pct=10, end_value=0))
    out = result.as_dict()
    assert set(out) == {
        "cost", "baseline_cost", "savings", "end_value", "end_soc_pct", "baseline_end_soc_pct", "wear", "min_soc_pct",
        "slots",
    }
    assert out["slots"][0]["start"] == T0.isoformat()
    assert set(out["slots"][0]) >= {"start", "end", "action", "battery_kw", "grid_kw", "soc_end_pct", "cost"}


# ----------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"slots": ()}, "no_slots"),
        ({"soc_now_pct": 120}, "invalid_soc"),
        ({"cycle_cost": -1}, "invalid_cycle_cost"),
        ({"soc_steps": 1}, "invalid_steps"),
        ({"charge_shares": (0.0,)}, "invalid_charge_shares"),
        ({"end_value": float("nan")}, "invalid_end_value"),
        ({"battery": BatteryParams(usable_kwh=0, max_charge_kw=1, max_discharge_kw=1)}, "invalid_battery"),
        ({"battery": BatteryParams(10, 1, 1, min_soc_pct=50, max_soc_pct=40)}, "invalid_battery"),
        ({"battery": BatteryParams(10, 1, 1, round_trip_efficiency=1.2)}, "invalid_battery"),
    ],
)
def test_refuses_bad_inputs(change: dict, code: str) -> None:
    kwargs = {"slots": _slots([100] * 2), "battery": BATTERY, "soc_now_pct": 50, **change}
    with pytest.raises(PlanInputError) as err:
        plan_battery(PlanRequest(**kwargs))
    assert err.value.code == code


def test_refuses_a_gap_between_slots() -> None:
    a, b = _slots([100] * 3)[0], _slots([100] * 3)[2]
    with pytest.raises(PlanInputError) as err:
        plan_battery(_request((a, b)))
    assert err.value.code == "gap"


def test_refuses_a_bad_slot() -> None:
    slot = _slots([float("nan")])
    with pytest.raises(PlanInputError) as err:
        plan_battery(_request(slot))
    assert err.value.code == "invalid_slot"
    naive = PlanSlot(start=datetime(2026, 1, 1), end=datetime(2026, 1, 1, 1), buy=1, sell=1, house_kw=1)
    with pytest.raises(PlanInputError):
        plan_battery(_request((naive,)))



# ----------------------------------------------------------------- running a plan against what happened


def test_action_power_follows_the_slots_own_load() -> None:
    deficit = _slots([100], house=3.0)[0]
    surplus = _slots([100], house=1.0, solar=4.0)[0]
    soc = 5.0
    assert action_power(deficit, BATTERY, soc, ACTION_SELF_CONSUME) == pytest.approx(-3.0)
    assert action_power(surplus, BATTERY, soc, ACTION_SELF_CONSUME) == pytest.approx(3.0)
    assert action_power(deficit, BATTERY, soc, ACTION_HOLD) == 0.0
    assert action_power(surplus, BATTERY, soc, ACTION_HOLD) == pytest.approx(3.0)
    assert action_power(deficit, BATTERY, soc, ACTION_DISCHARGE_HOUSE) == pytest.approx(-3.0)
    assert action_power(surplus, BATTERY, soc, ACTION_DISCHARGE_HOUSE) == 0.0
    assert action_power(deficit, BATTERY, soc, ACTION_GRID_CHARGE, 2.0) == pytest.approx(2.0)
    assert action_power(surplus, BATTERY, soc, ACTION_GRID_CHARGE, 2.0) == pytest.approx(3.0)
    assert action_power(deficit, BATTERY, 9.0, ACTION_EXPORT_DISCHARGE) == pytest.approx(-5.0)
    with pytest.raises(ValueError):
        action_power(deficit, BATTERY, soc, "boost")


def test_action_power_stays_within_the_energy_left() -> None:
    slot = _slots([100], house=8.0)[0]
    floor = BATTERY.min_kwh
    assert action_power(slot, BATTERY, floor, ACTION_SELF_CONSUME) == 0.0
    assert action_power(slot, BATTERY, BATTERY.max_kwh, ACTION_GRID_CHARGE, 5.0) == 0.0


def test_run_slot_prices_one_slot() -> None:
    slot = _slots([100], sell=[40], house=1.0, solar=5.0, export_cap=2.0)[0]
    decision = run_slot(slot, BATTERY, BATTERY.max_kwh, ACTION_SELF_CONSUME, 0.0)
    assert decision.grid_kw == pytest.approx(-2.0)
    assert decision.curtailed_kwh == pytest.approx(2.0)
    assert decision.cost == pytest.approx(-80.0)
    assert decision.soc_end_pct == pytest.approx(BATTERY.max_soc_pct)


# ----------------------------------------------------------------- stability (Predbat item 6)


def test_a_running_action_is_kept_when_the_new_plan_gains_too_little() -> None:
    # Charging now at 100 vs 101 later is nearly a tie: the running charge stays.
    buy = [100, 101, 300, 300]
    slots = _slots(buy, house=2.0)
    fresh = plan_battery(_request(slots, soc_now_pct=10, end_value=0))
    first = fresh.plan.decisions[0].action
    other = ACTION_HOLD if first != ACTION_HOLD else ACTION_SELF_CONSUME
    held = plan_battery(_request(slots, soc_now_pct=10, end_value=0), keep=(other, 0.0), keep_min_gain=10_000)
    assert held.kept is True
    assert held.plan.decisions[0].action == other
    assert held.keep_gain is not None and 0 <= held.keep_gain <= 10_000
    # The rest of the plan is still the cheapest from where the kept slot leaves the battery.
    assert held.as_dict()["kept"] is True


def test_a_running_action_is_replaced_when_the_new_plan_gains_enough() -> None:
    slots = _slots([20, 300, 300, 300], house=2.0)
    request = _request(slots, soc_now_pct=10, end_value=0)
    fresh = plan_battery(request)
    assert fresh.plan.decisions[0].action == ACTION_GRID_CHARGE
    moved = plan_battery(request, keep=(ACTION_SELF_CONSUME, 0.0), keep_min_gain=5.0)
    assert moved.kept is False
    assert moved.plan.decisions[0].action == ACTION_GRID_CHARGE
    assert moved.keep_gain is not None and moved.keep_gain > 5.0
    assert objective(moved.plan, BATTERY, moved.end_value) == pytest.approx(
        objective(fresh.plan, BATTERY, fresh.end_value)
    )


def test_a_kept_grid_charge_keeps_its_power() -> None:
    slots = _slots([20, 21, 300, 300], house=2.0)
    held = plan_battery(
        _request(slots, soc_now_pct=10, end_value=0), keep=(ACTION_GRID_CHARGE, 2.5), keep_min_gain=1e9
    )
    assert held.kept is True
    assert held.plan.decisions[0].action == ACTION_GRID_CHARGE
    assert held.plan.decisions[0].battery_kw == pytest.approx(2.5)


def test_without_a_kept_action_the_diagnostics_shape_is_unchanged() -> None:
    result = plan_battery(_request(_slots([20, 300], house=2.0)))
    assert result.kept is False and result.keep_gain is None
    assert "kept" not in result.as_dict()
