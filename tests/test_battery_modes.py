"""The battery modes and the wear constant (`battery/plan_model.py`, `battery/plan.py`): pure."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.spotnav.battery.plan import mode_request, plan_battery, plan_mode, self_consumption
from custom_components.spotnav.battery.plan_model import (
    ACTION_EXPORT_DISCHARGE,
    BALANCED_FLOOR_PCT,
    DEFAULT_CYCLE_COST,
    DEFAULT_MODE,
    EXPORT_GATE,
    MODE_BALANCED,
    MODE_DEFS,
    MODE_MAX_PROFIT,
    MODE_SELF_CONSUMPTION,
    MODES,
    WEAR_MINOR_PER_KWH,
    BatteryParams,
    PlanInputError,
    PlanRequest,
    PlanSlot,
    mode_named,
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


def test_the_modes_are_three_and_balanced_is_the_default() -> None:
    assert MODES == (MODE_SELF_CONSUMPTION, MODE_BALANCED, MODE_MAX_PROFIT)
    assert DEFAULT_MODE == MODE_BALANCED
    assert mode_named("max_profit") is MODE_DEFS[MODE_MAX_PROFIT]
    assert mode_named("export") is None and mode_named(None) is None


def test_a_mode_request_sets_floor_wear_and_export() -> None:
    request = _request(_slots([100], [50]))
    balanced = mode_request(request, MODE_DEFS[MODE_BALANCED], 15.0)
    assert balanced.battery.min_soc_pct == BALANCED_FLOOR_PCT
    assert balanced.cycle_cost == 30.0
    assert balanced.allow_export_discharge and balanced.export_hurdle == 15.0
    profit = mode_request(request, MODE_DEFS[MODE_MAX_PROFIT], 15.0)
    assert profit.battery.min_soc_pct == BATTERY.min_soc_pct
    assert profit.cycle_cost == 15.0 and profit.allow_export_discharge and profit.export_hurdle == 0.0
    # The battery's own floor wins when it is higher than the mode's.
    high = mode_request(_request(_slots([100], [50]), battery=BatteryParams(10, 5, 5, min_soc_pct=30)),
                        MODE_DEFS[MODE_BALANCED], 15.0)
    assert high.battery.min_soc_pct == 30


def test_a_negative_hurdle_is_refused() -> None:
    with pytest.raises(PlanInputError) as err:
        plan_battery(_request(_slots([100], [50]), export_hurdle=-1))
    assert err.value.code == "invalid_export_hurdle"


# ----------------------------------------------------------------- what each mode does


def test_self_consumption_does_not_plan() -> None:
    request = _request(_slots([20, 300, 20, 300], [10, 280, 10, 280]), soc_now_pct=50)
    result = plan_mode(request, MODE_DEFS[MODE_SELF_CONSUMPTION], 15.0)
    assert result.plan is result.baseline and result.savings == 0


def test_balanced_keeps_its_reserve_and_is_measured_against_the_battery_floor() -> None:
    # Dear all day: self-consumption runs the battery to its own floor; Balanced stops at 20 %.
    request = _request(_slots([300] * 6, [10] * 6, house=2.0), soc_now_pct=60)
    result = plan_mode(request, MODE_DEFS[MODE_BALANCED], 15.0)
    assert min(d.soc_end_pct for d in result.plan.decisions) >= BALANCED_FLOOR_PCT - 1e-6
    assert result.baseline.end_soc_pct == pytest.approx(BATTERY.min_soc_pct)
    assert result.baseline == self_consumption(request)
    assert result.battery.min_soc_pct == BALANCED_FLOOR_PCT


def test_the_gate_lets_only_a_clear_spread_sell() -> None:
    # A full battery whose stored kWh is worth nothing at the end: selling pays whenever the sale clears the wear.
    # At 40 öre a kWh, Max profit sells (40 > 15 wear); Balanced does not (2 x 15 wear + 15 gate = 45).
    narrow = _request(_slots([100] * 3, [40] * 3, house=0.0))
    assert _sold_kwh(plan_mode(narrow, MODE_DEFS[MODE_MAX_PROFIT], 15.0)) > 1
    assert _sold_kwh(plan_mode(narrow, MODE_DEFS[MODE_BALANCED], 15.0)) == pytest.approx(0, abs=1e-6)
    # At 200 öre both sell, and Balanced still stops at its reserve.
    wide = _request(_slots([300] * 3, [200] * 3, house=0.0))
    profit = plan_mode(wide, MODE_DEFS[MODE_MAX_PROFIT], 15.0)
    balanced = plan_mode(wide, MODE_DEFS[MODE_BALANCED], 15.0)
    assert ACTION_EXPORT_DISCHARGE in {d.action for d in balanced.plan.decisions}
    assert _sold_kwh(balanced) > 1 and _sold_kwh(profit) > _sold_kwh(balanced)
    assert balanced.plan.end_soc_pct == pytest.approx(BALANCED_FLOOR_PCT, abs=0.5)


def test_the_gate_margin_is_weighed_never_paid() -> None:
    wide = _request(_slots([300] * 3, [200] * 3, house=0.0))
    mode = MODE_DEFS[MODE_BALANCED]
    assert mode.export == EXPORT_GATE
    balanced = plan_mode(wide, mode, 15.0)
    sold = _sold_kwh(balanced)
    assert balanced.plan.hurdle == pytest.approx(sold * 15.0, rel=1e-6)
    assert balanced.plan.cost == pytest.approx(sum(d.cost for d in balanced.plan.decisions))
    assert balanced.as_dict()["export_hurdle"] == pytest.approx(round(sold * 15.0, 2))


def test_more_wear_cycles_less() -> None:
    buy = [60, 120] * 6
    request = _request(_slots(buy, [b - 60 for b in buy], house=1.0), soc_now_pct=20, end_value=80)

    def throughput(wear: float) -> float:
        plan = plan_mode(request, MODE_DEFS[MODE_MAX_PROFIT], wear)
        return sum(abs(d.battery_kw) for d in plan.plan.decisions)

    assert throughput(40.0) <= throughput(15.0) <= throughput(0.0)
    assert throughput(40.0) < throughput(0.0)
