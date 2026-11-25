"""The export limit's own switch and gates through the battery control shell, on a site with the owner's Sigenergy
plant: the switch alone arms it (a site without a battery too), the one-time migration from the battery opt-in, an
untested recipe on a beta tester's site only, the check that comes first where nothing states that 0 means no export,
short positive gaps bridged, the day's stretches, the block that ends with the day, and what the card is shown."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.spotnav.battery import commands as commands_module
from custom_components.spotnav.battery import export_shell as export_shell_module
from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.commands import SIGENERGY_EXPORT_CONTROL
from custom_components.spotnav.battery.control import BatteryControl, ControlNotReachable
from custom_components.spotnav.battery.executor import Executor
from custom_components.spotnav.battery.export_gate import (
    BRIDGE_GAP_S,
    CHECK_MIN_EXPORT_KW,
    CHECK_RETRY_S,
    CURTAIL_CHANGES_A_DAY,
    ExportGate,
    bridges,
)
from custom_components.spotnav.const import CONF_BATTERY_AGGREGATE_POWER_ENTITY, CONF_GRID_POWER_SOURCE
from tests.test_battery_control import (
    EXPORT,
    SWITCH,
    _export_limit,
    _options,
    _passed,
    _site,
)

pytestmark = pytest.mark.usefixtures("offline_relay")

SET_ZERO = ("number", "set_value", {"entity_id": EXPORT, "value": 0.0})
SET_BACK = ("number", "set_value", {"entity_id": EXPORT, "value": 11.5})


def _switch(hass: HomeAssistant, site: Any, on: bool) -> None:
    hass.config_entries.async_update_entry(site, data={**site.data, gate.CONF_EXPORT_CURTAIL: on})


async def _control(hass, controller, sell, plan=None) -> BatteryControl:
    control = BatteryControl(
        hass, controller, plan_source=(lambda _now: None) if plan is None else plan,
        sell_source=sell if callable(sell) else (lambda _now: sell),
    )
    await control.async_start()
    return control


async def _looks(control: BatteryControl, start: datetime, count: int, step_s: float = 10.0) -> datetime:
    now = start
    for _ in range(count):
        await control.async_look(now)
        now += timedelta(seconds=step_s)
    return now


def _exporting(monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant, export_kw: float = 3.0) -> None:
    """The site exports `export_kw` until its export limit reads 0, as a plant that obeys does."""
    monkeypatch.setattr(
        BatteryControl, "_grid_import_kw", lambda self: 0.0 if hass.states.get(EXPORT).state == "0" else -export_kw
    )
    _measured(monkeypatch)


def _measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The site's measurement is healthy (the test site has no meter of its own)."""
    monkeypatch.setattr(export_shell_module.ExportShell, "_measurement_ok", lambda self: True)


# --- the switch ------------------------------------------------------------------------------------------------------


