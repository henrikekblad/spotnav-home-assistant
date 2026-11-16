"""The verification routine against fake batteries: each mode commanded through the executor, judged, handed back;
a report per firmware string."""

from __future__ import annotations

from datetime import datetime

from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL, SOLAREDGE_CONTROL
from custom_components.spotnav.battery.executor import Executor
from custom_components.spotnav.battery.monitor import Capability
from custom_components.spotnav.battery.verify import MAX_REPORTS, Sample, Verification, keep_report
from tests.battery_control_fakes import SIGENERGY_STATES, SOLAREDGE_STATES, T0, TICK, FakeBattery, inputs


def _verify(
    executor: Executor, battery: FakeBattery, *, looks: int = 400, house_kw: float = 2.0,
    modes: tuple[str, ...] | None = None,
) -> Verification:
    now = T0
    verification = Verification(executor, now, firmware="V100R001C21SPC117", modes=modes)
    failed: tuple[str, ...] = ()
    capability = Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0)
    for _ in range(looks):
        power = battery.power_kw(battery.action, battery.rate_kw)
        sample = Sample(power, 5.0, battery.soc_pct, house_kw)
        action, rate = verification.command(now, sample, 10.0)
        result = executor.step(inputs(battery, now, action, rate, failed=failed, house_deficit_kw=house_kw))
        failed = tuple(op.role for op in result.ops if not battery.apply(op))
        state = executor.state
        battery.action = state.action if state.phase == "active" else None
        battery.rate_kw = state.rate_kw
        verification.after(now, result, sample, capability)
        now += TICK
        if verification.done and state.phase in ("idle", "blocked") and not result.ops:
            break
    return verification


def _outcomes(verification: Verification) -> dict[str, str | None]:
    return {mode: row["outcome"] for mode, row in verification.report()["modes"].items()}


def test_a_battery_that_obeys_every_mode_is_reported_so_and_left_as_found() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery)
    assert verification.done
    assert _outcomes(verification) == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"
    }
    report = verification.report()
    assert report["firmware"] == "V100R001C21SPC117" and report["brand"] == "sigenergy"
    assert len(report["modes"]["hold"]["readings"]) == 3
    assert report["modes"]["charge"]["rate_kw"] == 2.0
    # A charge limit that was never set cannot be set back to "never set": it holds the rating (research §1.4).
    assert battery.states == SIGENERGY_STATES | {"charge_limit": "10"}
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_a_battery_that_ignores_its_commands_is_reported_ignored_mode_by_mode() -> None:
    # Firmware SPC117 with the limits written but not obeyed: every reading is what it did without them.
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), obeys=False)
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery, looks=600)
    outcomes = _outcomes(verification)
    assert outcomes["hold"] == "ignored" and outcomes["charge"] == "ignored"
    assert outcomes["normal"] == "restored"
    assert battery.states == SIGENERGY_STATES | {"charge_limit": "10"}


def test_a_write_the_integration_drops_aborts_the_routine_and_hands_back() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), read_only=True)
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery)
    outcomes = _outcomes(verification)
    assert outcomes["hold"].startswith("aborted:write_not_taken")
    assert outcomes["charge"] == outcomes["discharge_house"] == outcomes["normal"] == "skipped"
    assert battery.states == SIGENERGY_STATES


def test_a_mode_the_battery_cannot_show_now_is_not_judgeable() -> None:
    # Nothing to stop (the battery is idle), so a hold cannot be judged; the others still are.
    battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES), idle_kw=0.0)
    executor = Executor(SOLAREDGE_CONTROL)
    verification = _verify(executor, battery)
    outcomes = _outcomes(verification)
    assert outcomes["hold"] == "not_judgeable:battery_not_discharging"
    assert outcomes["charge"] == "obeyed" and outcomes["normal"] == "restored"
    assert battery.states == SOLAREDGE_STATES


def test_a_later_run_tries_only_the_modes_left_and_always_ends_with_the_hand_back_check() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery, modes=("charge",))
    assert _outcomes(verification) == {"charge": "obeyed", "normal": "restored"}
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_reports_are_kept_per_firmware_and_bounded() -> None:
    reports: dict = {}
    for index in range(MAX_REPORTS + 3):
        at = datetime(2026, 11, 1 + index, tzinfo=T0.tzinfo).isoformat()
        reports = keep_report(reports, {"firmware": f"FW{index}", "started_at": at, "finished_at": at})
    assert len(reports) == MAX_REPORTS and "FW0" not in reports and f"FW{MAX_REPORTS + 2}" in reports
    later = datetime(2026, 12, 1, tzinfo=T0.tzinfo).isoformat()
    reports = keep_report(reports, {"firmware": "FW5", "started_at": later, "finished_at": later, "new": True})
    assert reports["FW5"]["new"] is True and list(reports)[-1] == "FW5"
