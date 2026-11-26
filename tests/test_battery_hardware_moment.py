"""When a pressed hardware test may start its battery part (`BatteryControl.hardware_test_moment`): the routine's own
moment, and room under the fuse for the test's grid charge once the battery stops covering the house."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import commands as commands_module
from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.control import NO_ROOM_FOR_CHARGE, BatteryControl
from custom_components.spotnav.battery.executor import preview
from tests import battery_patch
from tests import test_battery_control as shell
from tests.test_battery_control import _hold, _looks, _options, _site, _started

pytestmark = pytest.mark.usefixtures("offline_relay")


def _headroom(monkeypatch: pytest.MonkeyPatch, amps: dict[str, float]) -> None:
    """The least headroom on a phase as the looks read it (`amps["a"]`, changed by the test), with the plant's own
    power per phase not read: the battery's cover is then counted as the recipe says (`cover_a`)."""
    original = BatteryControl.inputs

    def inputs(self: BatteryControl, now: Any, control: Any, **kwargs: Any) -> Any:
        return replace(original(self, now, control, **kwargs), headroom_a=amps["a"], plant_phase_kw=None)

    monkeypatch.setattr(BatteryControl, "inputs", inputs)


async def _control(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]) -> Any:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=True)
    control.facts = control._read_facts()
    return site, control, plant


async def _run_test(control: BatteryControl) -> dict[str, str]:
    assert await control.async_hardware_test_start() is None
    started = now = dt_util.now()
    while control.hardware_test_progress()["running"] and now < started + timedelta(minutes=15):
        now = await _looks(control, now, 1)
    return {mode: row["outcome"] for mode, row in control.hardware_test_progress()["report"]["modes"].items()}


async def test_the_test_waits_while_its_grid_charge_would_not_fit_under_the_fuse(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    # A plant that charges on one phase, covering a 2 kW house: 8.7 A come back on that phase once it stops, and
    # the charge step asks for 1.31 kW (the HA-down cap on one phase), 5.7 A. With 12 A left the hold still fits
    # (2 A of margin), the charge does not start: 12 - 8.7 - 2 = 1.3 A, 0.3 kW, under the least charge of 0.5 kW.
    amps = {"a": 12.0}
    _site_entry, control, _plant = await _control(hass, monkeypatch, hass_storage)
    _headroom(monkeypatch, amps)
    monkeypatch.setattr(BatteryControl, "_plant_phases", lambda self, control: 1)
    now = dt_util.now()
    assert control._grid_charge_cap_kw(now, control.options()) == pytest.approx(1.31, abs=0.01)
    assert control.hardware_test_moment(now) == NO_ROOM_FOR_CHARGE == "charge_no_room"
    assert control.executor.state.phase == "idle", "asking writes nothing and takes nothing"
    # 12.8 A: 2.1 A, 0.48 kW, still under the least charge.
    amps["a"] = 12.8
    assert control.hardware_test_moment(now) == NO_ROOM_FOR_CHARGE
    # The house uses less (or the fuse has more room): at 14 A the charge would start, lowered to 14 - 8.7 - 2 =
    # 3.3 A, 0.77 kW. Not all of the 1.31 kW asked, and the moment suits: the step is judged against the rate
    # commanded.
    amps["a"] = 14.0
    assert control.hardware_test_moment(now) is None
    amps["a"] = 17.0  # 6.3 A, 1.46 kW: all of it
    assert control.hardware_test_moment(now) is None
    # Under the hold's own rule nothing changes: no room for the hold is still said as that.
    amps["a"] = 10.0
    assert control.hardware_test_moment(now) == "headroom_too_small"


@pytest.mark.parametrize(("headroom_a", "waits", "charge"), [
    # Refused at its start: the test would be incomplete. Lowered to 0.77 kW of the 1.31 kW asked: judged against
    # the 0.77 kW commanded, a plant that did as told passes. All of it fits: the step passes.
    (12.0, True, "aborted:fuse_headroom"), (14.0, False, "obeyed"), (17.0, False, "obeyed"),
])
async def test_the_wait_is_for_what_a_test_started_at_that_moment_would_meet(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], headroom_a: float,
    waits: bool, charge: str,
) -> None:
    _site_entry, control, _plant = await _control(hass, monkeypatch, hass_storage)
    _headroom(monkeypatch, {"a": headroom_a})
    monkeypatch.setattr(BatteryControl, "_plant_phases", lambda self, control: 1)
    assert (control.hardware_test_moment(dt_util.now()) == NO_ROOM_FOR_CHARGE) is waits
    outcomes = await _run_test(control)
    assert outcomes["charge"] == charge and outcomes["hold"] == "obeyed", outcomes
    if headroom_a == 14.0:
        readings = control.hardware_test_progress()["report"]["modes"]["charge"]["readings"]
        assert {(row["power_kw"], row["expected_kw"]) for row in readings} == {(0.771, 0.771)}


