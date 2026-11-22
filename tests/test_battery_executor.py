"""The battery's write path against fake entities: every brand's commands, the order and read-back of each write,
the fuse clamp, and the hand-back on every exit path (stop, stale data, fuse, management change, a write not taken,
a change by someone else, no effect, a restart)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from hypothesis import assume, example, given, settings, strategies as st
import pytest

from custom_components.spotnav.battery import gate
from custom_components.spotnav.battery.commands import (
    FERROAMP_CONTROL,
    HUAWEI_CONTROL,
    NORMAL,
    SIGENERGY_CONTROL,
    SOLAREDGE_CONTROL,
    SOLAX_CONTROL,
    WATCHDOG_RENEW_S,
    WATCHDOG_S,
    control_for,
    huawei_forcible,
    service_call,
    table,
)
from custom_components.spotnav.battery.executor import (
    BLOCKED_RESTORE_S,
    CALL_TIMEOUT_BLOCK_S,
    DAILY_WRITE_CAP,
    HEADROOM_MARGIN_A,
    MANAGEMENT_BLOCK_S,
    MANAGEMENT_GRACE_S,
    PEAK_COVER_A,
    PEAK_RECOVER_S,
    PEAK_REHOLD_S,
    RATE_UP_DWELL_S,
    REARM_S,
    RESTORE_ATTEMPTS,
    RESTORE_HEADROOM_A,
    RESTORE_RETRY_S,
    ExecState,
    Executor,
    grid_charge_cap_kw,
    hold_headroom_ok,
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
    assert {control.brand for control in (SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL, FERROAMP_CONTROL)} == {
        "sigenergy", "solaredge", "huawei", "solax", "ferroamp"
    }
    for brand in ("victron_modbus", "enphase", None):
        assert control_for(brand) is None
    rows = table()
    for control in (SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL, FERROAMP_CONTROL):
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
    battery.idle_kw = 0.0  # nothing covered at the start: the cover is counted out of a start from idle
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


def test_a_plant_in_ai_mode_is_taken_over_only_after_the_person_acknowledged_it_and_handed_back_to_msc() -> None:
    executor, battery = _sigenergy(work_mode_override="ai_mode")
    _now, results = run(executor, battery, T0, 3, "hold")
    assert written(results) == [], "without the acknowledgement AI mode is only watched"
    executor, battery = _sigenergy(work_mode_override="ai_mode")
    now, results = run(executor, battery, T0, 6, "hold", takeover=True)
    assert executor.state.phase == "active"
    assert written(results) == [
        ("discharge_limit", 0.0), ("remote_ems", True), ("control_mode", "Maximum Self Consumption")
    ]
    # Turning Remote EMS off lands the plant in Maximum Self Consumption, never back in AI mode.
    battery.work_mode_override = None
    _now, results = run(executor, battery, now, 6, "self_consume", takeover=True)
    assert executor.state.phase == "idle"
    assert battery.states["remote_ems"] == "off" and battery.management().settings["EMS work mode"] == "max_self_consumption"


def test_the_acknowledged_takeover_is_no_way_past_another_planner_or_another_s_remote_ems() -> None:
    executor, battery = _sigenergy(work_mode_override="ai_mode")
    planner = Management("externally_controlled", "planner:predbat", {"EMS work mode": "ai_mode"})
    _now, results = run(executor, battery, T0, 3, "hold", takeover=True, management=planner)
    assert written(results) == []
    executor, battery = _sigenergy()
    battery.states["remote_ems"] = "on"
    _now, results = run(executor, battery, T0, 3, "hold", takeover=True)
    assert written(results) == []


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


def test_a_call_given_up_on_hands_back_and_blocks_only_for_a_while() -> None:
    executor, battery = _sigenergy(times_out={"remote_ems"})
    now, results = run(executor, battery, T0, 6, "hold")
    assert events(results, "not_taken")[0]["why"] == "call_timeout"
    assert executor.state.phase == "blocked" and executor.state.blocked.startswith("call_timeout:remote_ems")
    assert battery.states["discharge_limit"] == "23.04", "handed back as for any write not taken"
    # A slow integration is no finding about the plant: the block clears by itself, then control runs again.
    battery.times_out = set()
    now, results = run(executor, battery, now, int(CALL_TIMEOUT_BLOCK_S / TICK.total_seconds()) // 2, "hold")
    assert written(results) == [] and executor.state.phase == "blocked"
    now, results = run(executor, battery, now, int(CALL_TIMEOUT_BLOCK_S / TICK.total_seconds()) // 2 + 12, "hold")
    assert events(results, "unblocked") and executor.state.phase == "active"


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
        # A stale reading alone is ridden out a while (`test_battery_silence.py`); with the fuse near, at once.
        ({"battery_age_s": 600.0, "headroom_a": 1.2}, "stale_data", False),
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
    now, results = run(executor, battery, now, 3, "hold", management=unknown)
    # After the grace an unknown management is handed back as stale data is: no block, the next start re-arms.
    assert events(results, "hand_back")[0]["reason"] == "management_unknown"
    now, _ = run(executor, battery, now, 6, "hold", management=unknown)
    assert executor.state.phase == "idle" and executor.state.blocked is None
    _now, results = run(executor, battery, now + timedelta(seconds=REARM_S), 6, "hold")
    assert executor.state.phase == "active"


def test_a_real_management_change_blocks_and_the_block_clears_by_itself_after_a_long_rearm() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    ai = Management("self_managed", "EMS work mode:ai_mode", {"EMS work mode": "ai_mode"})
    now, _ = run(executor, battery, now, 8, "hold", management=ai)
    assert executor.state.phase == "blocked" and executor.state.blocked.startswith("management_changed")
    # Hours later the plant is back in Maximum Self Consumption; the block still holds until the long re-arm.
    now, results = run(executor, battery, now + timedelta(hours=1), 3, "hold")
    assert executor.state.phase == "blocked" and written(results) == []
    # It survives a restart, and clears by itself after MANAGEMENT_BLOCK_S without anything calling reset.
    executor = Executor(SIGENERGY_CONTROL, state=ExecState.from_dict(executor.state.as_dict()))
    assert executor.restart(now) == [] and executor.state.phase == "blocked"
    _now, results = run(executor, battery, now + timedelta(seconds=MANAGEMENT_BLOCK_S), 6, "hold")
    assert executor.state.phase == "active" and written(results)


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
    battery.idle_kw = 0.0  # nothing covered at the start: the cover is counted out of a start from idle
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
    battery.idle_kw = 0.0  # nothing covered at the start: the cover is counted out of a start from idle
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
    battery.idle_kw = 0.0  # nothing covered at the start: the cover is counted out of a start from idle
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    now, _ = run(executor, battery, now, 1, "grid_charge", 4.0, car_held=True, car_first=True, grid_import_kw=5.0)
    assert executor.state.rate_kw == pytest.approx(3.0)
    # One step per dwell: a few looks later it has not yielded again (the car's regulator needs time to follow).
    now, _ = run(executor, battery, now, 4, "grid_charge", 4.0, car_held=True, car_first=True, grid_import_kw=5.0)
    assert executor.state.rate_kw == pytest.approx(3.0)
    _now, _ = run(executor, battery, now + timedelta(seconds=RATE_UP_DWELL_S), 1, "grid_charge", 4.0, car_held=True,
                  car_first=True, grid_import_kw=5.0)
    assert executor.state.rate_kw == pytest.approx(2.0), "it keeps yielding while the car is held"
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    _now, _ = run(executor, battery, now, 2, "grid_charge", 4.0, car_held=True, car_first=False, grid_import_kw=5.0)
    assert executor.state.rate_kw == 4.0


def test_a_yield_step_is_counted_from_the_battery_s_measured_charge_not_its_last_command() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    # The battery has not followed the last command down yet: it still charges 4 kW, so one yield is 3 kW, ...
    held = {"car_held": True, "car_first": True, "grid_import_kw": 5.0, "power_kw": 4.0}
    now, _ = run(executor, battery, now, 1, "grid_charge", 4.0, **held)
    assert executor.state.rate_kw == pytest.approx(3.0)
    # ... and a dwell later, with the battery still at 4 kW (slow to follow), the next yield is 3 kW again, not 2.
    _now, _ = run(executor, battery, now + timedelta(seconds=RATE_UP_DWELL_S), 1, "grid_charge", 4.0, **held)
    assert executor.state.rate_kw == pytest.approx(3.0)
    assert grid_charge_cap_kw(inputs(battery, now, "grid_charge", 4.0, power_kw=2.0, grid_import_kw=5.0,
                                     car_held=True, car_first=True), 4.0) == pytest.approx(1.0)


def test_the_live_clamp_counts_the_least_headroom_on_the_phases_the_plant_spreads_the_charge_over() -> None:
    """A charge not known to be balanced may all land on the tightest phase: a third of the three-phase figure."""
    _executor, battery = _sigenergy()
    look = inputs(battery, T0, "grid_charge", 10.0, headroom_a=10.0 + HEADROOM_MARGIN_A, grid_import_kw=0.0)
    balanced = grid_charge_cap_kw(look, 10.0)
    assert balanced == pytest.approx(10.0 * 3 ** 0.5 * 400.0 / 1000), "the default: three phases"
    assert grid_charge_cap_kw(replace(look, charge_phases=3), 10.0) == pytest.approx(balanced)
    one = grid_charge_cap_kw(replace(look, charge_phases=1), 10.0)
    assert one == pytest.approx(balanced / 3)
    assert one * 1000 / (400.0 / 3 ** 0.5) == pytest.approx(10.0), "the whole room of one phase, no more"


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


def test_a_fuse_hand_back_writes_every_role_at_once_and_a_lift_not_read_back_never_delays_remote_ems_off() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    battery.drops = {"discharge_limit"}
    now, results = run(executor, battery, now, 1, "hold", headroom_a=0.5)
    assert [(op.role, op.value) for op in results[0].ops] == [("discharge_limit", 23.04), ("remote_ems", False)]
    assert battery.states["remote_ems"] == "off"
    # Each is still read back: Remote EMS off is confirmed, the dropped limit is retried as any restore.
    now, results = run(executor, battery, now, _looks(READBACK_DEADLINE_S) + 1, "hold", headroom_a=0.5)
    assert [event["reason"] for event in events(results, "confirmed")] == ["remote_ems"]
    assert events(results, "not_taken")[0]["reason"] == "discharge_limit"
    assert "discharge_limit" in executor.state.retries and executor.state.phase == "restoring"
    battery.drops.clear()
    _now, results = run(executor, battery, now, _looks(60.0), "hold", headroom_a=0.5)
    assert written(results) == [("discharge_limit", 23.04)]
    assert executor.state.phase == "idle" and battery.states["discharge_limit"] == "23.04"


def test_a_peak_cover_lift_still_awaiting_its_read_back_does_not_hold_the_fuse_hand_back() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    battery.drops = {"discharge_limit"}
    now, results = run(executor, battery, now, 1, "hold", headroom_a=1.2)
    assert written(results) == [("discharge_limit", 23.04)] and executor.state.pending == "discharge_limit"
    _now, results = run(executor, battery, now, 1, "hold", headroom_a=0.2)
    assert written(results) == [("remote_ems", False)], "Remote EMS goes off in the very next look"
    assert set(executor.state.in_flight) == {"discharge_limit", "remote_ems"}


def test_a_hand_back_that_is_not_for_the_fuse_keeps_one_write_at_a_time() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    _now, results = run(executor, battery, now, 6, "hold", stopping="stop")
    assert written(results) == [("discharge_limit", 23.04), ("remote_ems", False)]
    assert all(len(result.ops) <= 1 for result in results)
    assert executor.state.in_flight == ()


def test_a_hold_starts_only_when_the_headroom_carries_the_whole_discharge_on_one_phase() -> None:
    executor, battery = _sigenergy()
    # 3 kW is 4.3 A per phase spread over three, but 13 A on one phase.
    assert not hold_headroom_ok(inputs(battery, T0, "hold", headroom_a=8.0, power_kw=-3.0))
    assert hold_headroom_ok(inputs(battery, T0, "hold", headroom_a=15.1, power_kw=-3.0))
    _now, results = run(executor, battery, T0, 3, "hold", headroom_a=8.0, power_kw=-3.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "headroom_too_small"


def test_fuse_s1_a_sale_starts_only_when_the_cover_it_may_take_off_one_phase_fits() -> None:
    """The battery covers 12 A on L1 (2.8 kW) with 4 A left: a 3 kW sale spread evenly gives 4.3 A back a phase,
    so L1 would lose 7.7 A. Not started; with the room for it, or nothing covered, it starts."""
    executor, battery = _sigenergy()
    _now, results = run(executor, battery, T0, 3, "export_discharge", 3.0, headroom_a=4.0, power_kw=-2.76)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "headroom_too_small"
    executor, battery = _sigenergy()
    _now, results = run(executor, battery, T0, 3, "export_discharge", 3.0, headroom_a=12.0, power_kw=-2.76)
    assert events(results, "enter")
    executor, battery = _sigenergy()
    _now, results = run(executor, battery, T0, 3, "export_discharge", 3.0, headroom_a=4.0, power_kw=0.0)
    assert events(results, "enter")


def test_fuse_s1_a_change_into_a_sale_passes_its_start_rule() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "discharge_house", 3.0, house_deficit_kw=3.0)
    assert executor.state.action == "discharge_house"
    _now, results = run(executor, battery, now, 6, "export_discharge", 3.0, headroom_a=4.0, power_kw=-2.76)
    assert events(results, "change") == []
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"


def test_fuse_s1_a_sale_is_handed_back_when_a_phase_nears_the_fuse() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "export_discharge", 3.0)
    assert executor.state.phase == "active"
    _now, results = run(executor, battery, now, 6, "export_discharge", 3.0, headroom_a=RESTORE_HEADROOM_A - 0.5)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert executor.state.phase in ("idle", "restoring")


def test_a_sale_at_the_export_cap_is_not_handed_back_for_the_export_side() -> None:
    """The export cap lets a sale and the sun fill each phase to the fuse less 2 A, where the headroom reads about
    `RESTORE_HEADROOM_A`: the hand-back judges the importing phases only (the export side is the cap's), so a sale
    at its cap runs on, not handed back and started again every `REARM_S`."""
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "export_discharge", 3.0)
    assert executor.state.phase == "active" and executor.state.action == "export_discharge"
    exporting = {"headroom_a": RESTORE_HEADROOM_A - 0.3, "phase_headroom_a": (0.7, 0.8, 0.9),
                 "grid_phase_kw": (-4.0, -4.0, -4.0)}
    _now, results = run(executor, battery, now, 15, "export_discharge", 3.0, **exporting)
    assert events(results, "hand_back") == []
    assert executor.state.phase == "active" and executor.state.action == "export_discharge"


def test_a_sale_is_handed_back_when_an_importing_phase_nears_the_fuse() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "export_discharge", 3.0)
    importing = {"headroom_a": RESTORE_HEADROOM_A - 0.5, "phase_headroom_a": (RESTORE_HEADROOM_A - 0.5, 9.0, 9.0),
                 "grid_phase_kw": (4.4, -3.0, -3.0)}
    _now, results = run(executor, battery, now, 3, "export_discharge", 3.0, **importing)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"


def test_fuse_s2_a_change_into_a_hold_passes_the_holds_start_rule() -> None:
    """The battery covers 3 kW (13 A on one phase) with 8 A left: from idle a hold is refused; as a change from
    covering the house it is handed back to plain self-consumption, never written."""
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "discharge_house", 3.0, house_deficit_kw=3.0)
    assert executor.state.phase == "active" and executor.state.action == "discharge_house"
    _now, results = run(executor, battery, now, 6, "hold", headroom_a=8.0, power_kw=-3.0)
    assert events(results, "change") == []
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"
    assert ("discharge_limit", 0.0) not in written(results)
    # With the room for it, the change is made.
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "discharge_house", 3.0, house_deficit_kw=3.0)
    _now, results = run(executor, battery, now, 3, "hold", headroom_a=16.0, power_kw=-3.0)
    assert events(results, "change")[0]["reason"] == "hold"


def test_a_change_from_a_grid_charge_into_a_hold_is_not_held_to_the_start_rule() -> None:
    """Ending a charge only frees room: a fuse-limited night charge (the clamp keeps the headroom near 2 A) that
    turns into a hold stays a hold, also when the headroom reads below the start margin at the slot change."""
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "grid_charge", 4.0)
    assert executor.state.phase == "active" and executor.state.action == "grid_charge"
    _now, results = run(executor, battery, now, 6, "hold", headroom_a=1.7, power_kw=3.0)
    assert events(results, "hand_back") == []
    assert events(results, "change")[0]["reason"] == "hold"
    assert executor.state.action == "hold"


def test_a_change_from_a_sale_into_a_hold_is_held_to_the_start_rule() -> None:
    """A sale covers the house and the cars behind the meter before it exports: ending it into a hold can put that
    load back on a phase, so the hold's start rule judges it, and without room the battery is handed back. A plant
    that spreads its sale evenly over three phases (SigenStor) gives each phase a third of it."""
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "export_discharge", 5.0)
    assert executor.state.action == "export_discharge"
    # 5 kW over three phases: 7.2 A a phase, which 12 A of headroom carries with the margin.
    _now, results = run(executor, battery, now, 6, "hold", headroom_a=12.0, power_kw=-5.0)
    assert events(results, "hand_back") == []
    assert events(results, "change")[0]["reason"] == "hold"
    # 9 A does not (1.8 A left).
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "export_discharge", 5.0)
    _now, results = run(executor, battery, now, 6, "hold", headroom_a=9.0, power_kw=-5.0)
    assert events(results, "change") == []
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"


def test_a_sale_into_a_hold_counts_all_of_it_on_one_phase_unless_the_plant_spreads_it_over_three() -> None:
    """A plant not known to spread its power over three phases (`charge_phases` 1) may have all 5 kW (22 A) on one
    phase, as before; a plant whose own power per phase shows more on one phase than a third is judged on that."""
    for overrides, headroom_a, held in (
        ({"charge_phases": 1}, 12.0, False),
        ({"charge_phases": 1}, 30.0, True),
        ({"plant_phase_kw": (4.0, 0.5, 0.5)}, 12.0, False),
        ({"plant_phase_kw": (4.0, 0.5, 0.5)}, 20.0, True),
        ({"plant_phase_kw": (1.7, 1.7, 1.6)}, 12.0, True),
    ):
        executor, battery = _sigenergy()
        now, _ = run(executor, battery, T0, 8, "export_discharge", 5.0, **overrides)
        assert executor.state.action == "export_discharge"
        _now, results = run(executor, battery, now, 6, "hold", headroom_a=headroom_a, power_kw=-5.0, **overrides)
        assert bool(events(results, "change")) is held, (overrides, headroom_a)
        assert bool(events(results, "hand_back")) is not held, (overrides, headroom_a)


def test_fuse_a_hold_after_a_three_phase_sale_never_puts_a_phase_over_the_fuse_less_the_margin() -> None:
    """Every house (amps per phase, import or export) and sale the start rule lets a sale end into a hold leaves each
    phase at most the fuse less `HEADROOM_MARGIN_A` once the sale's output is gone: with the plant spreading it
    evenly, and with the plant's own power per phase read and spread unevenly."""
    volts = 400.0
    phase_v = volts / 3**0.5
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    fuse = 20.0
    checked = 0
    for sale_kw in (0.5, 2.0, 5.0, 8.0, 10.0, 15.0):
        for split in ((1 / 3, 1 / 3, 1 / 3), (0.6, 0.2, 0.2), (1.0, 0.0, 0.0), (0.0, 0.5, 0.5)):
            output_a = [sale_kw * part * 1000.0 / phase_v for part in split]
            for house in ((0.0, 0.0, 0.0), (18.0, 1.0, 1.0), (10.0, 10.0, 10.0), (17.9, 4.0, 0.0), (25.0, 0.0, 0.0),
                          (5.0, 16.0, 2.0), (12.0, 12.0, 17.0)):
                current = [load - out for load, out in zip(house, output_a, strict=True)]
                headroom = min(fuse - abs(amps) for amps in current)
                balanced = split == (1 / 3, 1 / 3, 1 / 3)
                plant = None if balanced else tuple(sale_kw * part for part in split)
                look = inputs(battery, T0, "hold", headroom_a=headroom, power_kw=-sale_kw, plant_phase_kw=plant,
                              volts_between_phases=volts)
                if hold_headroom_ok(look, from_sale=True):
                    checked += 1
                    assert max(house) <= fuse - HEADROOM_MARGIN_A + 1e-9, (sale_kw, split, house)
    assert checked > 20


def test_fuse_a_sale_that_turns_into_a_grid_charge_counts_its_own_cover_out_of_the_headroom() -> None:
    """The headroom is measured while the sale still covers the house: 5 kW spread over three phases gives each 7.2 A.
    The house draws 16 A on L1, above its learned peak of 8 A, and the plant's own import limit acts, so the
    plan's HA-down cap does not hold the charge back (`None`, the plan's 10 kW). Changed into a charge, the sale's
    cover is counted out first: L1 ends at the fuse less the margin, not 7 A over the fuse."""
    fuse, volts = 20.0, 400.0
    phase_v = volts / 3**0.5
    peaks = {"L1": 8.0, "L2": 2.0, "L3": 2.0}
    limit = gate.import_limit_max_kw(fuse, volts, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                                     house_phase_peaks_a=peaks)
    plan_cap = gate.grid_charge_cap_kw(fuse, volts, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                                       plant_phases=3, import_limit_kw=limit, import_limit_acts=True,
                                       house_phase_peaks_a=peaks)
    assert plan_cap is None
    without_limit = gate.grid_charge_cap_kw(fuse, volts, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                                            plant_phases=3, house_phase_peaks_a=peaks)
    sale_kw = 5.0
    share_a = sale_kw * 1000.0 / 3.0 / phase_v
    for house in ((16.0, 2.0, 2.0), (12.0, 12.0, 12.0)):
        for plan_kw in (10.0, without_limit):
            assert plan_kw is not None
            executor, battery = _sigenergy()
            now, _ = run(executor, battery, T0, 8, "export_discharge", sale_kw)
            assert executor.state.action == "export_discharge"
            headroom = min(fuse - abs(load - share_a) for load in house)
            grid_kw = (sum(house) - 3 * share_a) * phase_v / 1000.0
            _now, results = run(executor, battery, now, 1, "grid_charge", plan_kw, headroom_a=headroom,
                                power_kw=-sale_kw, grid_import_kw=grid_kw, volts_between_phases=volts)
            change = events(results, "change")[0]
            charge_a = change["rate_kw"] * 1000.0 / 3.0 / phase_v
            assert max(house) + charge_a <= fuse - HEADROOM_MARGIN_A + 0.01, (house, plan_kw, change)
    # With the house at 17.5 A on L1 under the sale there is no room for a charge worth running: handed back.
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "export_discharge", sale_kw)
    house = (17.5, 2.0, 2.0)
    _now, results = run(executor, battery, now, 1, "grid_charge", 10.0,
                        headroom_a=min(fuse - abs(load - share_a) for load in house), power_kw=-sale_kw,
                        grid_import_kw=0.0, volts_between_phases=volts)
    assert events(results, "change") == []
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"


