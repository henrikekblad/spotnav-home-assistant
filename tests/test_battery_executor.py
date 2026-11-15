"""The battery's write path against fake entities: every brand's commands, the order and read-back of each write,
the fuse clamp, and the hand-back on every exit path (stop, stale data, fuse, management change, a write not taken,
a change by someone else, no effect, a restart)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.spotnav.battery.commands import (
    HUAWEI_CONTROL,
    SIGENERGY_CONTROL,
    SOLAREDGE_CONTROL,
    SOLAX_CONTROL,
    WATCHDOG_RENEW_S,
    control_for,
    huawei_forcible,
    service_call,
    table,
)
from custom_components.spotnav.battery.executor import (
    DAILY_WRITE_CAP,
    HEADROOM_MARGIN_A,
    MANAGEMENT_GRACE_S,
    PEAK_COVER_A,
    PEAK_RECOVER_S,
    PEAK_REHOLD_S,
    RATE_UP_DWELL_S,
    REARM_S,
    RESTORE_ATTEMPTS,
    RESTORE_HEADROOM_A,
    ExecState,
    Executor,
    grid_charge_cap_kw,
    preview,
)
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.ledger import READBACK_DEADLINE_S, Ledger
from custom_components.spotnav.battery.monitor import GRACE_S, Monitor
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S
from tests.battery_control_fakes import (
    HUAWEI_STATES,
    SIGENERGY_STATES,
    SOLAREDGE_STATES,
    SOLAX_STATES,
    T0,
    TICK,
    FakeBattery,
    events,
    inputs,
    run,
    written,
)


def _sigenergy(**kwargs) -> tuple[Executor, FakeBattery]:
    return Executor(SIGENERGY_CONTROL), FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), **kwargs)


def _held(executor: Executor, battery: FakeBattery) -> object:
    now, results = run(executor, battery, T0, 6, "hold")
    assert executor.state.phase == "active"
    return now


# --- the command table ---------------------------------------------------------------------------------------------


def test_every_brand_with_a_control_has_a_command_for_every_planned_action_and_the_rest_have_none() -> None:
    assert {control.brand for control in (SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL)} == {
        "sigenergy", "solaredge", "huawei", "solax"
    }
    for brand in ("victron", "goodwe", "enphase", None):
        assert control_for(brand) is None
    rows = table()
    for control in (SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL):
        actions = {row["action"] for row in rows if row["brand"] == control.brand}
        assert actions == {"hold", "grid_charge", "discharge_house", "export_discharge", "self_consume"}
    # Sigenergy: Remote EMS with the limits; never the discharge cut-off SoC.
    sig = SIGENERGY_CONTROL.wanted("grid_charge", 3.5)
    assert sig == {"charge_limit": 3.5, "discharge_limit": "<snapshot>", "remote_ems": True,
                   "control_mode": "Command Charging (Grid First)"}
    assert all("cut_off" not in role.match.pattern.pattern for role in SIGENERGY_CONTROL.roles)
    assert SOLAREDGE_CONTROL.wanted("grid_charge", 3.5)["charge_limit"] == 3500.0
    assert SOLAREDGE_CONTROL.wanted("hold", 0)["command_timeout"] == 1800.0
    assert HUAWEI_CONTROL.wanted("grid_charge", 3.25)["forcible"] == "charge:3250"
    assert SOLAX_CONTROL.wanted("export_discharge", 2.0)["active_power"] == -2000.0


def test_service_calls_use_core_entity_services_and_huaweis_own_services() -> None:
    assert service_call("number", "number.x", 2.0) == ("number", "set_value", {"entity_id": "number.x", "value": 2.0})
    assert service_call("select", "select.x", "A") == ("select", "select_option", {"entity_id": "select.x", "option": "A"})
    assert service_call("switch", "switch.x", True)[1] == "turn_on"
    assert service_call("switch", "switch.x", False)[1] == "turn_off"
    assert service_call("button", "button.x", True)[:2] == ("button", "press")
    services = HUAWEI_CONTROL.role("forcible").services
    assert service_call("service", None, "charge:3000", device_id="D", services=services, watchdog_s=1800) == (
        "huawei_solar", "forcible_charge", {"device_id": "D", "power": 3000, "duration": 30}
    )
    assert service_call("service", None, "stop", device_id="D", services=services) == (
        "huawei_solar", "stop_forcible_charge", {"device_id": "D"}
    )
    with pytest.raises(ValueError):
        service_call("service", None, "charge:1", services=services)  # no device


def test_huaweis_summary_sensor_reads_as_a_comparable_value() -> None:
    assert huawei_forcible("Stopped") == "stop"
    assert huawei_forcible("Charging at 3000W for 60 minutes") == "charge:3000"
    assert huawei_forcible("Discharging at 2500W until 20%") == "discharge:2500"
    assert huawei_forcible("unavailable") is None


# --- entering, in order, each write read back ------------------------------------------------------------------------


def test_a_sigenergy_hold_is_written_limit_first_then_remote_ems_then_its_mode_each_after_the_last_read_back() -> None:
    executor, battery = _sigenergy()
    _now, results = run(executor, battery, T0, 6, "hold")
    assert results[0].persist_first, "the snapshot is stored before the first write"
    assert written(results) == [
        ("discharge_limit", 0.0), ("remote_ems", True), ("control_mode", "Maximum Self Consumption")
    ]
    # One write per look, the next only after the last read back.
    assert [len(result.ops) for result in results[:4]] == [1, 1, 1, 0]
    assert len(events(results, "confirmed")) == 3
    state = executor.state
    assert state.phase == "active" and state.action == "hold"
    # The charge limit read "unknown" (never set): its restore value is the rating, not "unknown".
    assert state.restore_values == {"charge_limit": 10.0, "discharge_limit": 23.04}
    assert battery.states["charge_limit"] == "unknown", "a limit the action leaves alone is not written"
    assert state.snapshot == {"charge_limit": "unknown", "discharge_limit": "23.04"}


def test_plain_self_consumption_hands_a_sigenergy_back_in_the_restore_order() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    assert battery.states["control_mode"] == "Command Charging (Grid First)"
    assert battery.states["charge_limit"] == "4"
    _now, results = run(executor, battery, now, 8, "self_consume")
    assert written(results) == [
        ("control_mode", "Maximum Self Consumption"), ("remote_ems", False), ("charge_limit", 10.0)
    ]
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot
    assert battery.states["remote_ems"] == "off" and battery.states["discharge_limit"] == "23.04"
    assert executor.ledger.as_dict()["claims"] == {}, "every claim is released once handed back"
    assert events(results, "handed_back")[0]["reason"] == "plan_self_consume"


def test_a_plant_not_in_maximum_self_consumption_or_with_remote_ems_on_is_never_taken() -> None:
    executor, battery = _sigenergy(work_mode_override="ai_mode")
    _now, results = run(executor, battery, T0, 3, "hold")
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"].startswith("self_managed")
    executor, battery = _sigenergy()
    battery.states["remote_ems"] = "on"
    _now, results = run(executor, battery, T0, 3, "hold")
    assert written(results) == [] and events(results, "not_started")[0]["reason"].startswith("self_managed")


def test_a_control_that_cannot_be_read_or_restored_is_not_started() -> None:
    executor, battery = _sigenergy(missing={"control_mode"})
    _now, results = run(executor, battery, T0, 2, "hold")
    assert events(results, "not_started")[0]["reason"] == "control_not_found:control_mode"
    executor, battery = _sigenergy()
    battery.states["discharge_limit"] = "unavailable"
    _now, results = run(executor, battery, T0, 2, "hold")
    assert events(results, "not_started")[0]["reason"] == "no_restore_value:discharge_limit"
    assert written(results) == []


# --- failures: a write not taken, a call that raises, a change by someone else ----------------------------------------


def test_a_read_only_integration_drops_the_write_and_the_control_is_blocked_without_a_second_try() -> None:
    executor, battery = _sigenergy(read_only=True)
    looks = int(READBACK_DEADLINE_S / TICK.total_seconds()) + 4
    _now, results = run(executor, battery, T0, looks, "hold")
    assert written(results) == [("discharge_limit", 0.0)], "the same write is never repeated"
    assert events(results, "not_taken")[0]["reason"] == "discharge_limit"
    assert executor.state.phase == "blocked"
    assert executor.state.blocked.startswith("write_not_taken:discharge_limit")
    _now, results = run(executor, battery, _now, 5, "hold")
    assert written(results) == [], "blocked until reset"
    executor.reset()
    assert executor.state.phase == "idle"


def test_a_service_call_that_raises_counts_as_not_taken() -> None:
    executor, battery = _sigenergy(raises={"remote_ems"})
    _now, results = run(executor, battery, T0, 6, "hold")
    assert events(results, "not_taken")[0]["why"] == "service_failed"
    assert executor.state.blocked.startswith("write_not_taken:remote_ems")
    # The hand-back put the discharge limit it had written back.
    assert battery.states["discharge_limit"] == "23.04"


def test_a_change_by_someone_else_is_given_up_and_never_written_back_over() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    battery.states["discharge_limit"] = "5"  # the app sets its own limit
    looks = int(MIN_DIVERGENCE_S / TICK.total_seconds()) + 6
    _now, results = run(executor, battery, now, looks, "hold")
    assert events(results, "external")[0]["reason"] == "discharge_limit"
    assert ("discharge_limit", 23.04) not in written(results)
    assert battery.states["discharge_limit"] == "5"
    assert battery.states["remote_ems"] == "off", "the rest is handed back"
    assert executor.state.phase == "blocked" and executor.state.blocked.startswith("changed_outside_spotnav")


def test_an_unavailable_reading_never_counts_as_a_change_by_someone_else() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    battery.states["discharge_limit"] = "unavailable"
    looks = int(MIN_DIVERGENCE_S / TICK.total_seconds()) + 6
    _now, results = run(executor, battery, now, looks, "hold")
    assert events(results, "external") == []
    assert executor.state.phase == "active"


def test_a_limit_spotnav_wrote_that_reads_unknown_is_still_restored_by_writing_its_rating() -> None:
    """Sigenergy 2.0.1 shows a never-set limit as `unknown`. Once SpotNav wrote it, `unknown` is no proof that the
    limit is back as found: the restore writes the rating and waits for it to read back."""
    executor, battery = _sigenergy()
    battery.states["discharge_limit"] = "unknown"
    now = _held(executor, battery)
    assert executor.state.restore_values["discharge_limit"] == 10.0
    assert battery.states["discharge_limit"] == "0"
    battery.states["discharge_limit"] = "unknown"  # a read that looks like the value found
    _now, results = run(executor, battery, now, 8, "self_consume")
    assert ("discharge_limit", 10.0) in written(results)
    assert battery.states["discharge_limit"] == "10"
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot
    # The charge limit was never written: still `unknown` as found, so nothing is written back to it.
    assert all(role != "charge_limit" for role, _value in written(results))


def test_a_write_that_reads_back_unknown_is_never_taken_and_unknown_is_never_written_back() -> None:
    executor, battery = _sigenergy(read_only=True)
    battery.states["discharge_limit"] = "unknown"
    looks = int(READBACK_DEADLINE_S / TICK.total_seconds()) + 4
    _now, results = run(executor, battery, T0, looks, "hold")
    assert written(results)[0] == ("discharge_limit", 0.0)
    assert events(results, "confirmed") == [] and events(results, "external") == []
    assert events(results, "not_taken")[0]["reason"] == "discharge_limit"
    # The hand-back writes the rating for the limit it wrote (never "unknown") and waits for its read-back.
    assert ("discharge_limit", 10.0) in written(results)
    assert all(value != "unknown" for _role, value in written(results))
    assert executor.ledger.counters(T0.date().isoformat())["confirmed"] == 0


def test_a_stored_unknown_is_no_value_to_write_back() -> None:
    state = ExecState.from_dict(
        {"phase": "active", "snapshot": {"discharge_limit": "unknown"},
         "restore_values": {"discharge_limit": "unknown", "charge_limit": 10.0}, "written": ["discharge_limit"]}
    )
    assert state.restore_values == {"charge_limit": 10.0} and state.written == ("discharge_limit",)


# --- exit paths ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason", "blocked"),
    [
        ({"stopping": "stop"}, "stop", False),
        ({"stopping": "unload"}, "unload", False),
        ({"battery_age_s": 600.0}, "stale_data", False),
        ({"measurement_ok": False}, "stale_data", False),
        ({"on_grid": False}, "off_grid", False),
        ({"headroom_a": 0.5}, "fuse_headroom", False),
        ({"headroom_a": None}, "headroom_unknown", False),
        ({"management": Management("self_managed", "planner:predbat")}, "management_changed:self_managed:planner:predbat", True),
        # Another system drives it, whatever its own mode reads (`outside_control.py`).
        (
            {"management": Management("externally_controlled", "settings_changed", {"EMS work mode": "max_self_consumption"})},
            "management_changed:externally_controlled:settings_changed",
            True,
        ),
    ],
)
def test_every_exit_path_hands_the_battery_back(overrides, reason, blocked) -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 8, "hold", **overrides)
    assert events(results, "hand_back")[0]["reason"] == reason
    assert battery.states == {**SIGENERGY_STATES, "charge_limit": "unknown"} | {"discharge_limit": "23.04"}
    assert executor.state.phase == ("blocked" if blocked else "idle")
    assert not executor.state.holds_snapshot


def test_a_battery_another_system_drives_is_never_taken() -> None:
    executor, battery = _sigenergy()
    outside = Management("externally_controlled", "integration:checkwatt", {"EMS work mode": "max_self_consumption"})
    _now, results = run(executor, battery, T0, 6, "hold", management=outside)
    assert executor.state.phase == "idle" and not [op for result in results for op in result.ops]
    assert events(results, "not_started")[0]["reason"] == "externally_controlled:integration:checkwatt"


def test_no_plan_now_or_an_action_the_brand_cannot_do_hands_back() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 6, None)
    assert events(results, "hand_back")[0]["reason"] == "no_plan_now"
    assert executor.state.phase == "idle"


def test_a_site_battery_that_is_no_longer_that_brand_hands_back() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    probe = inputs(battery, now, "hold")
    result = executor.step(replace(probe, facts=replace(probe.facts, brand=None)))
    assert [event["reason"] for event in result.events if event["kind"] == "hand_back"] == ["battery_changed"]


def test_the_app_taking_remote_ems_back_hands_back_at_once_without_fighting() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    # The app sets AI mode: it clears Remote EMS itself (discussion #413).
    battery.states["remote_ems"] = "off"
    battery.states["control_mode"] = "unavailable"
    battery.work_mode_override = "ai_mode"
    _now, results = run(executor, battery, now, 6, "hold")
    assert events(results, "hand_back")[0]["reason"] == "management_changed:self_managed:EMS work mode:ai_mode"
    assert ("remote_ems", True) not in written(results) and ("remote_ems", False) not in written(results)
    assert written(results) == [("discharge_limit", 23.04)], "only its own limit is put back"
    assert executor.state.phase == "blocked"


def test_a_management_reading_that_blinks_is_tolerated_for_a_grace() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    unknown = Management("unknown", "EMS work mode:unavailable", {"EMS work mode": "unavailable"})
    looks = int(MANAGEMENT_GRACE_S / TICK.total_seconds()) - 1
    now, results = run(executor, battery, now, looks, "hold", management=unknown)
    assert events(results, "hand_back") == []
    _now, results = run(executor, battery, now, 3, "hold", management=unknown)
    assert events(results, "hand_back")[0]["reason"] == "management_changed:unknown"


def test_the_day_s_write_cap_hands_back_and_waits_for_the_next_day() -> None:
    executor, battery = _sigenergy()
    executor.state = ExecState(day=T0.date().isoformat(), writes_today=DAILY_WRITE_CAP)
    _now, results = run(executor, battery, T0, 2, "hold")
    assert events(results, "not_started")[0]["reason"] == "daily_write_cap"
    tomorrow = T0 + timedelta(days=1)
    _now, results = run(executor, battery, tomorrow, 6, "hold")
    assert executor.state.phase == "active"


def test_after_a_hand_back_a_new_action_waits_to_rearm() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, _ = run(executor, battery, now, 6, "self_consume")
    assert executor.state.phase == "idle"
    now, results = run(executor, battery, now, 3, "hold")
    assert written(results) == []
    _now, results = run(executor, battery, now + timedelta(seconds=REARM_S), 2, "hold")
    assert written(results)


# --- fuse safety -----------------------------------------------------------------------------------------------------


def test_a_grid_charge_is_clamped_by_the_live_headroom_and_the_battery_charge_counts_against_the_fuse() -> None:
    executor, battery = _sigenergy()
    # 6 A of headroom less the 2 A margin over three phases at 400 V: about 2.77 kW.
    _now, results = run(executor, battery, T0, 6, "grid_charge", 8.0, headroom_a=6.0, grid_import_kw=1.0)
    assert executor.state.rate_kw == pytest.approx(2.771, abs=0.001)
    assert ("charge_limit", 2.771) in written(results)


def test_the_counted_back_charge_is_never_above_the_site_import() -> None:
    executor, battery = _sigenergy()
    probe = inputs(battery, T0, "grid_charge", 8.0, headroom_a=2.0, grid_import_kw=0.5, power_kw=5.0)
    # The battery charges 5 kW but the site imports 0.5 kW: the sun covers the rest, so only 0.5 kW is its grid's.
    assert grid_charge_cap_kw(probe, 5.0) == pytest.approx(0.5)
    probe = inputs(battery, T0, "grid_charge", 8.0, headroom_a=2.0, grid_import_kw=None, power_kw=5.0)
    assert grid_charge_cap_kw(probe, 5.0) == pytest.approx(0.0)


def test_the_rate_goes_down_at_once_and_up_only_after_the_dwell() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0, headroom_a=20.0)
    assert executor.state.rate_kw == 4.0
    # The house switches on a kettle: headroom shrinks; the battery's own 4 kW is in the import and counts back.
    now, results = run(executor, battery, now, 3, "grid_charge", 4.0, headroom_a=2.5, grid_import_kw=6.0)
    assert executor.state.rate_kw == 4.0 and written(results) == []
    now, results = run(executor, battery, now, 1, "grid_charge", 6.0, headroom_a=1.5, grid_import_kw=6.0)
    assert executor.state.rate_kw == pytest.approx(4.0 - 0.346, abs=0.001)
    assert executor.state.rate_kw < 4.0, "lowered at once"
    low = executor.state.rate_kw
    now, results = run(executor, battery, now, 2, "grid_charge", 6.0, headroom_a=20.0, grid_import_kw=6.0)
    assert executor.state.rate_kw == low, "not raised before the dwell"
    _now, results = run(
        executor, battery, now + timedelta(seconds=RATE_UP_DWELL_S), 2, "grid_charge", 6.0, headroom_a=20.0,
        grid_import_kw=6.0,
    )
    assert executor.state.rate_kw == 6.0


def test_with_car_first_the_battery_yields_to_a_held_car_and_with_battery_first_it_does_not() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    now, _ = run(executor, battery, now, 1, "grid_charge", 4.0, car_held=True, car_first=True, grid_import_kw=5.0)
    assert executor.state.rate_kw == pytest.approx(3.0)
    _now, _ = run(executor, battery, now, 1, "grid_charge", 4.0, car_held=True, car_first=True, grid_import_kw=5.0)
    assert executor.state.rate_kw == pytest.approx(2.0), "it keeps yielding while the car is held"
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    _now, _ = run(executor, battery, now, 2, "grid_charge", 4.0, car_held=True, car_first=False, grid_import_kw=5.0)
    assert executor.state.rate_kw == 4.0


def test_a_grid_charge_that_the_headroom_cannot_carry_is_not_started_or_is_handed_back() -> None:
    executor, battery = _sigenergy()
    _now, results = run(executor, battery, T0, 2, "grid_charge", 4.0, headroom_a=2.5)
    assert events(results, "not_started")[0]["reason"] in ("headroom_too_small", "fuse_headroom")
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    _now, results = run(executor, battery, now, 6, "grid_charge", 4.0, headroom_a=1.5, grid_import_kw=0.0)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"


# --- the peak cover under a hold -------------------------------------------------------------------------------------


def _looks(seconds: float) -> int:
    return int(seconds / TICK.total_seconds()) + 1


def test_the_peak_cover_mark_lies_between_the_hand_back_and_the_start_margin() -> None:
    assert RESTORE_HEADROOM_A < PEAK_COVER_A < HEADROOM_MARGIN_A


def test_a_peak_under_a_hold_lifts_the_discharge_limit_first_in_one_write_and_keeps_remote_ems() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, results = run(executor, battery, now, 3, "hold", headroom_a=1.2)
    assert written(results) == [("discharge_limit", 23.04)]
    assert events(results, "peak_cover")[0]["headroom_a"] == 1.2
    assert executor.state.phase == "active" and executor.state.covered_at is not None
    assert battery.states["remote_ems"] == "on" and battery.states["discharge_limit"] == "23.04"
    # The battery covers the peak: the headroom recovers and the battery discharges; that is no failed hold.
    _now, results = run(executor, battery, now, _looks(GRACE_S + 5 * 60), "hold", headroom_a=3.0, power_kw=-3.0)
    assert written(results) == [] and events(results, "hand_back") == [] and events(results, "monitor") == []
    assert executor.state.phase == "active"


def test_a_peak_the_lift_does_not_cover_is_handed_back_in_full_after_the_recover_time() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, results = run(executor, battery, now, _looks(PEAK_RECOVER_S) - 2, "hold", headroom_a=1.2)
    assert written(results) == [("discharge_limit", 23.04)] and events(results, "hand_back") == []
    _now, results = run(executor, battery, now, 6, "hold", headroom_a=1.2)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert written(results) == [("remote_ems", False)]
    assert executor.state.phase == "idle" and executor.state.blocked is None


def test_a_covered_hold_that_falls_low_again_is_handed_back_after_the_recover_time() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, _ = run(executor, battery, now, 3, "hold", headroom_a=1.2)
    now, _ = run(executor, battery, now, 6, "hold", headroom_a=3.0, power_kw=-3.0)
    now, results = run(executor, battery, now, _looks(PEAK_RECOVER_S) - 2, "hold", headroom_a=1.3, power_kw=-10.0)
    assert events(results, "hand_back") == []
    _now, results = run(executor, battery, now, 4, "hold", headroom_a=1.3, power_kw=-10.0)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"


def test_a_peak_under_the_hand_back_mark_hands_back_at_once_with_the_discharge_limit_first() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 6, "hold", headroom_a=RESTORE_HEADROOM_A - 0.5)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert results[0].ops[0].role == "discharge_limit", "the first write after the peak lets the battery cover it"
    assert written(results) == [("discharge_limit", 23.04), ("remote_ems", False)]
    # Lifted first, then under the mark: the hand-back goes on from there without writing the limit again.
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, results = run(executor, battery, now, 1, "hold", headroom_a=1.2)
    assert written(results) == [("discharge_limit", 23.04)]
    _now, results = run(executor, battery, now, 6, "hold", headroom_a=0.2)
    assert written(results) == [("remote_ems", False)]
    assert executor.state.phase == "idle"


def test_a_grid_charge_is_handed_back_for_the_fuse_with_the_mode_that_stops_it_first() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    _now, results = run(executor, battery, now, 6, "grid_charge", 4.0, headroom_a=0.5, grid_import_kw=0.0)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert written(results)[0] == ("control_mode", "Maximum Self Consumption")


def test_a_covered_hold_holds_again_only_after_the_dwell_and_with_the_start_margin() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, _ = run(executor, battery, now, 3, "hold", headroom_a=1.2)
    # The battery covers 3 kW (4.3 A per phase at 400 V): 5 A of headroom is not enough to hold it again.
    now, results = run(executor, battery, now, _looks(PEAK_REHOLD_S), "hold", headroom_a=5.0, power_kw=-3.0)
    assert written(results) == [] and events(results, "peak_rehold") == []
    _now, results = run(executor, battery, now, 3, "hold", headroom_a=8.0, power_kw=-3.0)
    assert events(results, "peak_rehold")[0]["headroom_a"] == 8.0
    assert written(results) == [("discharge_limit", 0.0)]
    assert executor.state.covered_at is None and executor.monitor.watch is not None
    # Before the dwell it does not hold again, however much headroom.
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, _ = run(executor, battery, now, 3, "hold", headroom_a=1.2)
    _now, results = run(executor, battery, now, _looks(PEAK_REHOLD_S) - 5, "hold", headroom_a=20.0)
    assert events(results, "peak_rehold") == []


def test_a_new_action_ends_the_cover_and_the_cover_survives_a_restart_in_the_stored_state() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    now, _ = run(executor, battery, now, 3, "hold", headroom_a=1.2)
    stored = ExecState.from_dict(executor.state.as_dict())
    assert stored.covered_at == executor.state.covered_at and stored.covered_at is not None
    _now, results = run(executor, battery, now, 6, "discharge_house")
    assert executor.state.covered_at is None and executor.state.action == "discharge_house"


@pytest.mark.parametrize(
    ("control", "states", "first"),
    [
        (SOLAREDGE_CONTROL, SOLAREDGE_STATES, [("command_mode", "Maximize Self Consumption")]),
        (HUAWEI_CONTROL, HUAWEI_STATES, [("discharge_power", 5000.0)]),
        (SOLAX_CONTROL, SOLAX_STATES, [("power_control", "Disabled"), ("trigger", True)]),
    ],
)
def test_every_brand_lifts_its_hold_for_a_peak(control, states, first) -> None:
    executor = Executor(control)
    battery = FakeBattery(control, dict(states))
    now, _ = run(executor, battery, T0, 8, "hold")
    assert executor.state.phase == "active"
    _now, results = run(executor, battery, now, 3, "hold", headroom_a=1.2)
    assert written(results) == first
    assert executor.state.phase == "active"


# --- the monitor ------------------------------------------------------------------------------------------------------


def test_a_hold_the_battery_ignores_is_handed_back_and_blocked_by_the_monitor() -> None:
    executor, battery = _sigenergy(obeys=False)
    looks = int((GRACE_S + 4 * 60) / TICK.total_seconds())
    _now, results = run(executor, battery, T0, looks, "hold")
    assert events(results, "monitor")[0]["reason"] == "no_effect"
    assert executor.state.phase == "blocked" and executor.state.blocked == "no_effect:hold"
    assert battery.states["remote_ems"] == "off"


# --- restart and restore retries --------------------------------------------------------------------------------------


def test_a_restart_restores_the_stored_snapshot_before_anything_else() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    stored = executor.as_dict()
    again = Executor(
        SIGENERGY_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        ExecState.from_dict(stored["state"]),
    )
    assert again.restart(now)[0]["reason"] == "restart"
    _now, results = run(again, battery, now, 8, "hold")
    # The hold's mode already is Maximum Self Consumption: the switch goes off, then the limit back.
    assert written(results) == [("discharge_limit", 23.04), ("remote_ems", False)]
    assert battery.states["remote_ems"] == "off"


def test_a_restore_write_not_taken_is_retried_with_backoff_and_the_others_still_handed_back() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    battery.raises = {"discharge_limit"}
    total = 0
    looks = int((READBACK_DEADLINE_S + sum((30, 60, 120, 300, 600))) / TICK.total_seconds()) + 20
    _now, results = run(executor, battery, now, looks, "self_consume")
    total = sum(1 for role, _ in written(results) if role == "discharge_limit")
    assert total == RESTORE_ATTEMPTS + 1
    assert battery.states["remote_ems"] == "off", "the other roles were handed back meanwhile"
    assert executor.state.phase == "blocked" and executor.state.blocked == "restore_failed:discharge_limit"
    assert executor.state.restore_values == {"discharge_limit": 23.04}, "its snapshot is kept for the next start"
    executor.reset()
    assert executor.state.phase == "blocked", "a kept snapshot cannot be reset away"


# --- the other brands ---------------------------------------------------------------------------------------------------


def test_solaredge_runs_in_remote_control_only_with_its_timeout_as_the_watchdog_renewed() -> None:
    executor = Executor(SOLAREDGE_CONTROL)
    battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES))
    now, results = run(executor, battery, T0, 6, "grid_charge", 3.0)
    assert written(results) == [
        ("command_timeout", 1800.0), ("charge_limit", 3000.0), ("command_mode", "Charge from Solar Power and Grid")
    ]
    now, results = run(executor, battery, now + timedelta(seconds=WATCHDOG_RENEW_S), 4, "grid_charge", 3.0)
    assert [role for role, _ in written(results)] == ["command_timeout", "command_mode"]
    _now, results = run(executor, battery, now, 8, "self_consume")
    assert battery.states == SOLAREDGE_STATES
    # Not in Remote Control, or no AC charging allowed: nothing is written.
    for states, reason in (
        ({"control_mode": "Maximize Self Consumption"}, "self_managed"),
        ({"ac_charge_policy": "Disabled"}, "ac_charge_disabled"),
    ):
        executor = Executor(SOLAREDGE_CONTROL)
        battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES) | states)
        _now, results = run(executor, battery, T0, 2, "grid_charge", 3.0)
        assert written(results) == [] and events(results, "not_started")[0]["reason"].startswith(reason)


def test_huawei_charges_through_its_forcible_charge_service_and_stops_it_on_the_way_back() -> None:
    executor = Executor(HUAWEI_CONTROL)
    battery = FakeBattery(HUAWEI_CONTROL, dict(HUAWEI_STATES))
    now, results = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert written(results) == [("forcible", "charge:3000")]
    assert battery.writes[0].device_id == "DEVICE"
    now, results = run(executor, battery, now, 4, "hold")
    assert written(results) == [("discharge_power", 0.0), ("forcible", "stop")]
    _now, results = run(executor, battery, now, 6, "self_consume")
    assert battery.states == HUAWEI_STATES


def test_solax_sends_its_remote_control_on_the_trigger_and_sends_disabled_on_the_way_back() -> None:
    executor = Executor(SOLAX_CONTROL)
    battery = FakeBattery(SOLAX_CONTROL, dict(SOLAX_STATES))
    now, results = run(executor, battery, T0, 6, "grid_charge", 2.0)
    assert written(results) == [
        ("autorepeat", 1800.0), ("active_power", 2000.0), ("power_control", "Enabled Battery Control"),
        ("trigger", True),
    ]
    assert battery.sent["power_control"] == "Enabled Battery Control"
    _now, results = run(executor, battery, now, 8, "self_consume")
    assert written(results)[:2] == [("power_control", "Disabled"), ("trigger", True)]
    assert battery.sent["power_control"] == "Disabled"
    assert battery.states == SOLAX_STATES


# --- the dry run the shadow parts share ------------------------------------------------------------------------------


def test_the_preview_is_the_executors_own_writes_and_hand_back_without_writing() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES) | {"discharge_limit": "unknown"})

    seen = preview(SIGENERGY_CONTROL, inputs(battery, T0, "hold"))

    assert seen.blocker is None
    assert [(op.role, op.value) for op in seen.writes] == [
        ("discharge_limit", 0.0), ("remote_ems", True), ("control_mode", "Maximum Self Consumption")
    ]
    # Handing back: the limit it wrote first, as the rating (it read unknown: never set), so the battery covers a
    # peak at once, then the mode and the switch.
    assert [(op.role, op.value) for op in seen.restores] == [
        ("discharge_limit", 10.0), ("control_mode", "Maximum Self Consumption"), ("remote_ems", False)
    ]
    assert all(op.restore for op in seen.restores)
    assert seen.snapshot == {"charge_limit": "unknown", "discharge_limit": "unknown"}
    assert battery.writes == [] and battery.states["discharge_limit"] == "unknown"


def test_the_preview_refuses_as_the_executor_does() -> None:
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), work_mode_override="ai_mode")
    assert preview(SIGENERGY_CONTROL, inputs(battery, T0, "hold")).blocker.startswith("self_managed")
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), missing={"remote_ems"})
    assert preview(SIGENERGY_CONTROL, inputs(battery, T0, "hold")).blocker == "control_not_found:remote_ems"
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    assert preview(SIGENERGY_CONTROL, inputs(battery, T0, "hold"), writes_today=DAILY_WRITE_CAP).blocker == "daily_write_cap"
    assert preview(SIGENERGY_CONTROL, inputs(battery, T0, "hold", headroom_a=None)).blocker == "headroom_unknown"


# --- the expected value is checked just before each write ---------------------------------------------------------


def test_a_rate_change_never_writes_over_a_limit_someone_else_just_changed() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0, headroom_a=20.0)
    assert executor.state.phase == "active"
    battery.states["charge_limit"] = "9"  # the app sets its own limit, seconds ago
    _now, results = run(executor, battery, now, 3, "grid_charge", 4.0, headroom_a=1.5, grid_import_kw=6.0)
    assert [event["reason"] for event in events(results, "external")] == ["charge_limit"]
    assert events(results, "external")[0]["expected"] == "4"
    assert all(role != "charge_limit" for role, _value in written(results)), "no write over the foreign value"
    assert battery.states["charge_limit"] == "9", "and the restore leaves it alone"
    assert executor.state.phase == "blocked" and executor.state.blocked.startswith("changed_outside_spotnav")


def test_a_role_never_written_that_no_longer_reads_as_found_is_not_restored_over() -> None:
    executor, battery = _sigenergy()
    battery.states["charge_limit"] = "8"
    now = _held(executor, battery)
    assert executor.state.snapshot["charge_limit"] == "8"
    battery.states["charge_limit"] = "9"  # the app sets the charge limit while we hold
    _now, results = run(executor, battery, now, 8, "self_consume")
    assert [event["reason"] for event in events(results, "external")] == ["charge_limit"]
    assert all(role != "charge_limit" for role, _value in written(results))
    assert battery.states["charge_limit"] == "9"
    assert ("discharge_limit", 23.04) in written(results), "what SpotNav wrote is still handed back"


def test_an_unknown_reading_before_a_write_is_no_evidence_and_the_write_goes_ahead() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0, headroom_a=20.0)
    battery.states["charge_limit"] = "unknown"
    now, results = run(executor, battery, now, 1, "grid_charge", 6.0, headroom_a=1.5, grid_import_kw=6.0)
    assert events(results, "external") == []
    assert any(role == "charge_limit" for role, _value in written(results))


def test_the_sigenergy_mode_texts_are_the_options_of_typqxq_s_remote_ems_control_mode_select() -> None:
    """TypQxQ/Sigenergy-Local-Modbus, custom_components/sigen/select.py lines 109-112 (options) and 120-123 (map),
    register 40031 values in modbusregisterdefinitions.py lines 164-167: 2 Maximum Self Consumption,
    3 Command Charging (Grid First), 5 Command Discharging (PV First)."""
    def mode(action: str) -> object:
        return SIGENERGY_CONTROL.wanted(action, 4.0, 0)["control_mode"]

    assert mode("grid_charge") == "Command Charging (Grid First)"  # register value 3
    assert mode("export_discharge") == "Command Discharging (PV First)"  # register value 5
    assert mode("hold") == "Maximum Self Consumption"  # register value 2
