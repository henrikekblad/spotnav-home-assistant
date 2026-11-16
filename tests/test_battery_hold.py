"""The shadow "hold the house battery while the car charges from the grid" policy: decisions only. What a hold
would write and hand back is the executor's action `hold` (`executor.preview`), not a write of the part's own."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL
from custom_components.spotnav.battery.executor import DAILY_WRITE_CAP, REARM_S, RoleRead
from custom_components.spotnav.battery.facts import BatteryFacts, Management
from custom_components.spotnav.battery.hold import (
    ARM_S,
    HoldInputs,
    HoldMemory,
    requested_action,
    restart,
    step,
)
from tests.battery_control_fakes import SIGENERGY_STATES, FakeBattery

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
LIMIT = "number.sigenergy_discharge_limit"
HOLD_WRITES = {"discharge_limit": 0.0, "remote_ems": True, "control_mode": "Maximum Self Consumption"}


def _reads(**states: str) -> dict[str, RoleRead]:
    return FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES) | states).reads()


def _facts(power_kw: float | None = -4.6, *, management: str = "free", **changes) -> BatteryFacts:
    facts = BatteryFacts(
        battery_entity_id="sensor.sigen_plant_battery_power",
        integration="sigen",
        brand="sigenergy",
        power_kw=power_kw,
        rated_charge_kw=23.04,
        rated_discharge_kw=23.04,
        on_grid=True,
        management=(
            Management("free", "self_consumption", {"EMS work mode": "max_self_consumption"}) if management == "free"
            else Management(management, "EMS work mode:ai_mode", {"EMS work mode": "ai_mode"})
        ),
    )
    return replace(facts, **changes)


def _inputs(at: datetime, **changes) -> HoldInputs:
    base = HoldInputs(
        now=at, grid_charging=True, facts=_facts(), battery_age_s=5.0, measurement_ok=True, headroom_a=25.0,
        charger_ids=("charger_a",), car_kw=7.0, reads=_reads(),
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


def test_a_discharge_into_a_grid_charge_would_run_the_executors_hold_after_it_lasts() -> None:
    memory, decisions = _run(HoldMemory(), _inputs(T0), _inputs(T0 + timedelta(seconds=30)))
    assert decisions == [] and not memory.held and requested_action(memory) is None

    memory, [decision] = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S)))

    assert decision.action == "would_write" and decision.entity_id == LIMIT and decision.value == HOLD_WRITES
    assert decision.read == "23.04"
    assert [(row["role"], row["value"]) for row in decision.detail["restore"]] == [
        ("discharge_limit", 23.04), ("control_mode", "Maximum Self Consumption"), ("remote_ems", False)
    ]
    assert memory.held and memory.snapshot == {"discharge_limit": "23.04"} and memory.writes_today == 3
    assert requested_action(memory) == "hold"


def test_a_short_dip_does_not_arm() -> None:
    _memory, decisions = _run(
        HoldMemory(),
        _inputs(T0),
        _inputs(T0 + timedelta(seconds=30), facts=_facts(0.0)),
        _inputs(T0 + timedelta(seconds=ARM_S)),
    )

    assert decisions == []


def test_a_restore_hands_back_what_the_executor_wrote_when_the_car_stops() -> None:
    memory, _ = _armed_hold()

    memory, [decision] = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S + 10), grid_charging=False))

    assert decision.action == "would_restore" and decision.reason == "car_not_charging_from_grid"
    assert decision.value == {"control_mode": "Maximum Self Consumption", "remote_ems": False, "discharge_limit": 23.04}
    assert not memory.held and memory.writes_today == 3, "a restore is not counted, as the executor counts"


def test_a_sigenergy_limit_that_reads_unknown_is_restored_as_the_rating_never_as_unknown() -> None:
    reads = _reads(discharge_limit="unknown")
    memory, [decision] = _run(HoldMemory(), _inputs(T0, reads=reads), _inputs(T0 + timedelta(seconds=ARM_S), reads=reads))
    assert decision.action == "would_write" and decision.read == "unknown"
    assert memory.restore["discharge_limit"] == 23.04 and memory.snapshot == {"discharge_limit": "unknown"}

    _memory, [back] = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S + 5), grid_charging=False, reads=reads))

    assert back.value["discharge_limit"] == 23.04
    assert "unknown" not in back.value.values()


def test_a_brand_without_executor_commands_cannot_be_held_even_in_shadow() -> None:
    facts = _facts(brand="victron", integration="victron_gx")
    memory, decisions = _run(HoldMemory(), _inputs(T0, facts=facts, reads={}), _inputs(T0 + timedelta(seconds=ARM_S), facts=facts, reads={}))

    assert [(d.action, d.reason) for d in decisions] == [("not_possible", "no_brand_control")] and not memory.held


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"facts": _facts(on_grid=False)}, "off_grid"),
        ({"battery_age_s": 600.0}, "battery_reading_stale"),
        ({"facts": _facts(3.0)}, "battery_charging"),
        ({"measurement_ok": False}, "measurement_lost"),
        ({"headroom_a": 0.5}, "headroom_low"),
        ({"facts": _facts(management="self_managed")}, "self_managed:EMS work mode:ai_mode"),
        ({"reads": {}}, "control_not_found:discharge_limit"),
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
    assert decision.detail["would_write"]["discharge_limit"] == 0.0 and decision.entity_id == LIMIT
    assert not memory.held and memory.writes_today == 0

    # The next grid charge says it again.
    memory, _ = _run(memory, _inputs(T0 + timedelta(seconds=200), grid_charging=False))
    _memory, again = _run(
        memory,
        _inputs(T0 + timedelta(seconds=300), facts=facts),
        _inputs(T0 + timedelta(seconds=300 + ARM_S), facts=facts),
    )
    assert [d.action for d in again] == ["left_alone"]


def test_a_battery_in_ai_mode_is_held_when_the_person_acknowledged_the_takeover() -> None:
    facts = _facts(management="self_managed")
    memory, decisions = _run(
        HoldMemory(),
        _inputs(T0, facts=facts, takeover=True),
        _inputs(T0 + timedelta(seconds=ARM_S), facts=facts, takeover=True),
    )
    assert [d.action for d in decisions] == ["would_write"]
    assert memory.held and requested_action(memory) == "hold"
    # Held under the takeover, the AI mode reading is no reason to let go.
    memory, decisions = _run(memory, _inputs(T0 + timedelta(seconds=ARM_S + 10), facts=facts, takeover=True))
    assert decisions == [] and memory.held


def test_a_charging_battery_is_never_held() -> None:
    facts = _facts(2.5)
    memory, decisions = _run(HoldMemory(), *[_inputs(T0 + timedelta(seconds=s), facts=facts) for s in (0, 60, 120, 180)])

    assert [(d.action, d.reason) for d in decisions] == [("not_possible", "battery_charging")]
    assert not memory.held


def _without(role: str) -> dict[str, RoleRead]:
    return {name: read for name, read in _reads().items() if name != role}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        # The executor's own refusals, as its preview gives them.
        ({"reads": _without("remote_ems")}, "control_not_found:remote_ems"),
        ({"reads": _reads() | {"remote_ems": replace(_reads()["remote_ems"], enabled=False)}}, "control_disabled:remote_ems"),
        ({"reads": _reads(remote_ems="on")}, "remote_ems_already_on"),
        ({"reads": _reads(discharge_limit="unavailable")}, "no_restore_value:discharge_limit"),
        ({"facts": _facts(on_grid=False)}, "off_grid"),
        ({"measurement_ok": False}, "stale_data"),
        ({"headroom_a": None}, "headroom_unknown"),
        # 4.6 kW can all be on one phase: 19.9 A at 230 V. 20 A of headroom would leave 0.1 A (spread over three
        # phases it would have left 13.4 A).
        ({"headroom_a": 20.0}, "headroom_too_small"),
    ],
)
def test_a_hold_that_is_not_possible_says_why(changes: dict, reason: str) -> None:
    memory, decisions = _run(HoldMemory(), _inputs(T0, **changes), _inputs(T0 + timedelta(seconds=ARM_S), **changes))

    assert [(d.action, d.reason) for d in decisions] == [("not_possible", reason)] and not memory.held


def test_the_daily_write_cap_is_the_executors() -> None:
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
    assert decision.value == {"control_mode": "Maximum Self Consumption", "remote_ems": False, "discharge_limit": 23.04}
    assert not released.held
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


def test_an_earlier_versions_single_snapshot_loads_as_none() -> None:
    memory = HoldMemory.from_dict({"held": True, "snapshot": "4.8", "snapshot_unset": False})

    assert memory.held and memory.snapshot == {} and memory.restore == {}


def _remote_ems(state: str = "self_managed", mode: str = "remote_ems") -> BatteryFacts:
    return replace(_facts(), management=Management(state, f"EMS work mode:{mode}", {"EMS work mode": mode}))


def test_a_hold_does_not_release_itself_on_the_remote_ems_its_own_writes_turn_on() -> None:
    memory, _ = _armed_hold()
    own = _inputs(T0 + timedelta(seconds=ARM_S + 5), facts=_remote_ems(), reads=_reads(remote_ems="on"))

    memory, decisions = _run(memory, own, replace(own, now=own.now + timedelta(seconds=10)))

    assert decisions == [] and memory.held
    # Another mode is still the plant's own management: released at once.
    _memory, [decision] = _run(memory, _inputs(own.now + timedelta(seconds=20), facts=_remote_ems(mode="ai_mode")))
    assert decision.action == "would_restore" and decision.reason == "self_managed:EMS work mode:ai_mode"


def test_a_blinking_management_reading_does_not_release_a_hold_the_executor_judges_it_after_its_grace() -> None:
    memory, _ = _armed_hold()
    blink = _inputs(T0 + timedelta(seconds=ARM_S + 5), facts=_remote_ems("unknown", "unavailable"))

    memory, decisions = _run(memory, blink)

    assert decisions == [] and memory.held