def test_fuse_a_sale_stepped_far_down_is_held_to_its_start_rule() -> None:
    """A sale stepped from 10 kW to 0.5 kW takes back 3.2 kW a phase (13.7 A) of cover at once, as an end into a
    hold would: without that room on the importing phases (and the margin) it is handed back in the same look, not
    a look later. A small step, and a step with room, are made as before."""

    def stepped(new_kw: float, **look: object) -> list:
        executor, battery = _sigenergy()
        now, _ = run(executor, battery, T0, 8, "export_discharge", 10.0)
        assert executor.state.action == "export_discharge" and executor.state.rate_kw == 10.0
        _now, results = run(executor, battery, now, 1, "export_discharge", new_kw, power_kw=-10.0, **look)
        return results

    results = stepped(0.5, headroom_a=12.0)
    assert events(results, "rate") == []
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"
    results = stepped(0.5, headroom_a=16.0)
    assert events(results, "rate")[0]["rate_kw"] == 0.5 and events(results, "hand_back") == []
    # 10 -> 9 kW takes back 1.4 A a phase: left to the fuse hand-back, even with only 3 A of headroom.
    results = stepped(9.0, headroom_a=3.0)
    assert events(results, "rate")[0]["rate_kw"] == 9.0 and events(results, "hand_back") == []
    # Judged on the phases that import: L1 imports with 3 A left (handed back); with 16 A on L1, the exporting
    # phases' small headroom does not count (each exports more than the step takes back).
    results = stepped(0.5, headroom_a=3.0, phase_headroom_a=(3.0, 9.0, 9.0), grid_phase_kw=(3.9, -1.0, -1.0))
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"
    results = stepped(0.5, headroom_a=0.8, phase_headroom_a=(16.0, 0.8, 0.8), grid_phase_kw=(0.9, -4.4, -4.4))
    assert events(results, "rate")[0]["rate_kw"] == 0.5 and events(results, "hand_back") == []
    # A plant not known to spread its sale over three phases may take the whole step back on one phase (never more
    # than the step): 1 kW is 4.3 A, which 20 A of headroom carries and 5 A does not; 9.5 kW is 41 A.
    results = stepped(9.0, headroom_a=20.0, charge_phases=1)
    assert events(results, "rate")[0]["rate_kw"] == 9.0 and events(results, "hand_back") == []
    results = stepped(9.0, headroom_a=5.0, charge_phases=1)
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"
    results = stepped(0.5, headroom_a=20.0, charge_phases=1)
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"