async def test_the_switch_alone_arms_the_export_limit_whatever_the_battery_options_say(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    control = await _control(hass, controller, -12.0)
    # The battery's control is off and shadow only: the export limit has its own switch.
    assert control.options().enabled is False and control.export.state() == "off"
    with pytest.raises(ControlNotReachable):
        await control.async_export_tick()
    now = await _looks(control, dt_util.now(), 3)
    assert plant.calls == []
    _switch(hass, site, True)
    assert control.export.state() == "on"
    now = await _looks(control, now, 4)
    assert plant.calls == [SET_ZERO]
    assert control.export_executor.state.phase == "active" and control.state() == "off"
    assert hass.states.get(SWITCH).state == "off", "the battery is never touched"
    # Turned off: handed back before the write's answer.
    plant.calls.clear()
    await control.async_update_options({gate.CONF_EXPORT_CURTAIL: False})
    assert plant.calls == [SET_BACK]
    assert control.export_executor.state.phase == "idle" and hass.states.get(EXPORT).state == "11.5"
    await control.async_stop()


async def test_turning_the_battery_control_off_leaves_a_running_curtail_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    control = await _control(hass, controller, -12.0)
    _switch(hass, site, True)
    _options(hass, site, enabled=True, shadow_only=False)
    control._enabled_seen = True
    _passed(control)
    await _looks(control, dt_util.now(), 4)
    assert SET_ZERO in plant.calls and control.export_executor.state.phase == "active"
    # The battery's opt-in off: the export limit has its own switch, still on, so it is not handed back and set again.
    plant.calls.clear()
    await control.async_update_options({gate.CONF_ENABLED: False})
    assert all(data.get("entity_id") != EXPORT for _domain, _service, data in plant.calls)
    assert control.export_executor.state.phase == "active" and hass.states.get(EXPORT).state == "0"
    assert control.export.gate.stretches == 1
    await control.async_stop()
    assert hass.states.get(EXPORT).state == "11.5"


async def test_the_battery_opt_in_no_longer_arms_the_export_limit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    control = await _control(hass, controller, -12.0)
    _switch(hass, site, False)  # the person's own choice, stored
    _options(hass, site, enabled=True, shadow_only=False)
    control._enabled_seen = True
    _passed(control)
    assert control.state() == "on" and control.export.state() == "off"
    await _looks(control, dt_util.now(), 4)
    assert all(data.get("entity_id") != EXPORT for _domain, _service, data in plant.calls)
    assert site.data[gate.CONF_EXPORT_CURTAIL] is False
    await control.async_stop()


async def test_a_site_without_a_battery_has_the_export_limit_from_its_grid_meter(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    battery = controller.config[CONF_BATTERY_AGGREGATE_POWER_ENTITY]
    controller.config = {
        key: value for key, value in controller.config.items() if key != CONF_BATTERY_AGGREGATE_POWER_ENTITY
    } | {CONF_GRID_POWER_SOURCE: {"power": battery}}
    control = await _control(hass, controller, -12.0)
    await _looks(control, dt_util.now(), 2)
    assert control.facts.battery_entity_id is None and control.status()["battery_found"] is False
    shown = control.export.status()
    assert shown["found"] is True and shown["brand"] == "sigenergy" and shown["enabled"] is False
    assert plant.calls == []
    answer = await control.async_update_options({gate.CONF_EXPORT_CURTAIL: True})
    assert answer["battery_found"] is False
    await _looks(control, dt_util.now() + timedelta(minutes=1), 4)
    assert plant.calls == [SET_ZERO]
    assert control.export.status()["state"] == "on"
    await control.async_stop()
    assert hass.states.get(EXPORT).state == "11.5"


# --- the migration ---------------------------------------------------------------------------------------------------


async def test_a_site_whose_battery_control_already_writes_gets_the_switch_turned_on_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    control = await _control(hass, controller, -12.0)
    _options(hass, site, enabled=True, shadow_only=False)
    control._enabled_seen = True
    _passed(control)
    assert gate.CONF_EXPORT_CURTAIL not in site.data
    now = await _looks(control, dt_util.now(), 4)
    assert site.data[gate.CONF_EXPORT_CURTAIL] is True
    assert [row["reason"] for row in control.log if row["kind"] == "export_migrated"] == ["battery_control_wrote"]
    assert SET_ZERO in plant.calls, "nothing stops by the update"
    # Once: the person's later choice is never undone, also after a restart.
    await control.async_update_options({gate.CONF_EXPORT_CURTAIL: False})
    await control.async_stop()
    again = await _control(hass, controller, -12.0)
    assert again.export.gate.migrated is True
    await _looks(again, now + timedelta(minutes=5), 3)
    assert site.data[gate.CONF_EXPORT_CURTAIL] is False
    await again.async_stop()


async def test_a_site_whose_battery_control_does_not_write_starts_with_the_switch_off(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = await _control(hass, controller, -12.0)
    _options(hass, site, enabled=True, shadow_only=True)
    await _looks(control, dt_util.now(), 3)
    assert gate.CONF_EXPORT_CURTAIL not in site.data and control.export.gate.migrated is True
    # Battery control turned on later does not arm it: the decision was made once.
    _options(hass, site, enabled=True, shadow_only=False)
    await _looks(control, dt_util.now() + timedelta(minutes=1), 3)
    assert gate.CONF_EXPORT_CURTAIL not in site.data and control.export.state() == "off"
    assert all(data.get("entity_id") != EXPORT for _domain, _service, data in plant.calls)
    await control.async_stop()


async def test_only_a_brand_the_battery_opt_in_armed_before_is_migrated(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    assert commands_module.EXPORT_ARMED_BY_BATTERY == frozenset({"sigenergy"})
    monkeypatch.setattr(export_shell_module, "EXPORT_ARMED_BY_BATTERY", frozenset({"another"}))
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = await _control(hass, controller, -12.0)
    _options(hass, site, enabled=True, shadow_only=False)
    control._enabled_seen = True
    _passed(control)
    await _looks(control, dt_util.now(), 3)
    assert gate.CONF_EXPORT_CURTAIL not in site.data and control.export.state() == "off"
    await control.async_stop()


# --- an untested recipe ---------------------------------------------------------------------------------------------


async def test_an_untested_export_recipe_writes_on_a_beta_testers_site_only(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    monkeypatch.setattr(export_shell_module, "learning_gate_open", lambda _hass, _site: False)
    control = await _control(hass, controller, -12.0)
    _switch(hass, site, True)
    assert control.export.state() == "beta_only"
    now = await _looks(control, dt_util.now(), 4)
    assert plant.calls == []
    shown = control.export.status()
    assert shown["state"] == "beta_only" and shown["need"] == "beta_only" and shown["beta"] is True
    # The site becomes a beta tester's: it writes.
    monkeypatch.setattr(export_shell_module, "learning_gate_open", lambda _hass, _site: True)
    now = await _looks(control, now, 4)
    assert plant.calls == [SET_ZERO]
    # A beta period that ends hands the limit back.
    monkeypatch.setattr(export_shell_module, "learning_gate_open", lambda _hass, _site: False)
    plant.calls.clear()
    await _looks(control, now, 4)
    assert plant.calls == [SET_BACK]
    # A recipe marked field tested needs no beta site.
    control.export_executor = Executor(replace(SIGENERGY_EXPORT_CONTROL, field_tested=True))
    assert control.export.state() == "on"
    await control.async_stop()


# --- the check first ------------------------------------------------------------------------------------------------


async def test_where_zero_is_not_stated_the_check_runs_at_the_next_export_before_any_ordinary_curtail(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = await _control(hass, controller, -12.0)
    control.export_executor = Executor(replace(SIGENERGY_EXPORT_CONTROL, zero_stated=False))
    _switch(hass, site, True)
    # A negative price but the site does not export: nothing to judge, so nothing is written yet.
    _measured(monkeypatch)
    monkeypatch.setattr(BatteryControl, "_grid_import_kw", lambda self: 0.4)
    now = await _looks(control, dt_util.now(), 4)
    assert plant.calls == [] and control.export.waiting == "check"
    assert control.export.verification is None
    # The site exports: the check runs by itself, judged on the grid's power, and hands back.
    _exporting(monkeypatch, hass)
    now = await _looks(control, now, 45)
    assert plant.calls[:2] == [SET_ZERO, SET_BACK]
    key = control.export.snapshot()["key"]
    assert control.export.gate.check_passed(key)
    kinds = [row["kind"] for row in control.log if row["kind"].startswith("export_check")]
    assert kinds == ["export_check_started", "export_check_done"]
    assert [row["outcome"] for row in control.log if row["kind"] == "export_check_done"] == ["obeyed"]
    # Then the ordinary curtail (the price is still negative), and no second check.
    now = await _looks(control, now, 20)
    assert plant.calls == [SET_ZERO, SET_BACK, SET_ZERO]
    assert control.export_executor.state.phase == "active" and control.export.waiting is None
    await control.async_stop()


async def test_a_check_that_shows_the_export_goes_on_holds_the_limit_back_until_switched_off_and_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = await _control(hass, controller, -12.0)
    control.export_executor = Executor(replace(SIGENERGY_EXPORT_CONTROL, zero_stated=False))
    _switch(hass, site, True)
    # The inverter takes the value and goes on exporting (0 does not mean "no export" here).
    _measured(monkeypatch)
    monkeypatch.setattr(BatteryControl, "_grid_import_kw", lambda self: -3.0)
    now = await _looks(control, dt_util.now(), 60)
    assert plant.calls == [SET_ZERO, SET_BACK]
    key = control.export.snapshot()["key"]
    # The executor's own monitor saw it first and blocked the limit; the check did not pass.
    assert control.export_executor.state.blocked == "not_obeyed:curtail_export:no_effect"
    assert not control.export.gate.check_passed(key)
    assert control.export.status()["need"] == "not_obeyed"
    now = await _looks(control, now, 12)
    assert plant.calls == [SET_ZERO, SET_BACK] and control.export.waiting == "check"
    # Switched off and on: the block is lifted and the check is tried again.
    await control.async_update_options({gate.CONF_EXPORT_CURTAIL: False})
    await control.async_update_options({gate.CONF_EXPORT_CURTAIL: True})
    assert control.export_executor.state.phase != "blocked" and control.export.status()["need"] != "not_obeyed"
    await control.async_stop()


async def test_a_check_counts_what_the_battery_and_the_chargers_take_as_theirs(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = await _control(hass, controller, -12.0)
    control.export_executor = Executor(replace(SIGENERGY_EXPORT_CONTROL, zero_stated=False))
    _switch(hass, site, True)
    _measured(monkeypatch)
    # The car starts charging 3 kW as the limit is set: the site imports 2.5 kW, all of it the car's.
    monkeypatch.setattr(
        BatteryControl, "_grid_import_kw", lambda self: 2.5 if hass.states.get(EXPORT).state == "0" else -3.0
    )
    monkeypatch.setattr(export_shell_module.ExportShell, "excused_kw", lambda self: 3.0)
    await _looks(control, dt_util.now(), 45)
    assert plant.calls[:2] == [SET_ZERO, SET_BACK]
    assert [row["outcome"] for row in control.log if row["kind"] == "export_check_done"] == ["obeyed"]
    assert control.export.gate.check_passed(control.export.snapshot()["key"])
    await control.async_stop()


async def test_a_check_ends_when_the_battery_plan_sells(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    plan: dict[str, Any] = {"now": None}
    control = await _control(hass, controller, 20.0, plan=lambda _now: plan["now"])
    control.export_executor = Executor(replace(SIGENERGY_EXPORT_CONTROL, zero_stated=False))
    _switch(hass, site, True)
    _exporting(monkeypatch, hass)
    now = await _looks(control, dt_util.now(), 4)
    assert plant.calls == [SET_ZERO] and control.export.verification is not None
    # The plan sells from the battery: a limit at 0 would stop the sale, so the check ends and hands back at once.
    plan["now"] = ("export_discharge", 3.0, None)
    await _looks(control, now, 4)
    assert plant.calls == [SET_ZERO, SET_BACK] and control.export.verification is None
    key = control.export.snapshot()["key"]
    assert not control.export.gate.check_passed(key) and not control.export.gate.check_failed(key)
    await control.async_stop()


def test_a_stored_check_that_is_no_text_is_no_outcome() -> None:
    state = ExportGate.from_dict({"checks": {"k": {"outcome": ["ignored"], "at": 5}, "j": {"outcome": "ignored"}}})
    assert state.check_outcome("k") is None
    state.forget_failed()
    assert "j" not in state.checks and state.check_due("k", dt_util.now(), -3.0)


def test_a_failed_check_holds_ordinary_curtails_back_until_it_is_forgotten() -> None:
    state = ExportGate()
    now = dt_util.now()
    state.keep_check("k", "ignored", now)
    assert state.check_failed("k") and state.check_needed("k", zero_stated=False)
    assert not state.check_due("k", now + timedelta(days=1), -3.0), "never tried again by itself"
    state.forget_failed()
    assert not state.check_failed("k") and state.check_due("k", now, -3.0)


def test_a_check_waits_for_enough_export_and_is_not_tried_again_at_once() -> None:
    state = ExportGate()
    now = dt_util.now()
    assert state.check_needed("k", zero_stated=False) and not state.check_needed("k", zero_stated=True)
    assert not state.check_due("k", now, None)
    assert not state.check_due("k", now, -(CHECK_MIN_EXPORT_KW - 0.1))
    assert state.check_due("k", now, -CHECK_MIN_EXPORT_KW)
    state.check_started(now)
    assert not state.check_due("k", now + timedelta(seconds=CHECK_RETRY_S - 1), -3.0)
    assert state.check_due("k", now + timedelta(seconds=CHECK_RETRY_S), -3.0)
    state.keep_check("k", "obeyed", now)
    assert not state.check_needed("k", zero_stated=False) and not state.check_due("k", now, -3.0)
    # Another firmware is checked again.
    assert state.check_needed("other", zero_stated=False)
    assert ExportGate.from_dict(state.as_dict()).as_dict() == state.as_dict()
    assert ExportGate.from_dict("x").as_dict() == ExportGate().as_dict()


# --- gaps and stretches --------------------------------------------------------------------------------------------


def _sells(start: datetime, minutes: list[tuple[int, float]]):
    """A sell price by the minute since `start`: `(from minute, price)` rows."""

    def sell(now: datetime) -> float:
        elapsed = (now - start).total_seconds() / 60.0
        price = minutes[0][1]
        for since, value in minutes:
            if elapsed >= since:
                price = value
        return price

    return sell


def test_a_gap_of_at_most_half_an_hour_is_bridged_and_a_longer_one_is_not() -> None:
    start = dt_util.now()
    last_negative = start + timedelta(minutes=15) - timedelta(seconds=10)
    now = start + timedelta(minutes=15)
    assert bridges(now, last_negative, _sells(start, [(0, -5.0), (15, 4.0), (30, -5.0)]))
    assert bridges(now, last_negative, _sells(start, [(0, -5.0), (15, 4.0), (45, -5.0)]))
    assert not bridges(now, last_negative, _sells(start, [(0, -5.0), (15, 4.0), (60, -5.0)]))
    assert not bridges(now, last_negative, _sells(start, [(0, -5.0), (15, 4.0)]))
    assert not bridges(now, None, _sells(start, [(0, -5.0)]))
    assert BRIDGE_GAP_S == 1800.0


async def test_a_short_positive_gap_leaves_the_limit_at_zero_and_a_long_one_hands_it_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    start = dt_util.now()
    # Negative, 15 minutes positive, negative again, then positive for good.
    control = await _control(hass, controller, _sells(start, [(0, -5.0), (15, 4.0), (30, -5.0), (45, 4.0)]))
    _switch(hass, site, True)
    now = await _looks(control, start, 4)
    assert plant.calls == [SET_ZERO]
    for minute in (14, 16, 22, 29, 31, 44):
        await _looks(control, start + timedelta(minutes=minute), 2)
        assert plant.calls == [SET_ZERO], minute
        assert control.export_executor.state.phase == "active"
    # No negative price ahead: handed back.
    await _looks(control, start + timedelta(minutes=46), 4)
    assert plant.calls == [SET_ZERO, SET_BACK]
    assert control.export.gate.stretches == 1, "one stretch, two writes"
    await control.async_stop()


async def test_a_gap_is_not_bridged_while_the_battery_plan_sells(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    start = dt_util.now()
    plan: dict[str, Any] = {"now": None}
    control = await _control(
        hass, controller, _sells(start, [(0, -5.0), (15, 4.0), (30, -5.0)]), plan=lambda _now: plan["now"]
    )
    _switch(hass, site, True)
    await _looks(control, start, 4)
    assert plant.calls == [SET_ZERO]
    # The plan sells from the battery in the gap: a limit at 0 would stop the sale, so it is handed back.
    plan["now"] = ("export_discharge", 3.0, None)
    await _looks(control, start + timedelta(minutes=16), 4)
    assert plant.calls == [SET_ZERO, SET_BACK]
    await control.async_stop()


async def test_at_most_six_stretches_start_in_a_day(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    sell = {"now": -5.0}
    control = await _control(hass, controller, lambda _now: sell["now"])
    _switch(hass, site, True)
    start = dt_util.now().replace(hour=1, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert CURTAIL_CHANGES_A_DAY == 6
    for stretch in range(CURTAIL_CHANGES_A_DAY + 1):
        at = start + timedelta(hours=stretch)
        sell["now"] = -5.0
        await _looks(control, at, 4)
        sell["now"] = 4.0
        await _looks(control, at + timedelta(minutes=5), 4)
    zeros = [call for call in plant.calls if call == SET_ZERO]
    assert len(zeros) == CURTAIL_CHANGES_A_DAY and control.export.gate.stretches == CURTAIL_CHANGES_A_DAY
    assert hass.states.get(EXPORT).state == "11.5"
    sell["now"] = -5.0
    await _looks(control, start + timedelta(hours=8), 3)
    assert control.export.waiting == "stretches_spent"
    # The next day counts anew.
    await _looks(control, start + timedelta(days=1), 4)
    assert len([call for call in plant.calls if call == SET_ZERO]) == CURTAIL_CHANGES_A_DAY + 1
    await control.async_stop()


async def test_the_first_curtail_on_a_firmware_is_logged_with_its_verdict(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _exporting(monkeypatch, hass)
    sell = {"now": -5.0}
    control = await _control(hass, controller, lambda _now: sell["now"])
    _switch(hass, site, True)
    now = await _looks(control, dt_util.now(), 4)
    sell["now"] = 4.0
    now = await _looks(control, now + timedelta(minutes=10), 4)
    rows = [row for row in control.log if row["kind"] == "export_first_curtail"]
    assert len(rows) == 1 and rows[0]["verdict"] == "clean" and rows[0]["brand"] == "sigenergy"
    assert rows[0]["export_before_kw"] == -3.0
    # The second one is not a first.
    sell["now"] = -5.0
    now = await _looks(control, now + timedelta(minutes=10), 4)
    sell["now"] = 4.0
    await _looks(control, now + timedelta(minutes=10), 4)
    assert len([row for row in control.log if row["kind"] == "export_first_curtail"]) == 1
    assert control.snapshot()["export_gate"]["first"]
    await control.async_stop()


# --- blocks and what the card is shown ------------------------------------------------------------------------------


async def test_a_limit_someone_else_changed_is_left_alone_for_the_day(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    monkeypatch.setattr(BatteryControl, "_grid_import_kw", lambda self: -3.0 if hass.states.get(EXPORT).state == "11.5" else 0.0)
    control = await _control(hass, controller, -5.0)
    _switch(hass, site, True)
    start = dt_util.now().replace(hour=10, minute=0, second=0, microsecond=0)
    now = await _looks(control, start, 4)
    assert plant.calls == [SET_ZERO]
    hass.states.async_set(EXPORT, "5", {"unit_of_measurement": "kW", "step": 0.001})  # the installer's app
    now = await _looks(control, now, 30)
    assert control.export_executor.state.phase == "blocked"
    assert control.export_executor.state.blocked.startswith("changed_outside_spotnav")
    assert plant.calls == [SET_ZERO] and hass.states.get(EXPORT).state == "5"
    await _looks(control, start + timedelta(hours=6), 4)
    assert plant.calls == [SET_ZERO], "nothing is written again that day"
    await _looks(control, start + timedelta(days=1), 4)
    assert plant.calls[-1] == SET_ZERO and len(plant.calls) == 2
    assert [row["reason"] for row in control.log if row["kind"] == "export_unblocked"] == ["next_day"]
    await control.async_stop()


async def test_a_limit_under_which_the_import_rose_is_tried_again_the_next_day(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    _measured(monkeypatch)
    # A house load starts as the limit is set (or the limit stops the inverter): the site imports.
    load = {"kw": 2.5}
    monkeypatch.setattr(
        BatteryControl, "_grid_import_kw", lambda self: load["kw"] if hass.states.get(EXPORT).state == "0" else -3.0
    )
    control = await _control(hass, controller, -5.0)
    _switch(hass, site, True)
    start = dt_util.now().replace(hour=10, minute=0, second=0, microsecond=0)
    now = await _looks(control, start, 45)
    assert plant.calls == [SET_ZERO, SET_BACK]
    assert control.export_executor.state.blocked == "not_obeyed:curtail_export:import_rose"
    # The card says so, and that it lifts by itself: nothing for the person to do.
    assert control.export.status()["need"] == "import_rose"
    await _looks(control, now + timedelta(hours=6), 4)
    assert plant.calls == [SET_ZERO, SET_BACK], "nothing is written again that day"
    # The next day it is tried again by itself: the load was the house's own.
    load["kw"] = 0.0
    await _looks(control, start + timedelta(days=1), 45)
    assert plant.calls == [SET_ZERO, SET_BACK, SET_ZERO]
    assert control.export_executor.state.phase == "active" and control.export.status()["need"] != "import_rose"
    assert [row["reason"] for row in control.log if row["kind"] == "export_unblocked"] == ["next_day"]
    await control.async_stop()


async def test_the_card_is_shown_the_switch_the_would_be_saving_and_what_the_person_can_act_on(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    control = await _control(hass, controller, -5.0)
    # No export control in Home Assistant: nothing to show a switch for.
    shown = control.export.status()
    assert shown["found"] is False and shown["need"] is None and shown["state"] == "no_control"
    _export_limit(hass)
    count = controller.battery_shadow.curtail.days.setdefault("2026-11-20", __import__(
        "custom_components.spotnav.battery.export_curtail", fromlist=["DayCount"]).DayCount())
    count.kwh, count.saving = 12.345, 67.89
    shown = control.export.status()
    assert shown["found"] is True and shown["enabled"] is False and shown["state"] == "off"
    assert shown["brand"] == "sigenergy" and shown["beta"] is True and shown["need"] is None
    assert shown["saving"]["days"] == 30 and shown["saving"]["kwh"] == 12.35 and shown["saving"]["minor"] == 67.9
    # A limit never set: SpotNav never writes the plant's maximum, so the person sets a limit first.
    hass.states.async_set(EXPORT, "unknown", {"unit_of_measurement": "kW", "step": 0.001})
    shown = control.export.status()
    assert shown["need"] == "limit_unset" and shown["entity_id"] == EXPORT
    _switch(hass, site, True)
    await _looks(control, dt_util.now(), 3)
    assert hass.states.get(EXPORT).state == "unknown"
    # This site measures currents only and has no total grid power: nothing could judge the limit, and it says so.
    hass.states.async_set(EXPORT, "11.5", {"unit_of_measurement": "kW", "step": 0.001})
    assert control.export.status()["need"] == "no_grid_reading"
    await control.async_stop()


async def test_a_disabled_export_limit_entity_is_named_for_the_person_to_enable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    from homeassistant.helpers import entity_registry as er

    _site_entry, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    er.async_get(hass).async_update_entity(EXPORT, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    control = await _control(hass, controller, -5.0)
    shown = control.export.status()
    assert shown["found"] is True and shown["need"] == "control_disabled" and shown["entity_id"] == EXPORT
    await control.async_stop()


# --- the card's and the apps' commands ------------------------------------------------------------------------------


async def test_the_site_settings_view_carries_the_switch_and_an_administrator_writes_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], hass_ws_client: Any,
) -> None:
    from custom_components.spotnav.api.battery_control import async_battery_control_answer
    from custom_components.spotnav.api.site_view import site_settings_view

    site, controller, _plant = await _site(hass, monkeypatch, hass_storage, own=True)
    await hass.async_block_till_done()
    _export_limit(hass)
    client = await hass_ws_client(hass)

    async def ask(msg_id: int, **message: Any) -> dict[str, Any]:
        await client.send_json({"id": msg_id, "api_version": 1, "site_id": site.entry_id, **message})
        return (await client.receive_json())["result"]

    block = (await ask(1, type="spotnav/get_site_settings_view"))["export_curtail"]
    assert block["found"] is True and block["enabled"] is False and block["writable"] is True
    assert block["brand"] == "sigenergy" and block["state"] == "off"
    assert set(block) == {"found", "enabled", "brand", "state", "beta", "need", "entity_id", "saving", "writable"}
    assert set(block["saving"]) == {"days", "kwh", "minor", "blocked_minor", "major_unit", "minor_unit"}
    # Written with the battery control command's changes; the battery's own options are untouched.
    result = await ask(2, type="spotnav/battery_control", changes={"export_curtail_enabled": True})
    assert result["ok"] is True and "export_curtail_enabled" not in result["status"]["options"]
    assert result["status"]["options"][gate.CONF_ENABLED] is False
    assert site.data[gate.CONF_EXPORT_CURTAIL] is True
    block = (await ask(3, type="spotnav/get_site_settings_view"))["export_curtail"]
    assert block["enabled"] is True and block["state"] == "on"
    assert (await ask(4, type="spotnav/battery_control", changes={"export_curtail_enabled": "yes"}))["error"] == (
        "spotnav_invalid_value"
    )
    # A webhook's secret (the apps) reads it and never writes it; nor does a user who is no administrator.
    view = site_settings_view(hass, site, writable=True, active_control_writable=False)
    assert view["export_curtail"]["writable"] is False and view["export_curtail"]["enabled"] is True
    refused = await async_battery_control_answer(
        hass, site, {"export_curtail_enabled": False}, admin=False, over_webhook=True
    )
    assert refused["error"] == "spotnav_not_permitted_over_webhook"
    refused = await async_battery_control_answer(hass, site, {"export_curtail_enabled": False}, admin=False)
    assert refused["error"] == "spotnav_not_admin"
    assert site.data[gate.CONF_EXPORT_CURTAIL] is True
