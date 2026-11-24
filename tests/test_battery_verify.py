"""The verification routine against fake batteries: each mode commanded through the executor, judged, handed back;
a report per firmware string."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL, SOLAREDGE_CONTROL
from custom_components.spotnav.battery.executor import Executor
from custom_components.spotnav.battery.gate import merge_report, mode_verdict, modes_to_run, needs_run
from custom_components.spotnav.battery.plan_model import ACTION_EXPORT_DISCHARGE, ACTION_GRID_CHARGE
from custom_components.spotnav.battery.monitor import Capability
from custom_components.spotnav.battery.verify import (
    MAX_REPORTS,
    TEST_CHARGE_SOC_MAX_PCT,
    Sample,
    Timing,
    Verification,
    keep_report,
    supported_modes,
)
from tests.battery_control_fakes import SIGENERGY_STATES, SOLAREDGE_STATES, T0, TICK, FakeBattery, inputs


def _verify(
    executor: Executor, battery: FakeBattery, *, looks: int = 400, house_kw: float = 2.0,
    modes: tuple[str, ...] | None = None, headroom: Callable[[str | None], float] = lambda _mode: 20.0,
    quick: bool = False,
) -> Verification:
    now = T0
    verification = Verification(
        executor, now, firmware="V100R001C21SPC117", modes=modes,
        timing=Timing.quick() if quick else None, stop_on_fail=quick,
        **({"charge_soc_max_pct": TEST_CHARGE_SOC_MAX_PCT} if quick else {}),
    )
    failed: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()
    capability = Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0)
    for _ in range(looks):
        power = battery.power_kw(battery.action, battery.rate_kw)
        sample = Sample(power, 5.0, battery.soc_pct, house_kw)
        action, rate = verification.command(now, sample, 10.0)
        current = verification.current
        result = executor.step(inputs(
            battery, now, action, rate, failed=failed, house_deficit_kw=house_kw,
            headroom_a=headroom(None if current is None else current.mode), timed_out=timed_out,
        ))
        failed = tuple(op.role for op in result.ops if not battery.apply(op))
        timed_out = tuple(role for role in failed if role in battery.times_out)
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


def test_a_call_given_up_on_is_the_moment_s_not_a_failed_mode() -> None:
    # A slow integration during the charge step: tried again at a later run, never grid charging off for good.
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), times_out={"remote_ems"})
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery, modes=("charge",))
    outcome = _outcomes(verification)["charge"]
    assert outcome.startswith("aborted:call_timeout")
    assert mode_verdict(outcome) == "pending"
    # Handed back; a charge limit never set holds the rating, as after any charge.
    assert battery.states == SIGENERGY_STATES | {"charge_limit": "10"}


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


def test_normal_is_judged_only_after_the_run_wrote_something() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery, modes=())
    assert _outcomes(verification) == {"normal": "not_judgeable:nothing_written"}
    assert battery.states == SIGENERGY_STATES


def test_a_mode_aborted_for_the_moment_still_lets_normal_judge_the_hand_back() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    executor = Executor(SIGENERGY_CONTROL)
    # The fuse has no room when the charge mode comes: the executor does not take it.
    verification = _verify(executor, battery, headroom=lambda mode: 0.5 if mode == "charge" else 20.0)
    outcomes = _outcomes(verification)
    assert outcomes["hold"] == "obeyed" and outcomes["charge"] == "aborted:headroom_too_small"
    assert mode_verdict(outcomes["charge"]) == "pending"
    assert outcomes["discharge_house"] == "skipped" and outcomes["normal"] == "restored"
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


def test_the_hardware_test_runs_every_mode_in_minutes() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery, quick=True)
    assert verification.done
    assert _outcomes(verification) == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"
    }
    assert len(verification.report()["modes"]["hold"]["readings"]) == 2
    assert verification.finished_at is not None
    assert (verification.finished_at - verification.started_at).total_seconds() <= 10 * 60
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_the_hardware_test_stops_at_the_first_mode_that_does_not_pass_and_hands_back() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), obeys=False)
    executor = Executor(SIGENERGY_CONTROL)
    verification = _verify(executor, battery, quick=True)
    outcomes = _outcomes(verification)
    # Ignored under its first hold variant: no second variant, no further mode; normal judges the hand-back.
    assert outcomes["hold"] == "ignored"
    assert outcomes["charge"] == outcomes["discharge_house"] == "skipped"
    assert outcomes["normal"] == "restored"
    assert len(verification.report()["modes"]["hold"]["variants"]) == 1
    # Handed back: nothing SpotNav wrote is left (a limit never set keeps "never set").
    assert {key: value for key, value in battery.states.items() if key != "charge_limit"} == {
        key: value for key, value in SIGENERGY_STATES.items() if key != "charge_limit"}


class _NearlyFull(FakeBattery):
    """A battery at a high charge level that takes `taken` of a grid charge's rate."""

    taken: float = 1.0

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        power = super().power_kw(action, rate_kw)
        return power * self.taken if action == "grid_charge" else power


