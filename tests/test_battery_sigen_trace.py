"""Sigenergy's write path, pinned look by look: a hold, a balanced grid charge (rate down and up, the fuse's
lowering and hand-back), a discharge, the peak cover under a hold, a charge per phase (a phase's room falling, a
raise, the fuse hand-back) and a restart mid-charge, each run against the fake plants and compared with a trace
recorded on the engine before the recipe data for grid targets, fast watchdogs and the house's load was added
(`fixtures/battery/sigen_trace.json`, v3 at ca6c3a6c). Any change to what Sigenergy writes, when, or what it
reports, fails here.

Regenerate only on purpose, on the engine whose behaviour is wanted:
`python -m tests.test_battery_sigen_trace`."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL
from custom_components.spotnav.battery.executor import ExecState, Executor, StepResult
from tests.battery_control_fakes import SIGENERGY_STATES, T0, FakeBattery, run
from tests.test_battery_per_phase import _caps, _site
from tests.test_battery_per_phase import _run as _run_site

FIXTURE = Path(__file__).parent / "fixtures" / "battery" / "sigen_trace.json"
#: A quiet house's learned peaks, so every phase has room for a charge.
QUIET = {"L1": 4.0, "L2": 4.0, "L3": 4.0}


def _look(result: StepResult) -> dict[str, Any]:
    return {
        "ops": [[op.role, op.entity_id, op.value, op.restore] for op in result.ops],
        "events": result.events,
        "persist_first": result.persist_first,
    }


def _trace(results: list[StepResult], executor: Executor) -> dict[str, Any]:
    return {"looks": [_look(result) for result in results], "state": executor.state.as_dict()}


def _balanced() -> dict[str, Any]:
    out: dict[str, Any] = {}
    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, hold = run(executor, battery, T0, 6, "hold")
    now, back = run(executor, battery, now, 8, "self_consume")
    out["hold"] = _trace(hold + back, executor)

    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    results: list[StepResult] = []
    now, part = run(executor, battery, T0, 10, "grid_charge", 3.0)
    results += part
    now, part = run(executor, battery, now, 6, "grid_charge", 2.0)
    results += part
    now, part = run(executor, battery, now, 40, "grid_charge", 4.0)
    results += part
    # The fuse: headroom falls under the start margin, then to the hand-back mark.
    now, part = run(executor, battery, now, 6, "grid_charge", 4.0, headroom_a=3.0)
    results += part
    now, part = run(executor, battery, now, 8, "grid_charge", 4.0, headroom_a=0.5)
    results += part
    now, part = run(executor, battery, now, 10, "self_consume")
    results += part
    out["grid_charge"] = _trace(results, executor)

    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, part1 = run(executor, battery, T0, 6, "discharge_house")
    now, part2 = run(executor, battery, now, 6, "export_discharge", 2.0)
    now, part3 = run(executor, battery, now, 8, "self_consume")
    out["discharge"] = _trace(part1 + part2 + part3, executor)

    # A peak under a hold: the mode back to Maximum Self Consumption, then the hold again, then a hand-back.
    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, part1 = run(executor, battery, T0, 6, "hold")
    now, part2 = run(executor, battery, now, 6, "hold", headroom_a=1.2)
    now, part3 = run(executor, battery, now, 60, "hold", headroom_a=16.0)
    now, part4 = run(executor, battery, now, 8, "self_consume")
    out["peak_cover"] = _trace(part1 + part2 + part3 + part4, executor)

    # A restart mid-charge: the stored state hands back first.
    executor = Executor(SIGENERGY_CONTROL)
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 3.0)
    restarted = Executor(SIGENERGY_CONTROL, state=ExecState.from_dict(json.loads(json.dumps(executor.state.as_dict()))))
    later = now + timedelta(minutes=5)
    events = restarted.restart(later)
    later, part = run(restarted, battery, later, 10, "grid_charge", 3.0)
    out["restart"] = {"restart_events": events, **_trace(part, restarted)}
    return out


def _per_phase() -> dict[str, Any]:
    out: dict[str, Any] = {}
    caps = _caps(QUIET)
    executor, site = _site()
    assert executor.use_phases(True)
    results: list[StepResult] = []
    now, part = _run_site(executor, site, T0, 12, "grid_charge", 6.0, phase_caps_kw=caps)
    results += part
    # L1's house rises: its room falls, that phase goes down at once.
    site.house_a = [10.5, 3.0, 2.0]
    now, part = _run_site(executor, site, now, 12, "grid_charge", 6.0, phase_caps_kw=caps)
    results += part
    # It falls again: the charge goes back up after the dwell.
    site.house_a = [2.0, 3.0, 2.0]
    now, part = _run_site(executor, site, now, 40, "grid_charge", 6.0, phase_caps_kw=caps)
    results += part
    # A phase near the fuse: everything is handed back at once.
    site.house_a = [2.0, 3.0, 19.5]
    now, part = _run_site(executor, site, now, 8, "grid_charge", 6.0, phase_caps_kw=caps)
    results += part
    site.house_a = [2.0, 3.0, 2.0]
    now, part = _run_site(executor, site, now, 10, "self_consume", phase_caps_kw=caps)
    results += part
    out["per_phase"] = _trace(results, executor)

    # The owner's house (L2's peak fills it): only the phases with room charge; then a hold and a hand-back.
    executor, site = _site()
    assert executor.use_phases(True)
    now, part1 = _run_site(executor, site, T0, 12, "grid_charge", 4.0)
    now, part2 = _run_site(executor, site, now, 10, "hold")
    now, part3 = _run_site(executor, site, now, 10, "self_consume")
    out["per_phase_to_hold"] = _trace(part1 + part2 + part3, executor)
    return out


def trace_all() -> dict[str, Any]:
    return json.loads(json.dumps({**_balanced(), **_per_phase()}, default=str))


def test_sigenergy_writes_and_reports_look_by_look_as_before() -> None:
    expected = json.loads(FIXTURE.read_text())
    got = trace_all()
    assert set(got) == set(expected)
    for name in expected:
        assert got[name] == expected[name], name


def test_the_trace_exercises_what_it_pins() -> None:
    got = trace_all()

    def kinds(name: str) -> set[str]:
        return {event["kind"] for look in got[name]["looks"] for event in look["events"]}

    def roles(name: str) -> set[str]:
        return {op[0] for look in got[name]["looks"] for op in look["ops"]}

    assert {"enter", "hand_back"} <= kinds("hold")
    assert {"enter", "rate", "hand_back"} <= kinds("grid_charge")
    assert "peak_cover" in kinds("peak_cover")
    assert {"enter", "rate", "hand_back"} <= kinds("per_phase")
    assert {"phase_l1_target", "phase_l2_target", "phase_l3_target"} <= roles("per_phase")
    assert got["restart"]["looks"][0]["ops"], "a restart hands back first"


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(trace_all(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE}")