def test_a_sale_stepped_down_is_not_handed_back_for_headroom_on_the_export_side_or_for_the_sun() -> None:
    """A phase that exports only exports less when the sale steps down, however little headroom it has on that side;
    the sun in the plant's power per phase is no part of the step; a plant not known to be three-phase is judged by
    the step, not by its whole sale. A phase that would be left importing without the margin still hands it back."""

    def stepped(start_kw: float, new_kw: float, **look: object) -> list:
        executor, battery = _sigenergy()
        now, _ = run(executor, battery, T0, 8, "export_discharge", start_kw)
        assert executor.state.action == "export_discharge" and executor.state.rate_kw == start_kw
        _now, results = run(executor, battery, now, 1, "export_discharge", new_kw, power_kw=-start_kw, **look)
        return results

    def made(results: list, rate_kw: float) -> bool:
        return events(results, "hand_back") == [] and events(results, "rate")[0]["rate_kw"] == rate_kw

    # Every phase exports 13 A (7 A left on that side): 10 -> 4 kW leaves each exporting 4.3 A.
    assert made(stepped(10.0, 4.0, headroom_a=7.0, phase_headroom_a=(7.0, 7.0, 7.0),
                        grid_phase_kw=(-3.0, -3.0, -3.0)), 4.0)
    # At the export cap (0.8 A left) the whole sale may go: the phases end near 0.
    assert made(stepped(10.0, 0.5, headroom_a=0.8, phase_headroom_a=(0.8, 0.8, 0.8),
                        grid_phase_kw=(-4.4, -4.4, -4.4)), 0.5)
    # 6 kW of sun beside a sale of 5 kW (3.67 kW a phase on the plant's own reading), stepped to 4.7 kW: 0.3 kW.
    sun = {"plant_phase_kw": (3.67, 3.67, 3.67)}
    assert made(stepped(5.0, 4.7, headroom_a=5.5, phase_headroom_a=(5.5, 5.5, 5.5),
                        grid_phase_kw=(-3.3, -3.3, -3.3), **sun), 4.7)
    # The same with L1 importing and 3 A left there: still only the step.
    assert made(stepped(5.0, 4.7, headroom_a=3.0, phase_headroom_a=(3.0, 9.0, 9.0),
                        grid_phase_kw=(3.9, -2.5, -2.5), **sun), 4.7)
    # A plant not known to be three-phase: 4 -> 3.7 kW is 1.3 A on one phase.
    assert made(stepped(4.0, 3.7, headroom_a=19.0, charge_phases=1), 3.7)
    # Still handed back: the step takes more back than a phase exports and leaves it importing without the margin
    # (L1 exports 2 A with 18 A left there, so the fuse may be 20 A; 41 A comes back on it).
    results = stepped(10.0, 0.5, headroom_a=18.0, phase_headroom_a=(18.0, 18.0, 18.0),
                      grid_phase_kw=(-0.46, -0.46, -0.46), charge_phases=1)
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"
    # And with the sun, a large step onto a phase that imports near the fuse (the sun's share is counted: cautious).
    results = stepped(5.0, 0.5, headroom_a=3.0, phase_headroom_a=(3.0, 9.0, 9.0),
                      grid_phase_kw=(3.9, -2.5, -2.5), **sun)
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"
    # The meter's signs not known: the least headroom of any phase, as before.
    results = stepped(10.0, 4.0, headroom_a=7.0)
    assert events(results, "hand_back")[0]["reason"] == "headroom_too_small"