def test_the_hardware_test_s_charge_step_is_tried_up_to_98_percent_and_a_short_one_near_full_is_not_judged() -> None:
    def charge(soc: float, taken: float, *, quick: bool) -> str | None:
        battery = _NearlyFull(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), soc_pct=soc)
        battery.taken = taken
        return _outcomes(_verify(Executor(SIGENERGY_CONTROL), battery, modes=("charge",), quick=quick))["charge"]

    # The routine and normal control keep 90 %.
    assert charge(95.0, 1.0, quick=False) == "not_judgeable:soc_near_full"
    # The hardware test tries it up to 98 %: taken in full, obeyed; taken short near full, nothing to judge.
    assert charge(95.0, 1.0, quick=True) == "obeyed"
    assert charge(95.0, 0.3, quick=True) == "not_judgeable:battery_full"
    assert charge(95.0, 0.0, quick=True) == "not_judgeable:battery_full"
    assert charge(98.0, 1.0, quick=True) == "not_judgeable:soc_near_full"
    # Below the taper a charge taken short is still a failure.
    assert charge(60.0, 0.0, quick=True) == "ignored"


# --- the mode `expiry` -----------------------------------------------------------------------------------------------


def _expiry_run(
    *, fallback_after_s: float | None, window_s: float = 60.0, renew_s: int = 20, silent_headroom_a: float = 20.0,
    charge_in_silence: bool = False, looks: int = 400,
) -> tuple[Verification, Executor, list[dict]]:
    """A SolarEdge hold whose command timeout is a 60 s dead-man SpotNav renews every 20 s (a recipe made up for the
    test: SolarEdge's own is 1800 s and never tried), with a plant that leaves the hold `fallback_after_s` after the
    last write of a renewed role, once the silence runs (`None`: never)."""
    from dataclasses import replace  # noqa: PLC0415

    from custom_components.spotnav.battery.commands import Expiry  # noqa: PLC0415

    expiry = Expiry("device_deadman", "spotnav", window_s, ("command_timeout", "command_mode"), renew_s=renew_s)
    control = replace(SOLAREDGE_CONTROL, expiry=expiry)
    executor = Executor(control)
    battery = FakeBattery(control, dict(SOLAREDGE_STATES))
    now = T0
    verification = Verification(executor, now, firmware="V1", modes=("hold", "expiry"), expiry=expiry)
    capability = Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0)
    house_kw = 2.0
    silent_at: datetime | None = None
    last_write = now
    probes: list[dict] = []
    for _ in range(looks):
        power = battery.power_kw(battery.action, battery.rate_kw)
        if silent_at is not None and fallback_after_s is not None and (now - last_write).total_seconds() >= fallback_after_s:
            power = -house_kw  # back in its own mode: the battery covers the house
        if silent_at is not None and charge_in_silence:
            power = 1.5
        grid = house_kw + power
        headroom_a = 20.0 if silent_at is None else silent_headroom_a
        sample = Sample(power, 5.0, battery.soc_pct, house_kw, grid, headroom_a=headroom_a)
        action, rate = verification.command(now, sample, 10.0)
        probe = verification.expiry_probe()
        if probe and silent_at is None:
            silent_at = now
        result = executor.step(replace(
            inputs(battery, now, action, rate, house_deficit_kw=house_kw, headroom_a=headroom_a, grid_import_kw=grid),
            facts=replace(battery.facts(power)), expiry_probe=probe,
        ))
        probes.append({"probe": probe, "ops": [op.role for op in result.ops],
                       "events": [event["kind"] for event in result.events]})
        for op in result.ops:
            battery.apply(op)
            if op.role in expiry.renewed_roles:
                last_write = now
        state = executor.state
        battery.action = state.action if state.phase == "active" else None
        verification.after(now, result, sample, capability)
        now += TICK
        if verification.done and state.phase in ("idle", "blocked") and not result.ops:
            break
    return verification, executor, probes


