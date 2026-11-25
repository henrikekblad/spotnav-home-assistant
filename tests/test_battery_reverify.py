"""After an update (the plant's firmware, its integration's version, the recipe's revision) control keeps running on
the last report that passed for the brand on this plant while the routine checks the new key, at the first suitable
moment of any hour (owner rule 2026-11-24: control must never stop because of an update). A check that fails still
stops control; another brand's report, a held-back recipe and a hold variant the recipe dropped never carry."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import commands as commands_module, control as control_module, gate
from custom_components.spotnav.battery.control import BatteryControl
from tests.test_battery_control import (
    FIRMWARE, _LIMITS_ONLY_PASS, _hardware_test_ends, _hold, _looks, _options, _site, _started, _waiting_site,
)

pytestmark = pytest.mark.usefixtures("offline_relay")

NEW_FIRMWARE = "V100R001C21SPC118"


def _charge(_now):
    return ("grid_charge", 2.0, None)


def _versions(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The battery integration's version as the control reads it (changed by the test)."""
    version = {"now": "1.0.0"}
    monkeypatch.setattr(BatteryControl, "_integration_version", lambda self, domain: version["now"] if domain else None)
    return version


def _soon():
    """The first look's time: after every report the test stored (the reports and the looks share one clock)."""
    return dt_util.now() + timedelta(minutes=1)


def _row(outcome: str, **more: Any) -> dict[str, Any]:
    return {"outcome": outcome, **more}


def _report(key: str, brand: str = "sigenergy", at: str = "2026-11-01T10:00:00+00:00", attempts: int = 1,
            **modes: Any) -> dict[str, Any]:
    rows = {"hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"} | modes
    return {"key": key, "brand": brand, "updated_at": at, "attempts": attempts,
            "modes": {name: row if isinstance(row, dict) else _row(row) for name, row in rows.items() if row}}


# --- the rule (`gate.control_report`) ---------------------------------------------------------------------------------


def test_the_report_control_runs_on_is_the_last_passed_one_of_the_brand_while_the_new_key_is_open() -> None:
    old = _report("A | sigen 1")
    reports = {"A | sigen 1": old}
    carried = gate.control_report(None, reports, key="B | sigen 1", brand="sigenergy")
    assert gate.report_status(carried) == "passed" and carried["carried_from"] == "A | sigen 1"
    assert carried["key"] == "B | sigen 1"
    assert gate.allowed_actions(carried, gate.Options(enabled=True, shadow_only=False)) >= {"hold", "grid_charge"}
    # Another brand's report, an unread firmware's, and no report at all: nothing to run on.
    assert gate.control_report(None, reports, key="B | sigen 1", brand="huawei") is None
    assert gate.control_report(None, {"unknown | sigen 1": old}, key="B | sigen 1", brand="sigenergy") is None
    assert gate.control_report(None, {}, key="B | sigen 1", brand="sigenergy") is None
    # The key's own report is never its own source.
    assert gate.control_report(None, reports, key="A | sigen 1", brand="sigenergy") is None


def test_a_mode_decided_on_the_new_key_counts_and_an_open_one_keeps_the_old_answer() -> None:
    reports = {"A | sigen 1": _report("A | sigen 1")}
    options = gate.Options(enabled=True, shadow_only=False)
    # The first run on the new key could not judge the discharge: it stays allowed as before the update.
    new = _report("B | sigen 1", discharge_house="not_judgeable:house_deficit_unknown")
    carried = gate.control_report(new, reports, key="B | sigen 1", brand="sigenergy")
    assert "discharge_house" in gate.allowed_actions(carried, options) and carried["carried_from"] == "A | sigen 1"
    # A charge the new firmware ignores is off, whatever passed before.
    new = _report("B | sigen 1", charge="ignored", discharge_house="not_judgeable:house_deficit_unknown")
    carried = gate.control_report(new, reports, key="B | sigen 1", brand="sigenergy")
    allowed = gate.allowed_actions(carried, options)
    assert "grid_charge" not in allowed and {"hold", "discharge_house"} <= allowed
    # A check that failed, or gave up, stands alone.
    failed = _report("B | sigen 1", hold="ignored")
    assert gate.control_report(failed, reports, key="B | sigen 1", brand="sigenergy") is failed
    gave_up = _report("B | sigen 1", attempts=gate.MAX_ATTEMPTS, discharge_house="not_judgeable:house_deficit_unknown")
    assert gate.control_report(gave_up, reports, key="B | sigen 1", brand="sigenergy") is gave_up
    # Everything decided on the new key: its own report.
    done = _report("B | sigen 1")
    assert gate.control_report(done, reports, key="B | sigen 1", brand="sigenergy") is done


