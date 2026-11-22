"""The battery plan stays under the grid charge cap control obeys (`gate.grid_charge_cap_kw`, shared through
`BatteryControl.plan_grid_charge_cap`): cap 0 plans no grid charge, a cap in between at most that, no cap of the
gate's kind (the plant's own import limit) as before, and a cap nobody knows is no grid charge. Sun charging is left
alone."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.spotnav.battery import plan_runtime
from custom_components.spotnav.battery.control import BatteryControl
from custom_components.spotnav.battery.plan import plan_battery
from custom_components.spotnav.battery.plan_model import ACTION_GRID_CHARGE, BatteryParams, PlanRequest
from custom_components.spotnav.battery.plan_runtime import BatteryPlanShadow, _solve
from custom_components.spotnav.battery.plan_shadow import (
    REASON_GRID_CHARGE_CAPPED,
    PriceSlot,
    build_slots,
    explain,
)

UTC = timezone.utc
TZ = "Europe/Stockholm"
MIDNIGHT = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
#: A SolaX-like battery: 10 kWh, 5 kW.
BATTERY = BatteryParams(usable_kwh=10, max_charge_kw=5, max_discharge_kw=5, min_soc_pct=10, max_soc_pct=100)
#: Cheap night, dear evening: a plan without a cap grid charges at night.
BUY = [20, 20, 20, 20, 300, 300, 300, 300]


def _prices() -> list[PriceSlot]:
    return [PriceSlot(MIDNIGHT + timedelta(hours=i), MIDNIGHT + timedelta(hours=i + 1), p, 40.0) for i, p in enumerate(BUY)]


def _house(kw: float = 2.0) -> dict[datetime, float]:
    return {MIDNIGHT + timedelta(hours=i): kw for i in range(len(BUY))}


def _plan(cap: float | None, *, solar_wh: dict[datetime, float] | None = None):
    slots, _ = build_slots(MIDNIGHT, _prices(), _house(), solar_wh, [], None, TZ, grid_charge_cap_kw=cap)
    request = PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=10, end_value=0)
    return slots, plan_battery(request)


def _grid_kw(slots, result) -> list[float]:
    """The battery's power from the grid per slot: its charge beyond the sun's surplus."""
    return [
        max(0.0, d.battery_kw - max(0.0, -slot.net_kw))
        for slot, d in zip(slots, result.plan.decisions, strict=True)
    ]


def test_without_a_cap_the_plan_grid_charges_in_the_cheap_hours() -> None:
    slots, result = _plan(None)

    assert any(d.action == ACTION_GRID_CHARGE and d.battery_kw > 1.0 for d in result.plan.decisions)
    assert max(_grid_kw(slots, result)) == pytest.approx(5.0)


def test_a_cap_of_zero_plans_no_grid_charge() -> None:
    slots, result = _plan(0.0)

    assert max(_grid_kw(slots, result)) == pytest.approx(0.0, abs=1e-6)
    assert all(d.action != ACTION_GRID_CHARGE or d.battery_kw <= 1e-6 for d in result.plan.decisions)


def test_a_cap_in_between_limits_every_slot_to_it() -> None:
    slots, result = _plan(2.0)

    grid = _grid_kw(slots, result)
    assert max(grid) <= 2.0 + 1e-6 and max(grid) > 1.0
    reasons = explain(result, slots)
    assert any(r["code"] == REASON_GRID_CHARGE_CAPPED for slot in reasons for r in slot)


def test_the_sun_still_charges_under_a_cap_of_zero() -> None:
    sun = {MIDNIGHT + timedelta(hours=i): 6000.0 for i in range(2)}  # 6 kW for two hours at night, a surplus of 4 kW
    slots, result = _plan(0.0, solar_wh=sun)

    assert result.plan.decisions[0].battery_kw == pytest.approx(4.0, abs=0.01)
    assert max(_grid_kw(slots, result)) == pytest.approx(0.0, abs=1e-6)


def test_the_testers_night_with_a_cap_of_zero_charges_nothing_from_the_grid() -> None:
    # 16 A fuse, conservative rule, cap 0: the plan used to charge in the cheapest quarters.
    slots, _ = build_slots(
        MIDNIGHT, [PriceSlot(MIDNIGHT + timedelta(minutes=15 * i), MIDNIGHT + timedelta(minutes=15 * (i + 1)),
                             20.0 if i < 8 else 250.0, 40.0) for i in range(16)],
        {MIDNIGHT + timedelta(hours=i): 1.5 for i in range(4)}, None, [], 10.4, TZ, grid_charge_cap_kw=0.0,
    )
    result = plan_battery(PlanRequest(slots=slots, battery=BATTERY, soc_now_pct=15, end_value=0))

    assert max(_grid_kw(slots, result)) == pytest.approx(0.0, abs=1e-6)


# ---- the cap the shell hands over -------------------------------------------------------------------------------


def _shadow(control: Any) -> BatteryPlanShadow:
    shadow = object.__new__(BatteryPlanShadow)
    shadow.site = SimpleNamespace() if control is False else SimpleNamespace(battery_control=control)
    return shadow


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class _Control:
    def __init__(self, answer: Any) -> None:
        self.answer = answer

    def plan_grid_charge_cap(self, now: datetime) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(
    ("control", "expected"),
    [
        (_Control((None, "import_limit")), (None, "import_limit")),  # the owner's Sigenergy: as before
        (_Control((3.2, "learned")), (3.2, "learned")),
        (_Control((0.0, "conservative")), (0.0, "conservative")),
        (_Control((None, "error")), (0.0, "unknown")),  # no cap known is not no cap
        (_Control((None, "conservative")), (0.0, "unknown")),
        (_Control((math.nan, "learned")), (0.0, "unknown")),
        (_Control((-1.0, "learned")), (0.0, "unknown")),
        (_Control(("2", "learned")), (0.0, "unknown")),
        (_Control(RuntimeError("boom")), (0.0, "error")),
        (False, (0.0, "unknown")),  # a site without battery control
    ],
)
def test_the_shell_fails_safe_where_the_cap_is_not_known(control: Any, expected: tuple[float | None, str]) -> None:
    assert _shadow(control)._grid_cap(NOW) == expected


def test_the_controls_cap_falls_back_to_zero_on_any_failure() -> None:
    class Broken:
        def options(self) -> Any:
            raise RuntimeError("boom")

    assert BatteryControl.plan_grid_charge_cap(Broken(), NOW) == (0.0, "error")  # type: ignore[arg-type]


def test_the_controls_cap_is_the_one_charge_limits_gives() -> None:
    seen: dict[str, Any] = {}

    class Fake:
        executor = SimpleNamespace(control=SimpleNamespace(phased=True))

        def options(self) -> Any:
            return SimpleNamespace(accept_ha_down=False)

        def _charge_limits(self, now: datetime, options: Any, *, phased: bool) -> tuple[float | None, None]:
            seen["phased"] = phased
            return 4.5, None

        def _learned(self, now: datetime) -> None:
            return None

        def _cap_rule(self, options: Any, cap: Any, failed: bool, learned: Any) -> str:
            return "conservative"

        def report(self) -> Any:
            return {}

        def _fixed_charge_kw(self) -> None:
            return None

    assert BatteryControl.plan_grid_charge_cap(Fake(), NOW) == (4.5, "conservative")  # type: ignore[arg-type]
    assert seen == {"phased": True}


# ---- the solve ---------------------------------------------------------------------------------------------------


def _solved(cap: float | None):
    return _solve(
        MIDNIGHT, TZ, _prices(), _house(), None, [], None, BATTERY, 10.0, "balanced", 0.0,
        {name: None for name in plan_runtime.REVIEWED}, grid_charge_cap_kw=cap,
    )


def test_the_solve_plans_under_the_cap_and_says_what_it_held_back() -> None:
    free = _solved(None)
    capped = _solved(0.0)

    assert free.grid_held is None
    assert capped.grid_held is not None and capped.grid_held[0] > 1.0
    plan = capped.plans["balanced"].plan
    assert all(d.action != ACTION_GRID_CHARGE or d.battery_kw <= 1e-6 for d in plan.decisions)


def test_a_cap_at_the_rated_charge_holds_nothing_back() -> None:
    assert _solved(5.0).grid_held is None


# ---- the cap is the control's total, the sun included ---------------------------------------------------------------


def _sunny_slot(cap: float | None):
    from custom_components.spotnav.battery.plan_model import PlanSlot

    # House 2 kW, sun 6 kW: a surplus of 4 kW.
    return PlanSlot(MIDNIGHT, MIDNIGHT + timedelta(hours=1), 20.0, 40.0, 2.0, solar_kw=6.0, grid_charge_cap_kw=cap)


@pytest.mark.parametrize(
    ("cap", "expected"),
    [(None, 5.0), (0.0, 4.0), (3.0, 4.0), (4.0, 4.0), (4.5, 4.5), (9.0, 5.0)],
)
def test_the_planned_charge_is_at_most_the_cap_in_all_and_never_below_the_suns_surplus(
    cap: float | None, expected: float
) -> None:
    from custom_components.spotnav.battery.plan import action_power

    # Control commands at most the cap in total (`gate.restricted`); the sun's own charge is never held back.
    assert action_power(_sunny_slot(cap), BATTERY, 5.0, ACTION_GRID_CHARGE, 5.0) == pytest.approx(expected)


# ---- where control runs no grid charge at all ------------------------------------------------------------------------


def _control(cap: float | None, *, caps: Any = None, per_phase_only: bool = False, fixed: float | None = None) -> Any:
    from custom_components.spotnav.battery import gate

    class Fake:
        executor = None

        def options(self) -> Any:
            return SimpleNamespace(accept_ha_down=False)

        def _charge_limits(self, now: datetime, options: Any, *, phased: bool) -> tuple[float | None, Any]:
            return cap, caps

        def _learned(self, now: datetime) -> None:
            return None

        def _cap_rule(self, options: Any, cap: Any, failed: bool, learned: Any) -> str:
            return "learned"

        def report(self) -> Any:
            return {"modes": {"charge": {"via": "per_phase" if per_phase_only else "direct"}}}

        def _fixed_charge_kw(self) -> float | None:
            return fixed

    assert gate.MIN_RATE_KW == 0.5
    return Fake()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"cap": 4.0}, 4.0),
        ({"cap": None}, None),
        ({"cap": 0.4}, 0.0),  # under the least rate control commands: self-consumption
        ({"cap": 0.5}, 0.5),
        ({"cap": 4.0, "fixed": 5.0}, 0.0),  # a plant that charges at its own rate, over the cap: not at all
        ({"cap": 6.0, "fixed": 5.0}, 6.0),
        ({"cap": None, "fixed": 5.0}, None),
        ({"cap": 4.0, "fixed": math.inf}, 0.0),  # its size is not known: never run
        ({"cap": None, "fixed": math.inf}, 0.0),
        ({"cap": 4.0, "per_phase_only": True}, 0.0),  # shown only per phase, no caps per phase: the balanced one never runs
        ({"cap": None, "per_phase_only": True}, 0.0),
        ({"cap": 4.0, "per_phase_only": True, "caps": (1.0, 1.0, 2.0)}, 4.0),
    ],
)
def test_the_plans_cap_is_zero_where_control_runs_no_grid_charge(kwargs: dict[str, Any], expected: float | None) -> None:
    cap, rule = BatteryControl.plan_grid_charge_cap(_control(**kwargs), NOW)  # type: ignore[arg-type]

    assert cap == expected and rule == "learned"


# ---- when a changed cap plans again ----------------------------------------------------------------------------------


def _keyed(*answers: tuple[float | None, int]) -> list[float | None]:
    """The trigger's key for a cap read at each `(cap, minutes after the first)`."""
    control = _Control((None, "import_limit"))
    shadow = _shadow(control)
    shadow._cap_seen = None
    out = []
    for cap, minutes in answers:
        control.answer = (cap, "learned") if cap is not None else (None, "import_limit")
        out.append(shadow._grid_cap_key(NOW + timedelta(minutes=minutes)))
    return out


def test_a_falling_cap_plans_again_at_once_and_a_rise_waits_a_quarter_of_an_hour() -> None:
    # 3.0 kW, a car's step down to 2.0 (at once), up to 2.5 and 3.0 within minutes (waits), later (taken).
    assert _keyed((3.0, 0), (2.0, 1), (2.5, 2), (3.0, 3), (3.0, 20)) == [3.0, 2.0, 2.0, 2.0, 3.0]


def test_a_cap_crossing_zero_or_no_cap_plans_again_at_once() -> None:
    assert _keyed((0.0, 0), (2.0, 1), (0.0, 2), (None, 3), (2.0, 4)) == [0.0, 2.0, 0.0, None, 2.0]


def test_a_cap_within_the_same_step_is_the_same_key() -> None:
    assert _keyed((3.1, 0), (3.4, 1), (3.0, 2)) == [3.0, 3.0, 3.0]