def test_fuse_a_sale_step_the_rule_allows_leaves_every_phase_under_the_fuse_less_the_margin() -> None:
    """Every house, sale, step and spread the step rule lets through leaves each phase at most the fuse less
    `HEADROOM_MARGIN_A`, unless the step takes under the margin off every phase (the fuse hand-back's)."""
    fuse, volts = 20.0, 400.0
    phase_v = volts / 3**0.5
    battery = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES))
    checked = 0
    for sale_kw, new_kw in ((10.0, 0.5), (10.0, 4.0), (6.0, 2.0), (4.0, 0.5), (5.0, 4.0), (12.0, 6.0)):
        for split, phases in (((1 / 3, 1 / 3, 1 / 3), 3), ((0.6, 0.2, 0.2), 3), ((1.0, 0.0, 0.0), 1),
                              ((0.5, 0.5, 0.0), 1)):
            for house in ((0.0, 0.0, 0.0), (18.0, 1.0, 1.0), (10.0, 10.0, 10.0), (17.9, 4.0, 0.0), (5.0, 16.0, 2.0),
                          (12.0, 12.0, 17.0), (15.0, 15.0, 15.0)):
                before = [load - sale_kw * part * 1000.0 / phase_v for load, part in zip(house, split, strict=True)]
                after = [load - new_kw * part * 1000.0 / phase_v for load, part in zip(house, split, strict=True)]
                rooms = tuple(fuse - abs(amps) for amps in before)
                plant = None if split[0] == split[1] == split[2] else tuple(sale_kw * part for part in split)
                for signs in (tuple(amps * phase_v / 1000.0 for amps in before), None):
                    look = inputs(battery, T0, "export_discharge", new_kw, headroom_a=min(rooms), power_kw=-sale_kw,
                                  phase_headroom_a=rooms if signs else None, grid_phase_kw=signs,
                                  plant_phase_kw=plant, charge_phases=phases, volts_between_phases=volts)
                    taken = max(new - old for new, old in zip(after, before, strict=True))
                    if taken >= HEADROOM_MARGIN_A and not Executor._sale_step_too_large(look, new_kw):
                        checked += 1
                        assert max(after) <= fuse - HEADROOM_MARGIN_A + 1e-6, (sale_kw, new_kw, split, house, signs)
    assert checked > 30


def _idle_start(fuse: float, house: tuple[float, float, float], cover: tuple[float, float, float], plan_kw: float):
    """A grid charge started from idle with the house (A a phase) partly covered by the battery in its own mode
    (A a phase): the headroom and the import are the meter's, under that cover."""
    phase_v = 400.0 / 3**0.5
    grid = [load - part for load, part in zip(house, cover, strict=True)]
    executor, battery = _sigenergy()
    _now, results = run(
        executor, battery, T0, 1, "grid_charge", plan_kw, headroom_a=fuse - max(grid),
        power_kw=-sum(cover) * phase_v / 1000.0, grid_import_kw=sum(grid) * phase_v / 1000.0,
        volts_between_phases=400.0,
    )
    return executor, battery, results


def test_fuse_a_grid_charge_from_idle_counts_the_cover_the_battery_gives_in_its_own_mode() -> None:
    """The house takes 10 kW and the battery covers 4 kW of it in its own mode, so the meter reads 6 kW and its
    headroom is 4 kW too large for a charge: the cover ends when the charge starts. A 5 kW charge sized on that
    headroom alone would put a phase over the fuse less the margin; with the cover counted out first (all of it on
    one phase, where it is on is not known) every phase stays under, or the charge is not started."""
    phase_v = 400.0 / 3**0.5
    total_a = 10_000.0 / phase_v
    cover_total_a = 4_000.0 / phase_v
    started = 0
    for fuse in (25.0, 35.0, 50.0):
        for split in ((1 / 3, 1 / 3, 1 / 3), (0.5, 0.3, 0.2), (0.7, 0.2, 0.1)):
            house = tuple(total_a * part for part in split)
            for cover_split in ((1 / 3, 1 / 3, 1 / 3), (1.0, 0.0, 0.0), split):
                cover = tuple(min(cover_total_a * part, load) for part, load in zip(cover_split, house, strict=True))
                if max(load - part for load, part in zip(house, cover, strict=True)) > fuse - HEADROOM_MARGIN_A:
                    continue
                executor, _battery, results = _idle_start(fuse, house, cover, 5.0)  # type: ignore[arg-type]
                if executor.state.phase != "active":
                    assert events(results, "not_started")[0]["reason"] == "fuse_headroom"
                    continue
                started += 1
                charge_a = executor.state.rate_kw * 1000.0 / 3.0 / phase_v
                assert max(house) + charge_a <= fuse - HEADROOM_MARGIN_A + 0.01, (fuse, house, cover, executor.state)
    assert started > 5
    # The example on a 35 A fuse: 26.3 A of headroom less the cover's 17.3 A and the margin leaves 4.86 kW of the 5.
    house = (total_a / 3, total_a / 3, total_a / 3)
    cover = (cover_total_a / 3, cover_total_a / 3, cover_total_a / 3)
    executor, _battery, _results = _idle_start(35.0, house, cover, 5.0)
    assert executor.state.rate_kw == pytest.approx(4.86, abs=0.01)


