"""SolarEdge, Huawei and SolaX, whose watchdog is renewed on the shared `WATCHDOG_RENEW_S`, pinned look by look as
`test_battery_sigen_trace` pins Sigenergy: a grid charge through two renewals (one with its role reading nothing),
a rate change, a hold and the hand-back, compared with a trace recorded on v3 at ca6c3a6c
(`fixtures/battery/watchdog_trace.json`).

Regenerate only on purpose: `python -m tests.test_battery_watchdog_trace`."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from custom_components.spotnav.battery.commands import HUAWEI_CONTROL, SOLAREDGE_CONTROL, SOLAX_CONTROL
from custom_components.spotnav.battery.executor import Executor, StepResult
from tests.battery_control_fakes import HUAWEI_STATES, SOLAREDGE_STATES, SOLAX_STATES, T0, FakeBattery, run
from tests.test_battery_sigen_trace import _trace

FIXTURE = Path(__file__).parent / "fixtures" / "battery" / "watchdog_trace.json"
RENEW = timedelta(seconds=900)
BRANDS = (
    (SOLAREDGE_CONTROL, SOLAREDGE_STATES, {"command_mode": "unavailable"}),
    (HUAWEI_CONTROL, HUAWEI_STATES, {"forcible": "unavailable"}),
    (SOLAX_CONTROL, SOLAX_STATES, {"autorepeat": "unavailable"}),
)


def trace_all() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for control, states, unreadable in BRANDS:
        executor, battery = Executor(control), FakeBattery(control, dict(states))
        results: list[StepResult] = []
        now, part = run(executor, battery, T0, 8, "grid_charge", 3.0)
        results += part
        now, part = run(executor, battery, now + RENEW, 6, "grid_charge", 3.0)
        results += part
        battery.states |= unreadable
        now, part = run(executor, battery, now + RENEW, 6, "grid_charge", 3.0)
        results += part
        now, part = run(executor, battery, now, 6, "grid_charge", 2.0)
        results += part
        now, part = run(executor, battery, now, 8, "hold")
        results += part
        now, part = run(executor, battery, now + RENEW, 6, "hold")
        results += part
        now, part = run(executor, battery, now, 10, "self_consume")
        results += part
        out[control.brand] = _trace(results, executor)
    return json.loads(json.dumps(out, default=str))


def test_the_brands_on_the_shared_watchdog_write_and_report_look_by_look_as_before() -> None:
    expected = json.loads(FIXTURE.read_text())
    got = trace_all()
    assert set(got) == set(expected)
    for name in expected:
        assert got[name] == expected[name], name
    for name in expected:
        assert any(event["kind"] == "renew" for look in got[name]["looks"] for event in look["events"]), name


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(trace_all(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE}")