def _cap(monkeypatch: pytest.MonkeyPatch, cap_kw: float) -> None:
    monkeypatch.setattr(BatteryControl, "_grid_charge_cap_kw", lambda self, now, options, **kwargs: cap_kw)


def _charge_share(monkeypatch: pytest.MonkeyPatch, plant: Any, share: float) -> None:
    """A plant that takes `share` of a grid charge's power and does everything else as told."""

    def power(self: BatteryControl) -> tuple[float, float]:
        kw = plant.power_kw()
        return (kw * share if kw > 0 else kw), 5.0

    monkeypatch.setattr(BatteryControl, "_battery_power", power)


@pytest.mark.parametrize(("share", "charge"), [(1.0, "obeyed"), (0.4, "partial"), (0.0, "ignored")])
async def test_a_charge_at_the_ha_down_cap_is_judged_against_the_cap_in_the_hardware_test(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], share: float, charge: str,
) -> None:
    # A one-phase plant on a small fuse: the HA-down cap leaves 0.7 kW of the 2 kW the step asks for. The plant that
    # charges exactly at that passed (it read `partial`, which ended the test as failed); one that takes too little
    # of the capped charge, or nothing, still fails.
    _site_entry, control, plant = await _control(hass, monkeypatch, hass_storage)
    _cap(monkeypatch, 0.7)
    _charge_share(monkeypatch, plant, share)
    assert control.hardware_test_moment(dt_util.now()) is None
    outcomes = await _run_test(control)
    assert outcomes["hold"] == "obeyed" and outcomes["charge"] == charge, outcomes
    report = control.hardware_test_progress()["report"]
    assert gate.hardware_test_passed(report) is (share == 1.0)
    if share == 1.0:
        row = report["modes"]["charge"]
        assert row["rate_kw"] == 2.0 and {(r["power_kw"], r["expected_kw"]) for r in row["readings"]} == {(0.7, 0.7)}
        assert any(call[2].get("value") == 0.7 for call in plant.calls), "charged at the cap"


@pytest.mark.parametrize(("share", "charge"), [(1.0, "obeyed"), (0.4, "partial"), (0.0, "ignored")])
async def test_a_charge_at_the_ha_down_cap_is_judged_against_the_cap_in_the_routine(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], share: float, charge: str,
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _cap(monkeypatch, 0.7)
    _charge_share(monkeypatch, plant, share)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    await _looks(control, dt_util.now(), 400)
    report = control.report()
    assert report["modes"]["charge"]["outcome"] == charge, report
    assert gate.mode_verdict(charge) == ("pass" if share == 1.0 else "fail")
    assert ("grid_charge" in control.allowed()) is (share == 1.0)
    if share == 1.0:
        readings = report["modes"]["charge"]["readings"]
        assert {(row["power_kw"], row["expected_kw"]) for row in readings} == {(0.7, 0.7)}
        assert control.state() == "on" and gate.report_status(report) == "passed"


async def test_a_cap_under_the_least_charge_is_not_judged_and_never_a_failure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    # The routine: the step is left for a later run.
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _cap(monkeypatch, 0.4)
    monkeypatch.setattr(BatteryControl, "_phase_charge_possible", lambda self: False)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    await _looks(control, dt_util.now(), 400)
    report = control.report()
    assert report["modes"]["charge"]["outcome"] == "not_judgeable:grid_charge_cap"
    assert gate.report_status(report) == "passed" and "grid_charge" not in control.allowed()
    assert not any(call[2].get("option") == "Command Charging (Grid First)" for call in plant.calls), "no grid charge"