def test_a_grid_charge_from_idle_without_a_cover_starts_as_before() -> None:
    for power_kw in (0.0, 1.5):  # idle, or the battery storing the sun
        executor, battery = _sigenergy()
        _now, results = run(executor, battery, T0, 1, "grid_charge", 5.0, headroom_a=12.0, power_kw=power_kw,
                            grid_import_kw=3.0)
        # 12 A less the margin over three phases is 6.9 kW: the plan's 5 kW at once.
        assert events(results, "enter")[0]["rate_kw"] == 5.0


def test_a_grid_charge_started_low_for_the_cover_goes_up_once_the_cover_has_ended() -> None:
    """10 kW of house, 1 kW covered, a 25 A fuse: started at 3.9 kW, and at the plan's 5 kW a dwell later, when the
    meter reads the house without the cover."""
    phase_v = 400.0 / 3**0.5
    house_a = 10_000.0 / phase_v / 3
    cover_a_phase = 1_000.0 / phase_v / 3
    executor, battery, _results = _idle_start(25.0, (house_a,) * 3, (cover_a_phase,) * 3, 5.0)
    first = executor.state.rate_kw
    assert first == pytest.approx(3.93, abs=0.02)
    now = T0 + TICK
    for _ in range(int(RATE_UP_DWELL_S / TICK.total_seconds()) + 8):
        rate = executor.state.rate_kw
        now, _ = run(executor, battery, now, 1, "grid_charge", 5.0, power_kw=rate, grid_import_kw=10.0 + rate,
                     headroom_a=25.0 - house_a - rate * 1000.0 / 3.0 / phase_v, volts_between_phases=400.0)
        assert house_a + executor.state.rate_kw * 1000.0 / 3.0 / phase_v <= 25.0 - HEADROOM_MARGIN_A + 0.01
    assert executor.state.phase == "active" and executor.state.rate_kw == 5.0


_PHASE_V = 400.0 / 3**0.5


def _idle_start_phases(
    fuse: float, house: tuple[float, ...], battery_a: tuple[float, ...], sun_a: tuple[float, ...], plan_kw: float, *,
    plant_read: bool, rooms_read: bool, charge_phases: int = 3, mapped: bool = True, order: tuple[int, ...] = (0, 1, 2),
):
    """A grid charge started from idle on a SigenStor in its own mode (A a phase): the battery puts `battery_a` out on
    each phase and the sun `sun_a`; the meter is the house less the two (signed, an export as it is), its headroom
    the fuse less the magnitude. The plant's power per phase and the headroom per phase are read or not; the meter's
    signs are never given. `order`: which of the meter's phases the plant's A, B and C are; `mapped`: the
    verification has shown them to be L1, L2 and L3 (then `order` is that)."""
    grid = [load - part - sun for load, part, sun in zip(house, battery_a, sun_a, strict=True)]
    rooms = tuple(fuse - abs(value) for value in grid)
    plant = tuple((battery_a[index] + sun_a[index]) * _PHASE_V / 1000.0 for index in order)
    executor, battery = _sigenergy()
    _now, results = run(
        executor, battery, T0, 1, "grid_charge", plan_kw, headroom_a=min(rooms),
        phase_headroom_a=rooms if rooms_read else None, plant_phase_kw=plant if plant_read else None,
        power_kw=-sum(battery_a) * _PHASE_V / 1000.0, grid_import_kw=sum(grid) * _PHASE_V / 1000.0,
        volts_between_phases=400.0, charge_phases=charge_phases, plant_phases_mapped=mapped,
    )
    return executor, results


_AMPS = st.floats(min_value=0.0, max_value=30.0, allow_nan=False)
_SUN = st.one_of(st.just(0.0), st.floats(min_value=0.0, max_value=10.0, allow_nan=False))


@settings(max_examples=600, deadline=None)
# The sun on the loaded phase beside a small discharge: it may end with the cover too.
@example(fuse=20.0, house=(20.0, 0.0, 0.0), battery_a=(1.0, 0.0, 0.0), sun_a=(8.0, 0.0, 0.0), plan_kw=5.0,
         plant_read=True, rooms_read=True, charge_phases=3, order=None)
# An unbalanced cover: most of it on the loaded phase.
@example(fuse=20.0, house=(18.0, 6.0, 6.0), battery_a=(12.0, 1.0, 1.0), sun_a=(0.0, 0.0, 0.0), plan_kw=5.0,
         plant_read=True, rooms_read=False, charge_phases=3, order=None)
# The plant's phase C is the meter's L1, not shown yet: the cover on L1 is read on another name.
@example(fuse=20.0, house=(19.0, 3.0, 3.0), battery_a=(12.0, 1.0, 1.0), sun_a=(0.0, 0.0, 0.0), plan_kw=5.0,
         plant_read=True, rooms_read=True, charge_phases=3, order=(1, 2, 0))
@given(
    fuse=st.sampled_from((16.0, 20.0, 25.0, 35.0)),
    house=st.tuples(_AMPS, _AMPS, _AMPS),
    battery_a=st.tuples(_AMPS, _AMPS, _AMPS),
    sun_a=st.tuples(_SUN, _SUN, _SUN),
    plan_kw=st.floats(min_value=0.5, max_value=12.0),
    plant_read=st.booleans(),
    rooms_read=st.booleans(),
    charge_phases=st.sampled_from((1, 3)),
    order=st.one_of(st.none(), st.permutations((0, 1, 2))),
)
def test_fuse_a_grid_charge_from_idle_with_the_cover_per_phase_leaves_every_phase_under_the_fuse(
    fuse: float, house: tuple[float, float, float], battery_a: tuple[float, float, float],
    sun_a: tuple[float, float, float], plan_kw: float, plant_read: bool, rooms_read: bool, charge_phases: int,
    order: tuple[int, ...] | None,
) -> None:
    """Every start from idle the rule allows leaves each phase at most the fuse less the margin once the battery's
    cover has ended: balanced and unbalanced houses and covers, the plant's power per phase and the headroom per
    phase read or not, the meter's signs never known. A charge on one phase may land on any of them. With the
    plant's power per phase read the sun's share on a phase may end with the cover too (the reading has it in it);
    without it the rule counts the battery's discharge only, and the sun is taken to keep feeding the house.
    `order` none: the verification has shown the plant's A, B and C to be L1, L2 and L3; else it has not, and they
    may be any of the meter's phases."""
    assume(sum(battery_a) > 0.05)
    executor, results = _idle_start_phases(
        fuse, house, battery_a, sun_a, plan_kw, plant_read=plant_read, rooms_read=rooms_read,
        charge_phases=charge_phases, mapped=order is None, order=tuple(order) if order is not None else (0, 1, 2),
    )
    if executor.state.phase != "active":
        return
    rate_a = executor.state.rate_kw * 1000.0 / _PHASE_V
    for sun_kept in ((True, False) if plant_read else (True,)):
        after = [load - (sun if sun_kept else 0.0) for load, sun in zip(house, sun_a, strict=True)]
        if charge_phases >= 3:
            worst = max(value + rate_a / 3.0 for value in after)
        else:
            worst = max(after) + rate_a
        assert worst <= fuse - HEADROOM_MARGIN_A + 0.01, (sun_kept, worst, events(results, "enter"))


def _sun_start_phases(
    fuse: float, house: tuple[float, ...], plant_a: tuple[float, ...], storing_kw: float, plan_kw: float, *,
    plant_read: bool = True, rooms_read: bool = True, charge_phases: int = 3, mapped: bool = True,
    order: tuple[int, ...] = (0, 1, 2),
):
    """A grid charge started with the battery idle or storing the sun (`storing_kw`, on the DC side) while the plant
    puts `plant_a` of sun out on each phase (A): the meter is the house less that (signed), its headroom the fuse
    less the magnitude. `order` and `mapped` as in `_idle_start_phases`."""
    grid = [load - part for load, part in zip(house, plant_a, strict=True)]
    rooms = tuple(fuse - abs(value) for value in grid)
    plant = tuple(plant_a[index] * _PHASE_V / 1000.0 for index in order)
    executor, battery = _sigenergy()
    _now, results = run(
        executor, battery, T0, 1, "grid_charge", plan_kw, headroom_a=min(rooms),
        phase_headroom_a=rooms if rooms_read else None, plant_phase_kw=plant if plant_read else None,
        power_kw=storing_kw, grid_import_kw=sum(grid) * _PHASE_V / 1000.0,
        volts_between_phases=400.0, charge_phases=charge_phases, plant_phases_mapped=mapped,
    )
    return executor, results