def test_a_hold_whose_dead_man_acts_within_its_window_is_obeyed_and_nothing_is_written_in_the_silence() -> None:
    verification, executor, looks = _expiry_run(fallback_after_s=55.0)
    assert verification.done
    row = verification.report()["modes"]["expiry"]
    assert row["outcome"] == "obeyed" and row["scope"] == "hold" and row["window_s"] == 60.0
    assert row["expiry_id"] == "device_deadman:60:command_timeout,command_mode"
    # Timed from the last write, to the look: within a look of the plant's own 55 s.
    assert 55.0 <= row["fallback_s"] <= 65.0
    assert _outcomes(verification)["normal"] == "restored"
    silent = [look for look in looks if look["probe"]]
    assert silent and all(look["ops"] == [] and "renew" not in look["events"] for look in silent)
    assert executor.state.phase == "idle" and executor.state.blocked is None


def test_a_hold_that_never_falls_back_is_ignored_by_the_deadline() -> None:
    verification, _executor, looks = _expiry_run(fallback_after_s=None)
    assert _outcomes(verification)["expiry"] == "ignored"
    # 60 s and a 30 s margin from the last write, a look apart.
    assert 6 <= sum(look["probe"] for look in looks) <= 10


def test_a_fallback_sooner_than_the_renewal_is_a_recipe_bug_not_a_pass() -> None:
    verification, _executor, _looks = _expiry_run(fallback_after_s=25.0, renew_s=40)
    assert _outcomes(verification)["expiry"] == "partial:early"


def test_a_fallback_later_than_the_window_and_a_look_is_not_a_pass() -> None:
    from custom_components.spotnav.battery import gate  # noqa: PLC0415

    verification, _executor, _looks = _expiry_run(fallback_after_s=85.0)
    row = verification.report()["modes"]["expiry"]
    assert row["outcome"] == "ignored" and row["late"] and row["fallback_s"] >= 85.0
    # Even written as obeyed, the gate takes no fallback past the minute and a look.
    assert not gate.expiry_row_passed({"modes": {"expiry": {**row, "outcome": "obeyed", "passes": 2}}},
                                      _expiry_of(60.0))
    assert gate.expiry_row_passed({"modes": {"expiry": {**row, "outcome": "obeyed", "fallback_s": 70.0,
                                                        "passes": 2}}}, _expiry_of(60.0))


def _expiry_of(window_s: float):  # noqa: ANN202
    from custom_components.spotnav.battery.commands import Expiry  # noqa: PLC0415

    return Expiry("device_deadman", "spotnav", window_s, ("command_timeout", "command_mode"), renew_s=20)


def test_the_expiry_test_gives_way_near_the_fuse_and_to_a_charge_in_the_silence() -> None:
    # 1.5 A of headroom: under the executor's margin, over its hand-back mark.
    verification, _executor, _looks = _expiry_run(fallback_after_s=None, silent_headroom_a=1.5)
    assert _outcomes(verification)["expiry"] == "aborted:fuse_headroom"
    verification, executor, _looks = _expiry_run(fallback_after_s=None, charge_in_silence=True)
    outcome = _outcomes(verification)["expiry"]
    assert outcome == "aborted:charge_in_silence"
    assert mode_verdict(outcome) == "pending"


def test_the_expiry_is_never_tried_unless_a_run_names_it_and_never_without_a_hold_that_passed() -> None:
    battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES))
    verification = _verify(Executor(SOLAREDGE_CONTROL), battery)
    assert "expiry" not in _outcomes(verification)
    battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES))
    verification = _verify(Executor(SOLAREDGE_CONTROL), battery, modes=("expiry",))
    assert _outcomes(verification)["expiry"] == "not_judgeable:not_supported"


def test_a_recipe_without_a_grid_charge_skips_the_charge_step_and_its_routine_never_waits_for_it() -> None:
    # A limits-only recipe (FoxESS's): hold and the discharge for the house, no grid charge and no sale.
    control = replace(SIGENERGY_CONTROL, states={
        action: build for action, build in SIGENERGY_CONTROL.states.items()
        if action not in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE)
    })
    battery = FakeBattery(control, dict(SIGENERGY_STATES))
    executor = Executor(control)
    verification = _verify(executor, battery, quick=True)
    assert verification.done
    assert _outcomes(verification) == {
        "hold": "obeyed", "charge": "skipped", "discharge_house": "obeyed", "normal": "restored",
    }
    # Nothing of a charge was ever written.
    assert all(op.role != "control_mode" or op.value != "Command Charging (Grid First)" for op in battery.writes)
    supported = supported_modes(control)
    assert "charge" not in supported and {"hold", "discharge_house", "normal"} <= supported
    report = merge_report(None, verification.report(), key="k", attempts=1, now=T0)
    assert modes_to_run(report, supported) == () and not needs_run(report, supported)
    # Without the recipe's modes the routine would keep trying the charge (every other brand: unchanged).
    assert modes_to_run(report) == ("charge",)
    assert supported_modes(SIGENERGY_CONTROL) >= {"hold", "charge", "discharge_house", "normal"}