async def test_a_cap_under_the_least_charge_is_not_judged_in_the_hardware_test_either(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    # Never waited for, said as not judged: the test is not complete rather than failed.
    _site_entry, control, plant = await _control(hass, monkeypatch, hass_storage)
    _cap(monkeypatch, 0.4)
    monkeypatch.setattr(BatteryControl, "_phase_charge_possible", lambda self: False)
    assert control.hardware_test_moment(dt_util.now()) is None
    outcomes = await _run_test(control)
    assert outcomes["charge"] == "not_judgeable:grid_charge_cap" and outcomes["hold"] == "obeyed", outcomes
    assert gate.mode_verdict(outcomes["charge"]) != "fail"
    assert not any(call[2].get("option") == "Command Charging (Grid First)" for call in plant.calls), "no grid charge"


async def test_a_plant_that_spreads_its_charge_over_three_phases_needs_a_third_of_it_on_each(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    # The hold's rule counts all the cover on one phase (8.7 A, 2 A of margin): at 11 A it just fits, and the 0.3 A
    # left carry 0.2 kW on three phases, not the 2 kW asked. At 14 A: 3.3 A, 2.3 kW.
    amps = {"a": 11.0}
    _site_entry, control, _plant = await _control(hass, monkeypatch, hass_storage)
    _headroom(monkeypatch, amps)
    assert control._plant_phases(control._executor().control) == 3
    assert control.hardware_test_moment(dt_util.now()) == NO_ROOM_FOR_CHARGE
    amps["a"] = 14.0
    assert control.hardware_test_moment(dt_util.now()) is None


async def test_a_recipe_without_a_grid_charge_never_waits_for_one(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    limits_only = replace(commands_module.SIGENERGY_CONTROL, states={
        action: build for action, build in commands_module.SIGENERGY_CONTROL.states.items()
        if action != commands_module.ACTION_GRID_CHARGE
    })
    battery_patch.patch(monkeypatch, "control_for", lambda brand, variant=None: limits_only.with_hold(variant))
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=True)
    control.facts = control._read_facts()
    _headroom(monkeypatch, {"a": 12.0})
    monkeypatch.setattr(BatteryControl, "_plant_phases", lambda self, control: 1)
    assert "charge" not in control._supported_modes()
    assert control.hardware_test_moment(dt_util.now()) is None


async def test_a_charge_step_the_test_would_not_run_is_never_waited_for(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    # A plant that charges at its own power, far over the HA-down cap: the step is not applicable in the test
    # (`gate.NOT_APPLICABLE_FIXED_CHARGE`) and the test passes without it, so no moment is waited for.
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    fixed = replace(commands_module.SIGENERGY_CONTROL, fixed_charge=True, fixed_charge_kw=50.0)
    battery_patch.patch(monkeypatch, "control_for", lambda brand, variant=None: fixed.with_hold(variant))
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=True)
    control.facts = control._read_facts()
    _headroom(monkeypatch, {"a": 12.0})
    monkeypatch.setattr(BatteryControl, "_plant_phases", lambda self, control: 1)
    assert control.hardware_test_moment(dt_util.now()) is None
    # An HA-down cap that leaves no balanced charge at all is the test's own to report, never a wait.
    monkeypatch.setattr(BatteryControl, "_grid_charge_cap_kw", lambda self, now, options, **kwargs: 0.2)
    battery_patch.patch(
        monkeypatch, "control_for", lambda brand, variant=None: commands_module.SIGENERGY_CONTROL.with_hold(variant)
    )
    control.executor = None
    assert control.hardware_test_moment(dt_util.now()) is None


async def test_a_fixed_charge_that_fits_under_the_cap_waits_for_room_for_all_of_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    # A plant that charges at its own power on three phases (here its 13.2 kW rating, 19 A a phase): never lowered,
    # so all of it must fit beside the cover that comes back (8.7 A, all counted on one phase) and the margin.
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    fixed = replace(commands_module.SIGENERGY_CONTROL, fixed_charge=True, fixed_charge_kw=3.0)
    battery_patch.patch(monkeypatch, "control_for", lambda brand, variant=None: fixed.with_hold(variant))
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=True)
    control.facts = control._read_facts()
    assert control._fixed_charge_kw() == pytest.approx(13.2)
    monkeypatch.setattr(BatteryControl, "_grid_charge_cap_kw", lambda self, now, options, **kwargs: 20.0)
    amps = {"a": 29.0}
    _headroom(monkeypatch, amps)
    assert control.hardware_test_moment(dt_util.now()) == NO_ROOM_FOR_CHARGE  # 18.3 A left, 12.7 kW
    amps["a"] = 30.0  # 19.3 A, 13.4 kW
    assert control.hardware_test_moment(dt_util.now()) is None


async def test_the_plant_under_spotnavs_own_command_is_not_read_as_someone_elses(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    # Control holds the plant (its Remote EMS is on): a test pressed then takes it over from control. A fresh
    # executor reads that command as the plant's own management, which made the test wait until
    # control let go by itself.
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = await _looks(control, dt_util.now(), 6)
    executor = control.executor
    assert executor.state.phase == "active" and hass.states.get(shell.SWITCH).state == "on"
    fresh = preview(executor.control, control.inputs(now, executor.control)).blocker
    assert fresh == "self_managed:EMS work mode:remote_ems"
    assert control._moment(now, executor).blocker is None
    assert control.hardware_test_moment(now) == "battery_not_discharging"  # the hold: nothing to judge one against
    plant.obeys = False  # the battery covers the house while control still has the plant
    assert control.hardware_test_moment(now) is None
    # The routine's own start still waits for control to let go.
    assert gate.suitable(control._moment(now, executor)) == "executor_busy"
    # Idle, a Remote EMS found on is someone else's, as before.
    await control.async_stop()
    hass.states.async_set(shell.SWITCH, "on")
    control.executor = None
    idle = control._executor()
    assert idle.state.phase == "idle" and not idle.state.holds_snapshot
    blocker = control._moment(now, idle).blocker
    assert blocker is not None and control.hardware_test_moment(now) == blocker