def _after_sun_start(
    house: tuple[float, ...], plant_a: tuple[float, ...], storing_kw: float, rate_kw: float, charge_phases: int, *,
    even: bool,
) -> float:
    """The most a phase imports (A) once the charge runs at `rate_kw`: the plant then takes `rate_kw` less what the
    battery stored already more from its AC side. `even`: its power is then the same on each phase (what it had
    above its average on a phase is gone from there); else the sun stays where it was and the change is spread over
    the plant's phases, or all on one where the charge is not known to be balanced."""
    step_a = (rate_kw - storing_kw) * 1000.0 / _PHASE_V
    if even:
        each = (sum(plant_a) - step_a) / 3.0
        return max(load - each for load in house)
    grid = [load - part for load, part in zip(house, plant_a, strict=True)]
    return max(grid) + (step_a / 3.0 if charge_phases >= 3 else max(step_a, 0.0))


@settings(max_examples=800, deadline=None)
# The owner's review case: 13 A of house on L1 all met by the sun on a 16 A fuse, the battery idle.
@example(fuse=16.0, house=(13.0, 0.0, 0.0), even_a=0.0, extra_a=(13.0, 0.0, 0.0), storing_kw=0.0, plan_kw=12.0,
         plant_read=True, rooms_read=True, charge_phases=3, order=None)
# The same read on another of the plant's names, the mapping not shown.
@example(fuse=16.0, house=(13.0, 0.0, 0.0), even_a=0.0, extra_a=(13.0, 0.0, 0.0), storing_kw=0.0, plan_kw=12.0,
         plant_read=True, rooms_read=True, charge_phases=3, order=(1, 2, 0))
# Strong even sun the battery stores most of, and a little more on the loaded phase.
@example(fuse=16.0, house=(12.0, 2.0, 2.0), even_a=4.0, extra_a=(8.0, 0.0, 0.0), storing_kw=4.0, plan_kw=8.0,
         plant_read=True, rooms_read=False, charge_phases=3, order=None)
# A charge not known to be balanced.
@example(fuse=20.0, house=(15.0, 5.0, 0.0), even_a=2.0, extra_a=(10.0, 3.0, 0.0), storing_kw=0.0, plan_kw=6.0,
         plant_read=True, rooms_read=True, charge_phases=1, order=None)
@given(
    fuse=st.sampled_from((16.0, 20.0, 25.0, 35.0)),
    house=st.tuples(_AMPS, _AMPS, _AMPS),
    even_a=st.floats(min_value=0.0, max_value=20.0, allow_nan=False),
    extra_a=st.one_of(st.just((0.0, 0.0, 0.0)), st.tuples(_SUN, _SUN, _SUN), st.tuples(_AMPS, _SUN, _SUN)),
    storing_kw=st.one_of(st.just(0.0), st.floats(min_value=0.0, max_value=8.0, allow_nan=False)),
    plan_kw=st.floats(min_value=0.5, max_value=12.0),
    plant_read=st.booleans(),
    rooms_read=st.booleans(),
    charge_phases=st.sampled_from((1, 3)),
    order=st.one_of(st.none(), st.permutations((0, 1, 2))),
)
def test_fuse_a_grid_charge_started_under_the_sun_leaves_every_phase_under_the_fuse(
    fuse: float, house: tuple[float, float, float], even_a: float, extra_a: tuple[float, float, float],
    storing_kw: float, plan_kw: float, plant_read: bool, rooms_read: bool, charge_phases: int,
    order: tuple[int, ...] | None,
) -> None:
    """Every start the rule allows with the battery idle or storing the sun leaves each phase at most the fuse less
    the margin: the sun the same on every phase (`even_a`) and more on some (`extra_a`), balanced and unbalanced
    houses, the plant's power per phase and the headroom per phase read or not, the mapping shown or not (`order` as
    in the test above), a charge on three phases or on any one.

    What is assumed of the plant under the charge is one of two things. Its power is the same on each of its phases,
    so a phase loses what the sun gave it above the plant's average: held wherever the plant's power per phase is
    read. Or the sun stays where it was and only the charge's own change is added: held always. Not held, and not
    claimed: a plant whose power per phase is not read and that still moves the sun off a loaded phase when the
    charge starts (the rule then has nothing to count it by), and a plant that under the charge puts less sun on a
    phase than a third of what it has left."""
    plant_a = tuple(even_a + part for part in extra_a)
    executor, results = _sun_start_phases(
        fuse, house, plant_a, storing_kw, plan_kw, plant_read=plant_read, rooms_read=rooms_read,
        charge_phases=charge_phases, mapped=order is None, order=tuple(order) if order is not None else (0, 1, 2),
    )
    if executor.state.phase != "active":
        return
    rate = executor.state.rate_kw
    for even in ((True, False) if plant_read else (False,)):
        worst = _after_sun_start(house, plant_a, storing_kw, rate, charge_phases, even=even)
        assert worst <= fuse - HEADROOM_MARGIN_A + 0.01, (even, worst, events(results, "enter"))


def test_fuse_a_grid_charge_counts_the_sun_a_phase_has_above_the_plants_average() -> None:
    """13 A of house on L1 of a 16 A fuse, all met by the sun the plant puts there, the battery idle: the meter
    reads 0 A and 16 A of headroom on every phase. Sized on that alone the charge is 9.7 kW, and with the plant's
    power even under it L1 ends near 23 A. L1's sun is 8.7 A above the plant's average of 4.3 A: counted out, the
    start is 5.3 A a phase (3.68 kW) and L1 ends at the fuse less the margin."""
    house, sun = (13.0, 0.0, 0.0), (13.0, 0.0, 0.0)
    executor, _results = _sun_start_phases(16.0, house, sun, 0.0, 12.0, plant_read=False)
    assert executor.state.rate_kw == pytest.approx(3 * 14.0 * _PHASE_V / 1000.0, abs=0.01)
    assert _after_sun_start(house, sun, 0.0, executor.state.rate_kw, 3, even=True) == pytest.approx(22.67, abs=0.02)
    executor, _results = _sun_start_phases(16.0, house, sun, 0.0, 12.0)
    assert executor.state.rate_kw == pytest.approx(3 * (16.0 - 26.0 / 3 - 2.0) * _PHASE_V / 1000.0, abs=0.01)
    assert _after_sun_start(house, sun, 0.0, executor.state.rate_kw, 3, even=True) == pytest.approx(14.0, abs=0.01)
    # The mapping not shown: the largest cover off the least headroom, the same start here whichever phase it is on.
    for order in ((0, 1, 2), (1, 2, 0), (2, 0, 1)):
        unmapped, _results = _sun_start_phases(16.0, house, sun, 0.0, 12.0, mapped=False, order=order)
        assert unmapped.state.rate_kw == executor.state.rate_kw
    # The battery storing 2 kW of a larger sun beside it: the same cover, the same start.
    storing, _results = _sun_start_phases(16.0, (15.0, 2.0, 2.0), (15.0, 2.0, 2.0), 2.0, 12.0)
    assert storing.state.rate_kw == pytest.approx(executor.state.rate_kw, abs=0.01)


def test_a_grid_charge_under_the_same_sun_on_every_phase_starts_as_without_the_reading() -> None:
    """The sun the plant puts evenly on its phases is no cover: a midday charge is sized as it was before the
    plant's power per phase was read, the battery idle or storing the sun."""
    cases = (
        ((2.0, 2.0, 2.0), (5.0, 5.0, 5.0), 0.0, 6.0),   # 3.45 kW of sun, each phase exporting 3 A
        ((3.0, 1.0, 0.5), (4.3, 4.3, 4.3), 0.0, 12.0),  # an unbalanced house under 3 kW of even sun
        ((1.0, 1.0, 1.0), (1.0, 1.0, 1.0), 9.0, 12.0),  # 9.7 kW of sun, 9 kW of it stored, a 12 kW plan
        ((1.0, 1.0, 1.0), (14.4, 14.4, 14.4), 0.0, 6.0),  # 10 kW of even sun all exported
    )
    started = 0
    for house, sun, storing_kw, plan_kw in cases:
        with_reading, _results = _sun_start_phases(16.0, house, sun, storing_kw, plan_kw)
        without, _results = _sun_start_phases(16.0, house, sun, storing_kw, plan_kw, plant_read=False)
        assert with_reading.state.phase == without.state.phase
        assert with_reading.state.rate_kw == pytest.approx(without.state.rate_kw, abs=0.001)
        started += with_reading.state.phase == "active"
    assert started == 3  # the last one exports 13.4 A a phase: 2.6 A of headroom by the meter, with or without