def test_the_last_verdict_of_the_brand_must_be_a_pass_and_its_expiry_and_dropped_hold_never_carry() -> None:
    old = _report("A | sigen 1", at="2026-11-01T10:00:00+00:00")
    later_failed = _report("B | sigen 1", at="2026-11-05T10:00:00+00:00", hold="ignored")
    # The plant stopped obeying on a later firmware: an older pass says nothing of the next one.
    assert gate.control_report(
        None, {"A | sigen 1": old, "B | sigen 1": later_failed}, key="C | sigen 1", brand="sigenergy"
    ) is None
    # The newest pass is the one carried.
    newer = _report("B | sigen 1", at="2026-11-05T10:00:00+00:00", charge="ignored")
    carried = gate.control_report(None, {"A | sigen 1": old, "B | sigen 1": newer}, key="C | sigen 1", brand="sigenergy")
    assert carried["carried_from"] == "B | sigen 1"
    # An expiry measured on the old firmware lifts no cap on the new one.
    with_expiry = _report("A | sigen 1", expiry=_row("obeyed", expiry_id="x", passes=2))
    carried = gate.control_report(None, {"A | sigen 1": with_expiry}, key="C | sigen 1", brand="sigenergy")
    assert "expiry" not in carried["modes"]
    # A pass older than the plant's last failure that left no report (`since`) is not carried; a later one is.
    since = dt_util.parse_datetime("2026-11-03T10:00:00+00:00")
    assert gate.control_report(None, {"A | sigen 1": old}, key="C | sigen 1", brand="sigenergy", since=since) is None
    assert gate.control_report(
        None, {"A | sigen 1": old, "B | sigen 1": newer}, key="C | sigen 1", brand="sigenergy", since=since
    )["carried_from"] == "B | sigen 1"
    # A hold passed in a variant the recipe no longer has is no pass (`without_removed_hold`).
    stale = _report("A | sigen 1", hold=_row("obeyed", variant="msc_no_discharge"))
    assert gate.control_report(
        None, {"A | sigen 1": stale}, key="C | sigen 1", brand="sigenergy", variants=("pv_first",)
    ) is None
    # Nor one the new firmware already ignored.
    kept = _report("A | sigen 1", hold=_row("obeyed", variant="pv_first"))
    new = _report("C | sigen 1", hold=_row("not_judgeable:battery_not_discharging", variants=[
        {"variant": "pv_first", "outcome": "ignored"}]), normal="skipped")
    assert gate.control_report(
        new, {"A | sigen 1": kept}, key="C | sigen 1", brand="sigenergy", variants=("pv_first", "other")
    ) is new


# --- on a site -----------------------------------------------------------------------------------------------------


async def test_an_integration_update_keeps_the_grid_charge_running_while_the_check_waits(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    version = _versions(monkeypatch)
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_charge)
    old_key = control.firmware_key()
    now = await _looks(control, _soon(), 6)
    assert control.executor.state.phase == "active" and control.executor.state.action == "grid_charge"
    version["now"] = "1.1.0"
    await _looks(control, now, 6)
    assert control.firmware_key() != old_key and control.report() is None
    assert control.state() == "on" and "grid_charge" in control.allowed()
    assert control.executor.state.phase == "active" and control.executor.state.action == "grid_charge"
    status = control.status()
    assert status["state"] == "on" and status["line"] == "waiting_after_update:executor_busy"
    assert "grid_charge" in status["allowed_actions"]
    assert control.snapshot()["carried_from"] == old_key
    assert not [row for row in control.log if row["kind"] == "verify_started"]


