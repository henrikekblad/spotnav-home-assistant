"""The battery modes and the wear constant (`battery/plan_model.py`, `battery/plan.py`): pure."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.spotnav.battery.plan import (
    action_power,
    house_reserve_kwh,
    mode_request,
    plan_battery,
    plan_mode,
    self_consumption,
)
from custom_components.spotnav.battery.plan_model import (
    ACTION_EXPORT_DISCHARGE,
    DEFAULT_CYCLE_COST,
    DEFAULT_MODE,
    DEFAULT_SELL_MARGIN,
    EXPORT_GATE,
    MODE_BALANCED,
    MODE_DEFS,
    MODE_MAX_PROFIT,
    MODE_SELF_CONSUMPTION,
    MODES,
    SELL_MARGIN_MAJOR,
    WEAR_MINOR_PER_KWH,
    BatteryParams,
    PlanInputError,
    PlanRequest,
    PlanSlot,
    mode_named,
    sell_margin_minor_per_kwh,
    wear_minor_per_kwh,
)

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
BATTERY = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=5, max_soc_pct=100,
                        round_trip_efficiency=0.9)
AREAS = Path(__file__).resolve().parents[1] / "custom_components" / "spotnav" / "pricing" / "areas.json"


def _slots(buy: list[float], sell: list[float], house: float = 0.5) -> tuple[PlanSlot, ...]:
    step = timedelta(hours=1)
    return tuple(
        PlanSlot(start=T0 + step * i, end=T0 + step * (i + 1), buy=b, sell=s, house_kw=house)
        for i, (b, s) in enumerate(zip(buy, sell))
    )


def _request(slots: tuple[PlanSlot, ...], **kwargs) -> PlanRequest:
    kwargs.setdefault("battery", BATTERY)
    kwargs.setdefault("soc_now_pct", 100)
    kwargs.setdefault("end_value", 0)
    return PlanRequest(slots=slots, **kwargs)


def _sold_kwh(plan) -> float:
    return sum(max(0.0, -d.grid_kw) * (d.end - d.start).total_seconds() / 3600 for d in plan.plan.decisions)


# ----------------------------------------------------------------- the constants


def test_every_area_currency_has_a_wear_figure() -> None:
    areas = json.loads(AREAS.read_text())["areas"]
    currencies = {area["currency"] for area in (areas.values() if isinstance(areas, dict) else areas)}
    assert currencies <= set(WEAR_MINOR_PER_KWH)
    assert wear_minor_per_kwh("sek") == 15.0 == DEFAULT_CYCLE_COST
    assert wear_minor_per_kwh("XYZ") is None and wear_minor_per_kwh(None) is None


def test_every_area_currency_has_a_sell_margin_of_about_one_krona() -> None:
    areas = json.loads(AREAS.read_text())["areas"]
    currencies = {area["currency"] for area in (areas.values() if isinstance(areas, dict) else areas)}
    assert currencies <= set(SELL_MARGIN_MAJOR)
    assert sell_margin_minor_per_kwh("sek") == 100.0 == DEFAULT_SELL_MARGIN
    assert sell_margin_minor_per_kwh("NOK") == 100.0
    assert sell_margin_minor_per_kwh("DKK") == pytest.approx(70.0)
    assert sell_margin_minor_per_kwh("EUR") == pytest.approx(10.0)
    assert sell_margin_minor_per_kwh("XYZ") == DEFAULT_SELL_MARGIN and sell_margin_minor_per_kwh(None) == DEFAULT_SELL_MARGIN
    # The same money everywhere: about 6-7 times the currency's wear.
    for currency, wear in WEAR_MINOR_PER_KWH.items():
        assert 5 <= sell_margin_minor_per_kwh(currency) / wear <= 9, currency


def test_the_modes_are_three_and_balanced_is_the_default() -> None:
    assert MODES == (MODE_SELF_CONSUMPTION, MODE_BALANCED, MODE_MAX_PROFIT)
    assert DEFAULT_MODE == MODE_BALANCED
    assert mode_named("max_profit") is MODE_DEFS[MODE_MAX_PROFIT]
    assert mode_named("export") is None and mode_named(None) is None


def test_a_mode_request_sets_wear_and_export_and_keeps_the_batterys_floor() -> None:
    request = _request(_slots([100], [50]))
    balanced = mode_request(request, MODE_DEFS[MODE_BALANCED], 15.0)
    assert balanced.cycle_cost == 30.0
    assert balanced.allow_export_discharge and balanced.export_hurdle == DEFAULT_SELL_MARGIN and balanced.export_reserve
    assert mode_request(request, MODE_DEFS[MODE_BALANCED], 10.0, 70.0).export_hurdle == 70.0, "the area's margin"
    profit = mode_request(request, MODE_DEFS[MODE_MAX_PROFIT], 15.0)
    assert profit.cycle_cost == 15.0 and profit.allow_export_discharge and profit.export_hurdle == 0.0
    assert not profit.export_reserve
    own = mode_request(request, MODE_DEFS[MODE_SELF_CONSUMPTION], 15.0)
    assert not own.allow_export_discharge and own.export_hurdle == 0.0
    # The modes differ in arbitrage only: every mode plans the battery as it is, its own floor included.
    high = _request(_slots([100], [50]), battery=BatteryParams(10, 5, 5, min_soc_pct=30))
    for mode in MODE_DEFS.values():
        assert mode_request(request, mode, 15.0).battery is BATTERY
        assert mode_request(high, mode, 15.0).battery.min_soc_pct == 30


def test_a_negative_hurdle_is_refused() -> None:
    with pytest.raises(PlanInputError) as err:
        plan_battery(_request(_slots([100], [50]), export_hurdle=-1))
    assert err.value.code == "invalid_export_hurdle"


# ----------------------------------------------------------------- what each mode does


def test_self_consumption_does_not_plan() -> None:
    request = _request(_slots([20, 300, 20, 300], [10, 280, 10, 280]), soc_now_pct=50)
    result = plan_mode(request, MODE_DEFS[MODE_SELF_CONSUMPTION], 15.0)
    assert result.plan is result.baseline and result.savings == 0


def test_every_mode_spends_down_to_the_batterys_own_floor_and_no_further() -> None:
    # Dear all day: Balanced, like self-consumption, runs the battery to its own floor; a higher floor read from the
    # battery holds for every mode.
    request = _request(_slots([300] * 6, [10] * 6, house=2.0), soc_now_pct=60)
    result = plan_mode(request, MODE_DEFS[MODE_BALANCED], 15.0)
    assert result.plan.end_soc_pct == pytest.approx(BATTERY.min_soc_pct)
    assert result.baseline.end_soc_pct == pytest.approx(BATTERY.min_soc_pct)
    assert result.baseline == self_consumption(request)
    assert result.battery == BATTERY
    high = _request(_slots([300] * 6, [10] * 6, house=2.0), soc_now_pct=60,
                    battery=BatteryParams(10, 5, 5, min_soc_pct=30))
    for mode in MODE_DEFS.values():
        ran = plan_mode(high, mode, 15.0)
        assert min(d.soc_end_pct for d in ran.plan.decisions) >= 30 - 1e-6
        assert ran.plan.end_soc_pct == pytest.approx(30)


def test_the_gate_lets_only_a_clear_spread_sell() -> None:
    # A full battery whose stored kWh is worth nothing at the end: selling pays whenever the sale clears the wear.
    # At 40 öre a kWh, Max profit sells (40 > 15 wear); Balanced does not (2 x 15 wear + 100 margin = 130).
    narrow = _request(_slots([100] * 3, [40] * 3, house=0.0))
    assert _sold_kwh(plan_mode(narrow, MODE_DEFS[MODE_MAX_PROFIT], 15.0)) > 1
    assert _sold_kwh(plan_mode(narrow, MODE_DEFS[MODE_BALANCED], 15.0)) == pytest.approx(0, abs=1e-6)
    # A modest peak, 1.20 kr: still short of Balanced's 1.30, while Max profit sells it.
    modest = _request(_slots([150] * 3, [120] * 3, house=0.0))
    assert _sold_kwh(plan_mode(modest, MODE_DEFS[MODE_MAX_PROFIT], 15.0)) > 1
    assert _sold_kwh(plan_mode(modest, MODE_DEFS[MODE_BALANCED], 15.0)) == pytest.approx(0, abs=1e-6)
    # At 200 öre the spread clears the gate: both sell the battery down to its own floor.
    wide = _request(_slots([300] * 3, [200] * 3, house=0.0))
    profit = plan_mode(wide, MODE_DEFS[MODE_MAX_PROFIT], 15.0)
    balanced = plan_mode(wide, MODE_DEFS[MODE_BALANCED], 15.0)
    assert ACTION_EXPORT_DISCHARGE in {d.action for d in balanced.plan.decisions}
    assert _sold_kwh(balanced) > 1 and _sold_kwh(balanced) == pytest.approx(_sold_kwh(profit))
    assert balanced.plan.end_soc_pct == pytest.approx(BATTERY.min_soc_pct, abs=0.5)


def test_the_gate_margin_is_weighed_never_paid() -> None:
    wide = _request(_slots([300] * 3, [200] * 3, house=0.0))
    mode = MODE_DEFS[MODE_BALANCED]
    assert mode.export == EXPORT_GATE
    balanced = plan_mode(wide, mode, 15.0)
    sold = _sold_kwh(balanced)
    assert balanced.plan.hurdle == pytest.approx(sold * DEFAULT_SELL_MARGIN, rel=1e-6)
    assert balanced.plan.cost == pytest.approx(sum(d.cost for d in balanced.plan.decisions))
    assert balanced.as_dict()["export_hurdle"] == pytest.approx(round(sold * DEFAULT_SELL_MARGIN, 2))


def _peak_evening(peak_sell: float) -> PlanRequest:
    """A full battery, an hour's price peak now, five expensive evening hours with 1 kW in the house, then a cheap
    night."""
    buy = [peak_sell + 100, *[300] * 5, *[50] * 6]
    sell = [peak_sell, *[100] * 5, *[30] * 6]
    return PlanRequest(slots=_slots(buy, sell, house=1.0), battery=BATTERY, soc_now_pct=100)


def test_balanced_sells_a_10_kr_peak_but_keeps_the_house_until_the_cheap_hours() -> None:
    request = _peak_evening(1000)
    reserve = house_reserve_kwh(request.slots, BATTERY)
    assert reserve[0] == pytest.approx(5 / BATTERY.one_way_efficiency), "the five evening hours, not the night"
    assert reserve[5] == 0.0 and reserve[-1] == 0.0
    balanced = plan_mode(request, MODE_DEFS[MODE_BALANCED], 15.0)
    first = balanced.plan.decisions[0]
    assert first.action == ACTION_EXPORT_DISCHARGE and first.grid_kw < -1
    # The surplus only: what is left after the peak covers the evening.
    left = first.soc_end_pct / 100 * BATTERY.usable_kwh
    assert left >= BATTERY.min_kwh + reserve[0] - 1e-6
    assert all(d.grid_kw <= 1e-6 for d in balanced.plan.decisions[1:6]), "the evening is not bought"
    # Max profit sells more of it and buys the evening back.
    profit = plan_mode(request, MODE_DEFS[MODE_MAX_PROFIT], 15.0)
    assert _sold_kwh(profit) > _sold_kwh(balanced) + 0.5


def test_balanced_does_not_sell_a_modest_peak_that_max_profit_sells() -> None:
    # The surplus beyond the evening is refilled at night (50 öre) and is worth the end value at the horizon (157):
    # at 2.00 kr a sale clears that and the wear, but not by Balanced's 1 kr more; Max profit sells it.
    request = _peak_evening(200)
    assert _sold_kwh(plan_mode(request, MODE_DEFS[MODE_BALANCED], 15.0)) == pytest.approx(0, abs=1e-6)
    assert _sold_kwh(plan_mode(request, MODE_DEFS[MODE_MAX_PROFIT], 15.0)) > 1


def test_self_consumption_never_sells_from_the_battery() -> None:
    result = plan_mode(_peak_evening(1000), MODE_DEFS[MODE_SELF_CONSUMPTION], 15.0)
    assert ACTION_EXPORT_DISCHARGE not in {d.action for d in result.plan.decisions}
    assert _sold_kwh(result) == 0


def test_a_replayed_export_runs_at_most_its_planned_power() -> None:
    slot = _slots([100], [50], house=1.0)[0]
    assert action_power(slot, BATTERY, 9.0, ACTION_EXPORT_DISCHARGE) == -5.0
    assert action_power(slot, BATTERY, 9.0, ACTION_EXPORT_DISCHARGE, 3.0) == -3.0
    assert action_power(slot, BATTERY, 9.0, ACTION_EXPORT_DISCHARGE, -3.0) == -3.0, "a signed plan kW too"
    assert action_power(slot, BATTERY, 9.0, ACTION_EXPORT_DISCHARGE, 8.0) == -5.0


def test_more_wear_cycles_less() -> None:
    buy = [60, 120] * 6
    request = _request(_slots(buy, [b - 60 for b in buy], house=1.0), soc_now_pct=20, end_value=80)

    def throughput(wear: float) -> float:
        plan = plan_mode(request, MODE_DEFS[MODE_MAX_PROFIT], wear)
        return sum(abs(d.battery_kw) for d in plan.plan.decisions)

    assert throughput(40.0) <= throughput(15.0) <= throughput(0.0)
    assert throughput(40.0) < throughput(0.0)