def test_fuse_a_on_a_20_a_fuse_the_cover_per_phase_lets_a_charge_from_idle_start() -> None:
    """The owner's case: a SigenStor on a 20 A fuse, the house at 10 kW and the battery covering 4 kW of it in its own
    mode (17.3 A on one phase, more than the 11.3 A of headroom). Without the plant's power per phase nothing starts.
    With it each phase's own 5.8 A of cover comes off that phase: 3.5 A a phase above the margin, a 2.47 kW start,
    which puts every phase at the fuse less the margin once the cover has ended."""
    house = (10_000.0 / _PHASE_V / 3,) * 3
    cover = (4_000.0 / _PHASE_V / 3,) * 3
    for rooms_read in (True, False):
        executor, results = _idle_start_phases(20.0, house, cover, (0.0,) * 3, 5.0, plant_read=False,
                                               rooms_read=rooms_read)
        assert executor.state.phase != "active"
        assert events(results, "not_started")[0]["reason"] == "fuse_headroom"
        executor, _results = _idle_start_phases(20.0, house, cover, (0.0,) * 3, 5.0, plant_read=True,
                                                rooms_read=rooms_read)
        assert executor.state.phase == "active"
        assert executor.state.rate_kw == pytest.approx(2.47, abs=0.01)
    # An unbalanced house (L2 the heaviest) the battery covers phase by phase: L2's own cover and headroom decide.
    house = (9.0, 16.0, 12.0)
    cover = (2.5, 8.0, 6.8)  # 17.3 A, 4 kW in all
    executor, _results = _idle_start_phases(20.0, house, cover, (0.0,) * 3, 5.0, plant_read=True, rooms_read=True)
    assert executor.state.phase == "active"
    assert executor.state.rate_kw == pytest.approx(3 * 2.0 * _PHASE_V / 1000.0, abs=0.01)  # 2 A a phase: 1.39 kW
    assert 16.0 + executor.state.rate_kw * 1000.0 / _PHASE_V / 3 <= 20.0 - HEADROOM_MARGIN_A + 0.01
    executor, _results = _idle_start_phases(20.0, house, cover, (0.0,) * 3, 5.0, plant_read=False, rooms_read=True)
    assert executor.state.phase != "active"


def test_fuse_a_cover_per_phase_is_not_paired_with_the_meters_phases_before_the_mapping_is_shown() -> None:
    """The plant's phase A may be the meter's L3 until the verification's mode `per_phase` has shown otherwise. A
    house with 19 A on L1 that the battery covers 12 A of, read by the plant as its phase C: paired by name, L1's
    12 A of headroom would lose only the 1 A the plant reports on its A, and the charge would put L1 above the fuse
    once the cover ends. Unmapped, the largest cover comes off the least headroom and nothing starts; mapped and in
    the meter's order, the same house starts within the fuse."""
    house, cover = (19.0, 3.0, 3.0), (12.0, 1.0, 1.0)
    executor, results = _idle_start_phases(20.0, house, cover, (0.0,) * 3, 5.0, plant_read=True, rooms_read=True,
                                           mapped=False, order=(1, 2, 0))
    assert executor.state.phase != "active"
    assert events(results, "not_started")[0]["reason"] == "fuse_headroom"
    # The same readings in the meter's order, unmapped: still the largest cover off the least headroom.
    executor, _results = _idle_start_phases(20.0, house, cover, (0.0,) * 3, 5.0, plant_read=True, rooms_read=True,
                                            mapped=False)
    assert executor.state.phase != "active"
    # A lighter house, mapped: each phase's own cover; unmapped the start is lower, never higher.
    house = (14.0, 3.0, 3.0)
    rates = []
    for mapped in (True, False):
        executor, _results = _idle_start_phases(20.0, house, (8.0, 1.0, 1.0), (0.0,) * 3, 9.0, plant_read=True,
                                                rooms_read=True, mapped=mapped)
        assert executor.state.phase == "active"
        assert 14.0 + executor.state.rate_kw * 1000.0 / _PHASE_V / 3 <= 20.0 - HEADROOM_MARGIN_A + 0.01
        rates.append(executor.state.rate_kw)
    assert rates[1] <= rates[0]


def test_a_running_grid_charge_does_not_count_a_discharge_still_read_as_cover() -> None:
    """The cover is counted on the change only: in the running charge a battery that still reads a discharge (the
    plant ramping) does not lower the cap a second time."""
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 8, "grid_charge", 4.0)
    assert executor.state.action == "grid_charge" and executor.state.rate_kw == 4.0
    _now, results = run(executor, battery, now, 1, "grid_charge", 4.0, headroom_a=12.0, power_kw=-3.0)
    assert events(results, "rate") == [] and executor.state.rate_kw == 4.0


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
    # The battery covers 3 kW, which can all be on one phase (SigenStor covers each phase on its own): 13 A at
    # 230 V. 8 A of headroom, enough if it were spread over three phases (4.3 A), is not enough to hold it again.
    now, results = run(executor, battery, now, _looks(PEAK_REHOLD_S), "hold", headroom_a=8.0, power_kw=-3.0)
    assert written(results) == [] and events(results, "peak_rehold") == []
    _now, results = run(executor, battery, now, 3, "hold", headroom_a=16.0, power_kw=-3.0)
    assert events(results, "peak_rehold")[0]["headroom_a"] == 16.0
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


def test_a_restore_given_up_is_tried_again_on_a_slow_timer_while_blocked() -> None:
    executor, battery = _sigenergy()
    now = _held(executor, battery)
    battery.raises = {"discharge_limit"}
    while executor.state.phase != "blocked":
        now, _ = run(executor, battery, now, 1, "self_consume")
    assert executor.state.blocked == "restore_failed:discharge_limit"
    # Still failing: tried again every BLOCKED_RESTORE_S, never more often.
    now, results = run(executor, battery, now, _looks(BLOCKED_RESTORE_S) - 3, "self_consume")
    assert written(results) == []
    now, results = run(executor, battery, now, 4, "self_consume")
    assert written(results) == [("discharge_limit", 23.04)]
    # The integration takes writes again: the next try restores it and the block ends.
    battery.raises = set()
    _now, results = run(executor, battery, now, _looks(sum(RESTORE_RETRY_S) + BLOCKED_RESTORE_S), "self_consume")
    assert battery.states["discharge_limit"] == "23.04"
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


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


def test_watchdog_renewals_do_not_count_against_the_day_s_write_cap() -> None:
    # A SolarEdge hold renews two roles every WATCHDOG_RENEW_S: 192 writes a day, past the cap of 96.
    executor = Executor(SOLAREDGE_CONTROL)
    battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES))
    now, _ = run(executor, battery, T0, 6, "hold")
    entered = executor.state.writes_today
    renewals = 0
    for _ in range(DAILY_WRITE_CAP // 2 + 4):
        now, results = run(executor, battery, now + timedelta(seconds=WATCHDOG_RENEW_S), 3, "hold")
        renewals += len(written(results))
    assert renewals > DAILY_WRITE_CAP and now.date() == T0.date()
    assert executor.state.phase == "active" and executor.state.writes_today == entered


_SLOW_WATCHDOGS = pytest.mark.parametrize(
    ("control", "states", "unreadable"),
    [
        (SOLAREDGE_CONTROL, SOLAREDGE_STATES, {"command_mode": "unavailable"}),
        # Huawei's summary sensor reads unavailable now and then.
        (HUAWEI_CONTROL, HUAWEI_STATES, {"forcible": "unavailable"}),
        (SOLAX_CONTROL, SOLAX_STATES, {"autorepeat": "unavailable"}),
    ],
    ids=["solaredge", "huawei", "solax"],
)


@_SLOW_WATCHDOGS
def test_a_renewal_on_the_shared_timer_is_never_counted_even_when_its_role_reads_something_else(
    control, states, unreadable
) -> None:
    # The brands on WATCHDOG_RENEW_S renew as they always did: a renewal is the dead-man, never a change, whatever
    # the role reads when it is due (only a recipe's fast watchdog counts a renewal whose value moved).
    assert control.watchdog_renew_s is None and control.watchdog_roles
    executor, battery = Executor(control), FakeBattery(control, dict(states))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 2.0)
    assert executor.state.phase == "active"
    entered = executor.state.writes_today
    battery.states |= unreadable
    now, results = run(executor, battery, now + timedelta(seconds=WATCHDOG_RENEW_S), 6, "grid_charge", 2.0)
    assert events(results, "renew") and set(control.watchdog_roles) <= {role for role, _ in written(results)}
    assert executor.state.writes_today == entered