async def test_the_check_after_an_update_runs_at_the_first_suitable_moment_of_any_hour(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plan = {"slot": _hold(None)}
    control = await _started(hass, site, controller, plan_source=lambda _now: plan["slot"])
    now = await _looks(control, _soon(), 6)
    assert control.executor.state.phase == "active"
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    now = await _looks(control, now, 6)
    # The hold goes on; the check waits only for the plan to let go of the battery.
    assert control.executor.state.phase == "active" and control.state() == "on"
    assert control.status()["line"] == "waiting_after_update:executor_busy"
    plan["slot"] = None
    now = await _looks(control, now, 12)
    assert control.state() == "verifying" and control.status()["line"] == "checking_after_update"
    now = await _looks(control, now, 400)
    assert control.verification is None and gate.report_status(control.report()) == "passed"
    assert control.state() == "on" and control.status()["line"] == "on"
    assert control.snapshot()["carried_from"] is None


async def test_a_failed_check_after_an_update_stops_control_and_turning_it_on_again_does_not_carry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    now = await _looks(control, _soon(), 2)
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    plant.obeys = False
    now = await _looks(control, now, 400)
    assert gate.report_status(control.report()) == "failed"
    assert control.state() == "failed" and control.allowed() == frozenset()
    assert control.status()["line"].startswith("failed:")
    assert control.executor.state.phase == "idle" and not control.executor.state.holds_snapshot
    # Off and on again forgets the failed check so it runs again, but the old pass is not trusted meanwhile.
    plant.obeys = True
    _options(hass, site, enabled=False)
    now = await _looks(control, now, 2)
    _options(hass, site, enabled=True)
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    await _looks(control, now, 2)
    assert control.state() == "waiting" and control.allowed() == frozenset()
    assert control.status()["line"] == "waiting:car_charging"
    # Also after a restart.
    await control.async_shutdown()
    again = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await again.async_start()
    assert again.state() == "waiting"
    await again.async_shutdown()


async def test_control_carried_over_an_update_survives_a_restart(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = await _looks(control, _soon(), 6)
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    await _looks(control, now, 3)
    await control.async_shutdown()
    again = BatteryControl(hass, controller, plan_source=_hold)
    await again.async_start()
    assert again.state() == "on" and again.report() is None
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    # (After the unload's hand-back, which is timed on the clock.)
    await _looks(again, dt_util.now() + timedelta(minutes=30), 6)
    assert again.state() == "on" and again.executor.state.phase == "active"
    assert again.status()["line"] == "waiting_after_update:car_charging"
    await again.async_shutdown()


async def test_another_brands_report_does_not_carry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    old_key = control.firmware_key()
    control.reports[old_key]["brand"] = "huawei"
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    await _looks(control, _soon(), 6)
    assert control.state() == "waiting" and control.allowed() == frozenset()
    assert control.status()["line"] == "waiting:car_charging"
    assert plant.calls == []


async def test_a_held_back_recipe_stops_control_also_while_an_old_report_would_carry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = await _looks(control, _soon(), 6)
    assert control.executor.state.phase == "active"
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    monkeypatch.setattr(control_module, "blocked", lambda control, key: commands_module.blocked(
        control, key, (commands_module.Blocked("sigenergy", firmware="SPC118"),)
    ))
    await _looks(control, now, 6)
    assert control.recipe_blocked() and control.state() == "shadow_only" and control.allowed() == frozenset()
    assert control.executor.state.phase == "idle" and not control.executor.state.holds_snapshot
    # Nor is a report passed on a key this release holds back a source for the next key.
    monkeypatch.setattr(control_module, "blocked", lambda control, key: commands_module.blocked(
        control, key, (commands_module.Blocked("sigenergy", firmware="SPC117"),)
    ))
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    await _looks(control, now + timedelta(minutes=5), 2)
    assert not control.recipe_blocked() and control.state() == "waiting"


async def test_a_hold_variant_the_recipe_dropped_does_not_carry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    control.reports[control.firmware_key()]["modes"]["hold"] |= {"variant": "msc_no_discharge"}
    monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    await _looks(control, _soon(), 4)
    assert control.state() == "waiting" and plant.calls == []


async def test_a_failed_hardware_test_leaves_no_older_pass_to_run_on_after_an_update(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, control, recipe = await _waiting_site(hass, monkeypatch, hass_storage)
    control.reports.clear()
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    assert control.state() == "on"
    # A later hardware test on the same key fails: shadow only there, the test asked for again.
    _hardware_test_ends(control, hold="ignored", normal="restored")
    assert control.hardware_test_due() == control_module.NEED_HARDWARE_TEST_AGAIN and control.state() == "shadow_only"
    # An update does not bring control back on the report from before that failure.
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    control.facts = control._read_facts()
    assert control.hardware_test_due() is None and control.carried_from() is None
    assert control.state() == "waiting" and control.allowed() == frozenset()
    assert control._data()["gate"]["carry_after"] is not None
    await control.async_stop()


# --- what failed after the report that would carry ---------------------------------------------------------------------


def test_what_failed_on_a_later_key_whose_check_never_finished_is_not_carried_past_it() -> None:
    options = gate.Options(enabled=True, shadow_only=False)
    old = _report("A | sigen 1", per_phase="obeyed", hold=_row("obeyed", variant="pv_first"))
    waiting = "not_judgeable:battery_not_discharging"
    # On B the hold could not be judged, so its report never became a verdict, but the charge was ignored there.
    charge = _report("B | sigen 1", at="2026-11-05T10:00:00+00:00", hold=waiting, normal="skipped", charge="ignored")
    carried = gate.control_report(None, {"A | sigen 1": old, "B | sigen 1": charge}, key="C | sigen 1", brand="sigenergy")
    allowed = gate.allowed_actions(carried, options)
    assert carried["carried_from"] == "A | sigen 1" and "grid_charge" not in allowed
    assert {"hold", "discharge_house"} <= allowed
    # The capability per phase withdrawn on B while control ran there on A's report stays withdrawn on C.
    withdrawn = gate.per_phase_failed(
        {"key": "B | sigen 1", "brand": "sigenergy", "attempts": 0, "modes": {}}, "partial",
        dt_util.parse_datetime("2026-11-05T10:00:00+00:00"),
    )
    carried = gate.control_report(
        None, {"A | sigen 1": old, "B | sigen 1": withdrawn}, key="C | sigen 1", brand="sigenergy"
    )
    assert gate.per_phase(carried) != "verified" and "hold" in gate.allowed_actions(carried, options)
    # The hold variant that passed on A was ignored on B: no hold to run on, so nothing is carried.
    hold = _report("B | sigen 1", at="2026-11-05T10:00:00+00:00", normal="skipped", hold=_row(
        waiting, variants=[{"variant": "pv_first", "outcome": "ignored"}]))
    assert gate.control_report(
        None, {"A | sigen 1": old, "B | sigen 1": hold}, key="C | sigen 1", brand="sigenergy",
        variants=("pv_first", "other"),
    ) is None
    # What failed before the pass says nothing: the pass is later.
    before = _report("0 | sigen 1", at="2026-10-20T10:00:00+00:00", hold=waiting, normal="skipped", charge="ignored")
    carried = gate.control_report(None, {"0 | sigen 1": before, "A | sigen 1": old}, key="C | sigen 1", brand="sigenergy")
    assert "grid_charge" in gate.allowed_actions(carried, options)


def test_the_last_verdict_is_the_last_in_time_whatever_the_stored_offsets() -> None:
    # The night the clocks go back: the failure (01:10 UTC) is later than the pass (00:30 UTC), though its text sorts
    # before it.
    old = _report("A | sigen 1", at="2026-10-25T02:30:00+02:00")
    failed = _report("B | sigen 1", at="2026-10-25T02:10:00+01:00", hold="ignored")
    assert gate.control_report(
        None, {"A | sigen 1": old, "B | sigen 1": failed}, key="C | sigen 1", brand="sigenergy"
    ) is None


def test_a_key_whose_pass_is_void_is_no_source_and_hides_no_older_pass() -> None:
    old = _report("A | sigen 1")
    newer = _report("B | sigen 1", at="2026-11-05T10:00:00+00:00")
    assert gate.control_report(None, {"A | sigen 1": old}, key="C | sigen 1", brand="sigenergy", void={"A | sigen 1"}) is None
    # The last verdict is the void one (held back by the release, a person's failed hardware test): what passed
    # before it says nothing of the plant now.
    assert gate.control_report(
        None, {"A | sigen 1": old, "B | sigen 1": newer}, key="C | sigen 1", brand="sigenergy", void={"B | sigen 1"}
    ) is None
    assert gate.control_report(
        None, {"A | sigen 1": old, "B | sigen 1": newer}, key="C | sigen 1", brand="sigenergy", void={"A | sigen 1"}
    )["carried_from"] == "B | sigen 1"


async def test_a_hardware_test_that_failed_before_the_time_was_kept_leaves_no_pass_to_run_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant, control, recipe = await _waiting_site(hass, monkeypatch, hass_storage)
    control.reports.clear()
    _hardware_test_ends(control, **_LIMITS_ONLY_PASS)
    _hardware_test_ends(control, hold="ignored", normal="restored")
    failed_on = control.firmware_key()
    # As stored by a version that kept the failed key only (`hardware_failed`), not when it failed.
    control.carry_after = None
    hass.states.async_set(FIRMWARE, NEW_FIRMWARE)
    control.facts = control._read_facts()
    assert failed_on in control.hardware_failed and control.firmware_key() != failed_on
    assert control.carried_from() is None and control.state() == "waiting" and control.allowed() == frozenset()
    await control.async_stop()
