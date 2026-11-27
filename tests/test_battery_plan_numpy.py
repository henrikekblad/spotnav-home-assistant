"""The battery plan's search on numpy (`battery/plan.py`, `_values_to_go_numpy`) against the same search point by point.

The two must give the same value-to-go at every grid point of every slot, as the same floats: the plan a site gets must
not depend on whether its Python has numpy. Random horizons with every bound the arithmetic branches on.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery import plan as plan_module
from custom_components.spotnav.battery.plan import _Grid, _reserves, _values_to_go, plan_battery
from custom_components.spotnav.battery.plan_model import BatteryParams, PlanRequest, PlanSlot

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _random_request(seed: int) -> PlanRequest:
    rng = random.Random(seed)
    minutes = rng.choice([15, 60])
    count = rng.randint(1, 60)
    step = timedelta(minutes=minutes)
    import_cap = rng.choice([None, None, rng.uniform(2, 12)])
    export_cap = rng.choice([None, None, 0.0, rng.uniform(1, 8)])
    slots = []
    for i in range(count):
        buy = rng.choice([rng.uniform(-30, 400), 100.0])
        slots.append(
            PlanSlot(
                start=T0 + step * i,
                end=T0 + step * (i + 1),
                buy=buy,
                sell=buy - rng.choice([60.0, 0.0, rng.uniform(0, 150)]),
                house_kw=rng.choice([0.0, rng.uniform(0, 6)]),
                solar_kw=rng.choice([0.0, 0.0, rng.uniform(0, 14)]),
                car_kw=rng.choice([0.0, 0.0, 0.0, 11.0]),
                import_cap_kw=rng.choice([import_cap, import_cap, None]),
                export_cap_kw=rng.choice([export_cap, export_cap, None]),
                grid_charge_cap_kw=rng.choice([None, None, None, 0.0, rng.uniform(0, 6)]),
            )
        )
    dc_bus = rng.random() < 0.5
    battery = BatteryParams(
        usable_kwh=rng.uniform(3, 40),
        max_charge_kw=rng.choice([0.0, rng.uniform(1, 13)]),
        max_discharge_kw=rng.choice([0.0, rng.uniform(1, 13)]),
        min_soc_pct=rng.choice([0.0, 10.0, rng.uniform(0, 40)]),
        max_soc_pct=rng.choice([100.0, rng.uniform(60, 100)]),
        round_trip_efficiency=rng.uniform(0.7, 1.0),
        sun_round_trip_efficiency=rng.uniform(0.9, 1.0) if dc_bus else None,
        discharge_loss=rng.uniform(0, 0.1) if dc_bus else 0.0,
    )
    sells = rng.random() < 0.7
    return PlanRequest(
        slots=tuple(slots),
        battery=battery,
        soc_now_pct=rng.choice([0.0, 100.0, rng.uniform(0, 100)]),
        # The default end value prices a refill at the rated charge power, so it needs one.
        end_value=rng.choice([None if battery.max_charge_kw else 50.0, 0.0, rng.uniform(0, 200)]),
        cycle_cost=rng.choice([0.0, 15.0, rng.uniform(0, 40)]),
        allow_export_discharge=sells,
        export_hurdle=rng.choice([0.0, 20.0]) if sells else 0.0,
        export_reserve=sells and rng.random() < 0.5,
        soc_steps=rng.choice([2, 7, 40, 100]),
        charge_shares=rng.choice([(0.25, 0.5, 0.75, 1.0), (1.0,), ()]),
    )


def _values(request: PlanRequest) -> list[list[float]]:
    grid = _Grid(request.battery, request.soc_steps)
    end_value = 37.5 if request.end_value is None else request.end_value
    return _values_to_go(request, end_value, grid, _reserves(request))


@pytest.mark.parametrize("seed", range(300))
def test_numpy_gives_the_same_values_and_plan_as_point_by_point(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("numpy")
    request = _random_request(seed)
    monkeypatch.setattr(plan_module, "_NUMPY", [])
    fast = _values(request)
    planned = plan_battery(request, keep=("grid_charge", 2.0), keep_min_gain=3.0)
    assert plan_module._NUMPY[0] is not None  # noqa: SLF001 - the numpy path was the one run
    monkeypatch.setattr(plan_module, "_NUMPY", [None])
    slow = _values(request)
    assert all(type(value) is float for row in fast for value in row)
    assert fast == slow
    assert planned == plan_battery(request, keep=("grid_charge", 2.0), keep_min_gain=3.0)


def test_a_python_without_numpy_still_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def no_numpy(name: str, *args: object, **kwargs: object) -> object:
        if name == "numpy":
            raise ImportError("No module named 'numpy'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(plan_module, "_NUMPY", [])
    monkeypatch.setattr(builtins, "__import__", no_numpy)
    request = _random_request(1)
    planned = plan_battery(request)
    assert plan_module._NUMPY == [None]  # noqa: SLF001 - looked for once, not found
    monkeypatch.setattr(builtins, "__import__", real_import)
    monkeypatch.setattr(plan_module, "_NUMPY", [])
    assert planned == plan_battery(request)
