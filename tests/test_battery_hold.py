"""The shadow "hold the house battery while the car charges from the grid" policy: decisions only."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.spotnav.battery.adapters import SIGENERGY, VICTRON, GOODWE
from custom_components.spotnav.battery.facts import BatteryFacts, ControlStatus, Management
from custom_components.spotnav.battery.hold import (
    ARM_S,
    DAILY_WRITE_CAP,
    REARM_S,
    HoldInputs,
    HoldMemory,
    restart,
    restore_value,
    step,
)

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LIMIT = "number.sigen_plant_ess_max_discharging_limit"


def _control(current: str = "23.04", *, unset: bool = True, enabled: bool = True) -> ControlStatus:
    assert SIGENERGY.hold is not None
    return ControlStatus(
        SIGENERGY.hold, LIMIT, found=True, enabled=enabled, available=True, current=current,
        current_kw=float(current), unset=unset,
    )


def _facts(power_kw: float | None = -4.6, *, management: str = "free", **changes) -> BatteryFacts:
    facts = BatteryFacts(
        battery_entity_id="sensor.sigen_plant_battery_power",
        integration="sigen",
        brand="sigenergy",
        power_kw=power_kw,
        rated_discharge_kw=23.04,
        on_grid=True,
        management=Management(management, "self_consumption" if management == "free" else "EMS work mode:ai_mode"),
        hold=_control(),
    )
    return replace(facts, **changes)


def _inputs(at: datetime, **changes) -> HoldInputs:
    base = HoldInputs(
        now=at, grid_charging=True, facts=_facts(), battery_age_s=5.0, measurement_ok=True, headroom_a=12.0,
        charger_ids=("charger_a",), car_kw=7.0,
    )
    return replace(base, **changes)


def _run(memory: HoldMemory, *inputs: HoldInputs):
    decisions = []
    for item in inputs:
        memory, decision = step(memory, item)
        if decision is not None:
            decisions.append(decision)
    return memory, decisions


def _armed_hold() -> tuple[HoldMemory, list]:
    return _run(HoldMemory(), _inputs(T0), _inputs(T0 + timedelta(seconds=ARM_S)))


def test_a_discharge_into_a_grid_charge_would_set_the_limit_to_zero_after_it_lasts() -> None:
    memory, decisions = _run(HoldMemory(), _inputs(T0), _inputs(T0 + timedelta(seconds=30)))
    assert decisions == [] and not memory.held

    memory, [decision] = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S)))

    assert decision.action == "would_write" and decision.entity_id == LIMIT and decision.value == 0.0
    assert decision.read == "23.04" and decision.detail["snapshot_unset"] is True
    assert memory.held and memory.snapshot == "23.04" and memory.writes_today == 1


def test_a_short_dip_does_not_arm() -> None:
    _memory, decisions = _run(
        HoldMemory(),
        _inputs(T0),
        _inputs(T0 + timedelta(seconds=30), facts=_facts(0.0)),
        _inputs(T0 + timedelta(seconds=ARM_S)),
    )

    assert decisions == []


def test_a_restore_writes_the_rating_for_an_unset_limit_when_the_car_stops() -> None:
    memory, _ = _armed_hold()

    memory, [decision] = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S + 10), grid_charging=False))

    assert decision.action == "would_restore" and decision.reason == "car_not_charging_from_grid"
    assert decision.value == 23.04 and not memory.held and memory.writes_today == 2


def test_a_real_limit_is_restored_as_it_was() -> None:
    facts = _facts(hold=_control("4.8", unset=False))
    memory, _ = _run(HoldMemory(), _inputs(T0, facts=facts), _inputs(T0 + timedelta(seconds=ARM_S), facts=facts))

    _memory, [decision] = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S + 5), grid_charging=False, facts=facts))

    assert decision.value == 4.8


def test_victron_no_limit_is_restored_as_minus_one() -> None:
    assert VICTRON.hold is not None
    control = ControlStatus(VICTRON.hold, "number.v", True, True, True, "-1", -0.001, True)
    memory = HoldMemory(held=True, snapshot="-1", snapshot_unset=True)

    assert restore_value(memory, control, None) == -1.0


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"facts": _facts(on_grid=False)}, "off_grid"),
        ({"battery_age_s": 600.0}, "battery_reading_stale"),
        ({"facts": _facts(3.0)}, "battery_charging"),
        ({"measurement_ok": False}, "measurement_lost"),
        ({"headroom_a": 0.5}, "headroom_low"),
        ({"facts": _facts(management="self_managed")}, "self_managed:EMS work mode:ai_mode"),
        ({"facts": _facts(hold=_control(enabled=False))}, "control_disabled"),
    ],
)
def test_each_restore_trigger_releases_at_once(changes: dict, reason: str) -> None:
    memory, _ = _armed_hold()

    memory, [decision] = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S + 5), **changes))

    assert decision.action == "would_restore" and decision.reason == reason and not memory.held


def test_a_new_hold_waits_after_a_restore() -> None:
    memory, _ = _armed_hold()
    released_at = T0 + timedelta(seconds=ARM_S + 5)
    memory, _ = _run(memory, _inputs(released_at, grid_charging=False))

    later = released_at + timedelta(seconds=10)
    memory, decisions = _run(memory, _inputs(later), _inputs(later + timedelta(seconds=ARM_S)))
    assert decisions == []

    _memory, [decision] = _run(memory, _inputs(released_at + timedelta(seconds=REARM_S + 1)))
    assert decision.action == "would_write"


def test_a_battery_that_runs_itself_is_left_alone_once_per_grid_charge() -> None:
    facts = _facts(management="self_managed")
    steps = [_inputs(T0 + timedelta(seconds=s), facts=facts) for s in (0, ARM_S, ARM_S + 10, ARM_S + 20)]

    memory, decisions = _run(HoldMemory(), *steps)

    [decision] = decisions
    assert decision.action == "left_alone" and decision.reason == "self_managed:EMS work mode:ai_mode"
    assert decision.detail["would_write"] == 0.0 and decision.entity_id == LIMIT
    assert not memory.held and memory.writes_today == 0

    # The next grid charge says it again.
    memory, _ = _run(memory, _inputs(T0 + timedelta(seconds=200), grid_charging=False))
    _memory, again = _run(
        memory,
        _inputs(T0 + timedelta(seconds=300), facts=facts),
        _inputs(T0 + timedelta(seconds=300 + ARM_S), facts=facts),
    )
    assert [d.action for d in again] == ["left_alone"]


def test_a_charging_battery_is_never_held() -> None:
    facts = _facts(2.5)
    memory, decisions = _run(HoldMemory(), *[_inputs(T0 + timedelta(seconds=s), facts=facts) for s in (0, 60, 120, 180)])

    assert [(d.action, d.reason) for d in decisions] == [("not_possible", "battery_charging")]
    assert not memory.held


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"facts": _facts(hold=None)}, "no_hold_control"),
        ({"facts": _facts(hold=_control(enabled=False))}, "control_disabled"),
        ({"facts": _facts(on_grid=False)}, "off_grid"),
        ({"measurement_ok": False}, "measurement_lost"),
        ({"headroom_a": None}, "headroom_unknown"),
        # 4.6 kW is 6.6 A per phase at 400 V: 8 A of headroom would leave 1.4 A.
        ({"headroom_a": 8.0}, "headroom_too_small"),
    ],
)
def test_a_hold_that_is_not_possible_says_why(changes: dict, reason: str) -> None:
    memory, decisions = _run(HoldMemory(), _inputs(T0, **changes), _inputs(T0 + timedelta(seconds=ARM_S), **changes))

    assert [(d.action, d.reason) for d in decisions] == [("not_possible", reason)] and not memory.held


def test_goodwe_has_no_hold_to_offer() -> None:
    assert GOODWE.hold is None


def test_the_daily_write_cap() -> None:
    memory = HoldMemory(day=T0.date().isoformat(), writes_today=DAILY_WRITE_CAP)

    _memory, decisions = _run(memory, _inputs(T0), _inputs(T0 + timedelta(seconds=ARM_S)))

    assert [d.reason for d in decisions] == ["daily_write_cap"]


def test_the_count_starts_again_each_day() -> None:
    memory = HoldMemory(day="2026-11-12", writes_today=DAILY_WRITE_CAP)

    _memory, decisions = _run(memory, _inputs(T0), _inputs(T0 + timedelta(seconds=ARM_S)))

    assert [d.action for d in decisions] == ["would_write"]


def test_a_restart_restores_first() -> None:
    memory, _ = _armed_hold()
    stored = HoldMemory.from_dict(json.loads(json.dumps(memory.as_dict())))
    assert stored == memory

    released, decision = restart(stored, _facts(), T0 + timedelta(hours=1))

    assert decision is not None and decision.action == "would_restore" and decision.reason == "restart"
    assert decision.value == 23.04 and not released.held
    assert restart(released, _facts(), T0 + timedelta(hours=1)) == (released, None)


def test_the_kwh_the_battery_gave_the_car_is_counted_per_day_up_to_the_cars_draw() -> None:
    memory, _ = _run(
        HoldMemory(),
        # 4.6 kW discharge, the car draws 3 kW: 3 kW for 30 s, then 4.6 kW (car 7 kW) for 30 s.
        _inputs(T0, car_kw=3.0),
        _inputs(T0 + timedelta(seconds=30), car_kw=3.0),
        _inputs(T0 + timedelta(seconds=60)),
        # A gap longer than a minute adds nothing; no grid charge adds nothing.
        _inputs(T0 + timedelta(seconds=600)),
        _inputs(T0 + timedelta(seconds=630), grid_charging=False),
    )

    assert memory.gave_car_kwh == {"2026-11-13": pytest.approx((3.0 * 30 + 4.6 * 30) / 3600)}


def test_broken_memory_loads_empty() -> None:
    assert HoldMemory.from_dict({"held": "yes", "writes_today": -3, "gave_car_kwh": {"d": "x"}, "noted": "x"}) == HoldMemory()
    assert HoldMemory.from_dict(None) == HoldMemory()