@_SLOW_WATCHDOGS
def test_a_renewal_on_the_shared_timer_starts_on_time_even_when_the_last_round_has_not_ended(
    control, states, unreadable
) -> None:
    # A round left unfinished (a role still in it) does not hold the next one back: it starts every WATCHDOG_RENEW_S,
    # with every watchdog role, as before the fast watchdog was added.
    del unreadable
    executor, battery = Executor(control), FakeBattery(control, dict(states))
    now, _ = run(executor, battery, T0, 8, "grid_charge", 2.0)
    executor.state = replace(executor.state, renew=control.watchdog_roles[:1])
    now += timedelta(seconds=WATCHDOG_RENEW_S)
    _now, results = run(executor, battery, now, 1, "grid_charge", 2.0)
    assert [event["reason"] for event in events(results, "renew")] == [",".join(control.watchdog_roles)]


def test_the_brands_before_victron_set_every_role_they_write_in_every_action_and_the_hand_back() -> None:
    # So releasing a role the running action no longer sets (`Executor._release_unset`, for targets the plant owns
    # outside the charge) and treating a late read-back of such a role as superseded never touch them.
    for control in (SIGENERGY_CONTROL, SOLAREDGE_CONTROL, HUAWEI_CONTROL, SOLAX_CONTROL):
        for phased in (False, True):
            if phased and control.phase_control is None:
                continue
            ctl = control.with_phases(True) if phased else control
            actions = ctl.states
            ever = set().union(*(ctl.wanted(action, 2.0) for action in actions))
            for action in actions:
                assert ever <= set(ctl.wanted(action, 2.0)), (control.brand, phased, action)
            back = {role.name for role in ctl.roles if role.restored} | set(NORMAL.get(control.brand, {}))
            assert ever <= back, (control.brand, phased)
            assert ctl.watchdog_renew_s is None and not ctl.house_states, control.brand


def test_a_role_never_written_that_reads_unknown_for_a_moment_is_not_written_on_the_way_back() -> None:
    executor = Executor(SOLAREDGE_CONTROL)
    battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES))
    now, results = run(executor, battery, T0, 6, "hold")
    assert "charge_limit" not in [role for role, _ in written(results)]
    # The limits blink while the command mode changes back: no reading is no reason to write the snapshot.
    battery.states["charge_limit"] = "unavailable"
    battery.states["discharge_limit"] = "unknown"
    _now, results = run(executor, battery, now, 8, "self_consume")
    assert [role for role, _ in written(results)] == ["command_mode", "command_timeout"]
    assert executor.state.phase == "idle"


def test_huawei_charges_through_its_forcible_charge_service_and_stops_it_on_the_way_back() -> None:
    executor = Executor(HUAWEI_CONTROL)
    battery = FakeBattery(HUAWEI_CONTROL, dict(HUAWEI_STATES))
    now, results = run(executor, battery, T0, 4, "grid_charge", 3.0)
    assert written(results) == [("forcible", "charge:3000")]
    assert battery.writes[0].device_id == "DEVICE"
    now, results = run(executor, battery, now, 4, "hold")
    # The grid charge is stopped first, then the hold set.
    assert written(results) == [("forcible", "stop"), ("discharge_power", 0.0)]
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
    battery.idle_kw = 0.0  # nothing covered at the start: the cover is counted out of a start from idle
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


# --- every hand-back ends -------------------------------------------------------------------------------------------

#: Forty minutes of looks: longer than every restore retry together.
_HAND_BACK_LOOKS = int(40 * 60 / TICK.total_seconds())


def _ended(executor: Executor) -> None:
    state = executor.state
    assert state.phase in ("idle", "blocked"), state
    assert state.retries == {} and state.pending is None and state.in_flight == ()


@pytest.mark.parametrize("brand", ["sigenergy", "solaredge", "huawei", "solax"])
@pytest.mark.parametrize("action", ["hold", "grid_charge", "discharge_house", "export_discharge"])
@pytest.mark.parametrize(
    ("plan", "overrides"),
    [
        ("same", {"stopping": "stop"}),
        ("self_consume", {}),
        (None, {}),
        ("same", {"battery_age_s": 600.0}),
        ("same", {"headroom_a": 0.5}),
        ("same", {"management": Management("self_managed", "planner:predbat")}),
        ("same", {"on_grid": False}),
    ],
)
def test_every_hand_back_path_ends_idle_or_blocked_with_no_retry_left(brand, action, plan, overrides) -> None:
    control = control_for(brand)
    states = {"sigenergy": SIGENERGY_STATES, "solaredge": SOLAREDGE_STATES, "huawei": HUAWEI_STATES,
              "solax": SOLAX_STATES}[brand]
    if "headroom_a" in overrides and action not in ("hold", "grid_charge", "export_discharge"):
        overrides = {"stopping": "stop"}  # the fuse hands back only an action that adds import or stops covering it
    executor, battery = Executor(control), FakeBattery(control, dict(states))
    now, _ = run(executor, battery, T0, 8, action, 3.0)
    assert executor.state.phase == "active"
    _now, _ = run(executor, battery, now, _HAND_BACK_LOOKS, action if plan == "same" else plan, 3.0, **overrides)
    _ended(executor)


def test_a_fuse_hand_back_of_a_sigenergy_grid_charge_ends_though_remote_ems_off_hides_the_mode() -> None:
    # Remote EMS off makes the mode select unavailable before Maximum Self Consumption reads back: the mode is not
    # confirmed, but it needs no write any more (`idle_when`).
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    _now, results = run(executor, battery, now, _HAND_BACK_LOOKS, "grid_charge", 4.0, headroom_a=0.5)
    _ended(executor)
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot
    assert battery.states["remote_ems"] == "off" and battery.states["charge_limit"] == "10"
    executor.reset()
    assert executor.state.phase == "idle"


def test_a_write_still_pending_when_a_restore_begins_is_no_restore_retry() -> None:
    # Remote EMS on is dropped silently; the part stops before its read-back deadline.
    executor, battery = _sigenergy(drops={"remote_ems"})
    now, _ = run(executor, battery, T0, 3, "hold")
    assert executor.state.pending == "remote_ems"
    _now, results = run(executor, battery, now, _HAND_BACK_LOOKS, "hold", stopping="stop")
    _ended(executor)
    assert battery.states["discharge_limit"] == "23.04"
    assert ("remote_ems", True) not in [(op.role, op.value) for r in results for op in r.ops]


def test_a_restore_write_pending_across_a_restart_ends_once_the_role_needs_no_write() -> None:
    executor, battery = _sigenergy()
    now, _ = run(executor, battery, T0, 6, "grid_charge", 4.0)
    now, _ = run(executor, battery, now, 1, "grid_charge", 4.0, headroom_a=0.5)
    assert executor.state.phase == "restoring" and executor.state.in_flight
    stored = executor.as_dict()
    again = Executor(
        SIGENERGY_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        ExecState.from_dict(stored["state"]),
    )
    later = now + timedelta(seconds=READBACK_DEADLINE_S + 60)
    again.restart(later)
    _now, _ = run(again, battery, later, _HAND_BACK_LOOKS, "grid_charge", 4.0, headroom_a=0.5)
    _ended(again)
    assert not again.state.holds_snapshot


def test_solaredge_s_timeout_is_written_again_well_inside_it_when_the_action_changes_every_quarter_hour() -> None:
    """A plan of 15-minute slots may change the action at every quarter. Each change writes the new command mode,
    but the timeout itself, unchanged, would not be, and the renewal counts from the change: the timeout register
    would then never be written again after the start. Whatever restarts the inverter's timer (the timeout's write
    or the command's), it must be written again well before `WATCHDOG_S` runs out."""
    executor = Executor(SOLAREDGE_CONTROL)
    battery = FakeBattery(SOLAREDGE_CONTROL, dict(SOLAREDGE_STATES))
    now = T0
    times: list[float] = []
    quarter = _looks(WATCHDOG_RENEW_S) - 1
    for n in range(10):
        start = now
        now, results = run(executor, battery, now, quarter, ("hold", "discharge_house")[n % 2])
        for index, result in enumerate(results):
            if any(op.role == "command_timeout" for op in result.ops):
                times.append((start - T0).total_seconds() + index * TICK.total_seconds())
        assert executor.state.phase == "active"
    gaps = [later - earlier for earlier, later in zip([0.0, *times], [*times, (now - T0).total_seconds()], strict=True)]
    assert max(gaps) <= WATCHDOG_RENEW_S + 60, gaps
    assert max(gaps) < WATCHDOG_S / 2 + 60
