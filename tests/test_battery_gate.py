"""The battery control's gate (`battery/gate.py`): the site's options, the verification report per firmware, the
actions the report allows, when the routine may run, the grid charge cap and the fast shedding order."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.gate import (
    CONF_ENABLED,
    CONF_SHADOW_ONLY,
    HOUSE_PEAK_DEFAULT_KW,
    MAX_ATTEMPTS,
    Moment,
    Options,
    allowed_actions,
    battery_yields,
    departure_has_room,
    failed_mode,
    firmware_key,
    fuse_kw,
    grid_charge_cap_kw,
    keep_reports,
    merge_report,
    merged_action,
    mode_verdict,
    modes_to_run,
    needs_run,
    report_status,
    restricted,
    suitable,
    trial_refusal,
)

NOW = datetime(2026, 11, 16, 19, 0, tzinfo=timezone.utc)


def _run(**outcomes: str) -> dict:
    return {
        "brand": "sigenergy", "firmware": "FW", "started_at": NOW.isoformat(),
        "modes": {mode: {"mode": mode, "outcome": outcome} for mode, outcome in outcomes.items()},
    }


def _report(attempts: int = 1, **outcomes: str) -> dict:
    return merge_report(None, _run(**outcomes), key="K", attempts=attempts, now=NOW)


def test_the_options_default_to_off_and_shadow_only_and_only_a_stored_boolean_counts() -> None:
    assert Options.from_config({}) == Options()
    assert Options().shadow_only and not Options().enabled and not Options().writes
    assert Options.from_config({CONF_ENABLED: True}).writes is False, "shadow only stays on by default"
    assert Options.from_config({CONF_ENABLED: True, CONF_SHADOW_ONLY: False}).writes is True
    assert Options.from_config({CONF_ENABLED: "yes", CONF_SHADOW_ONLY: 0}) == Options()


def test_the_report_key_names_the_firmware_and_the_integration_version() -> None:
    assert firmware_key("SPC117", "sigen", "2.0.3") != firmware_key("SPC118", "sigen", "2.0.3")
    assert firmware_key("SPC117", "sigen", "2.0.3") != firmware_key("SPC117", "sigen", "2.0.4")
    assert "unknown" in firmware_key(None, None, None)


@pytest.mark.parametrize(
    ("outcome", "verdict"),
    [
        ("obeyed", "pass"), ("restored", "pass"), ("ignored", "fail"), ("partial", "fail"),
        ("aborted:write_not_taken:discharge_limit:no_read_back", "fail"), ("aborted:command_timeout", "fail"),
        ("aborted:changed_outside_spotnav:remote_ems", "fail"),
        ("not_judgeable:battery_not_discharging", "pending"), ("aborted:fuse_headroom", "pending"),
        ("aborted:unload", "pending"), ("aborted:stale_data", "pending"), ("skipped", "pending"), (None, "pending"),
    ],
)
def test_each_outcome_passes_fails_or_is_tried_again(outcome: str | None, verdict: str) -> None:
    assert mode_verdict(outcome) == verdict


def test_a_pass_needs_hold_and_the_hand_back_and_the_report_decides_the_actions() -> None:
    passed = _report(hold="obeyed", charge="obeyed", discharge_house="obeyed", normal="restored")
    assert report_status(passed) == "passed"
    assert allowed_actions(passed, Options()) == {"hold", "grid_charge", "discharge_house"}
    assert "export_discharge" in allowed_actions(passed, Options(allow_sell=True)), "selling only when allowed"
    only_hold = _report(hold="obeyed", charge="ignored", discharge_house="not_judgeable:x", normal="restored")
    assert report_status(only_hold) == "passed" and allowed_actions(only_hold, Options()) == {"hold"}
    assert modes_to_run(only_hold) == ("discharge_house",) and needs_run(only_hold)
    failed = _report(hold="ignored", charge="obeyed", normal="restored")
    assert report_status(failed) == "failed" and allowed_actions(failed, Options()) == frozenset()
    assert failed_mode(failed) == "hold" and not needs_run(failed)
    assert report_status(None) == "none" and allowed_actions(None, Options()) == frozenset()


def test_a_mode_that_could_not_be_judged_is_tried_again_and_a_decided_one_is_kept() -> None:
    first = _report(hold="not_judgeable:battery_not_discharging", charge="obeyed", discharge_house="aborted:fuse_headroom",
                    normal="restored")
    assert report_status(first) == "pending" and modes_to_run(first) == ("hold", "discharge_house")
    later = merge_report(first, _run(hold="obeyed", discharge_house="obeyed", normal="restored"), key="K", attempts=2,
                         now=NOW + timedelta(days=1))
    assert report_status(later) == "passed" and later["attempts"] == 2
    # A decided mode is not overwritten by a later run.
    again = merge_report(later, _run(hold="ignored"), key="K", attempts=3, now=NOW + timedelta(days=2))
    assert again["modes"]["hold"]["outcome"] == "obeyed"
    # The routine gives up after MAX_ATTEMPTS runs without a decision.
    stuck = _report(attempts=MAX_ATTEMPTS, hold="not_judgeable:x", normal="restored")
    assert report_status(stuck) == "failed" and not needs_run(stuck)


def test_reports_are_bounded_newest_last() -> None:
    reports: dict = {}
    for index in range(5):
        reports = keep_reports(reports, f"K{index}", {"updated_at": (NOW + timedelta(days=index)).isoformat()}, most=3)
    assert list(reports) == ["K2", "K3", "K4"]


def _moment(**changes) -> Moment:
    base = dict(local_hour=19, soc_pct=55.0, power_kw=-1.5, fresh=True, measurement_ok=True, headroom_a=12.0,
                car_charging=False, blocker=None, executor_idle=True)
    return Moment(**(base | changes))


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({}, None),
        ({"soc_pct": 92.0}, "soc_out_of_range"),
        ({"soc_pct": 20.0}, "soc_out_of_range"),
        ({"power_kw": 0.4}, "battery_not_discharging"),
        ({"car_charging": True}, "car_charging"),
        ({"headroom_a": 2.0}, "headroom_too_small"),
        ({"fresh": False}, "stale_data"),
        ({"blocker": "self_managed:EMS work mode:ai_mode"}, "self_managed:EMS work mode:ai_mode"),
        ({"executor_idle": False}, "executor_busy"),
    ],
)
def test_the_routine_runs_only_at_a_suitable_moment(changes: dict, why: str | None) -> None:
    assert suitable(_moment(**changes), evening=False) == why


def test_a_re_verification_waits_for_an_evening() -> None:
    assert suitable(_moment(local_hour=12), evening=True) == "not_evening"
    assert suitable(_moment(local_hour=12), evening=False) is None
    assert suitable(_moment(local_hour=20), evening=True) is None


def test_the_grid_charge_is_capped_to_leave_a_house_peak_under_the_fuse_until_the_import_limit_is_shown_to_act() -> None:
    fuse = fuse_kw(20.0, 400.0, 2.0)
    assert fuse == pytest.approx(12.47, abs=0.01)
    assert grid_charge_cap_kw(fuse, 3.0, import_limit_acts=False) == pytest.approx(fuse - 3.0)
    assert grid_charge_cap_kw(fuse, None, import_limit_acts=False) == pytest.approx(fuse - HOUSE_PEAK_DEFAULT_KW)
    assert grid_charge_cap_kw(fuse, 3.0, import_limit_acts=True) is None
    assert grid_charge_cap_kw(None, 3.0, import_limit_acts=False) == 0.0, "an unknown fuse allows no grid charge"
    slot = ("grid_charge", 15.0, None)
    assert restricted(slot, {"grid_charge"}, 9.0) == (("grid_charge", 9.0, None), "grid_charge_cap")
    assert restricted(slot, {"grid_charge"}, 0.2) == (("self_consume", 0.0, None), "grid_charge_cap")
    assert restricted(slot, {"grid_charge"}, None) == (slot, None)


def test_an_action_the_report_does_not_allow_becomes_self_consumption() -> None:
    assert restricted(("discharge_house", 3.0, None), {"hold"}, None) == (
        ("self_consume", 0.0, None), "not_verified:discharge_house"
    )
    assert restricted(("export_discharge", 3.0, None), {"hold", "discharge_house"}, None)[0][0] == "self_consume"
    assert restricted(None, {"hold"}, None) == (None, None)


def test_the_hold_for_the_car_fills_a_self_consumption_slot_only() -> None:
    end = NOW + timedelta(minutes=15)
    assert merged_action(("self_consume", 0.0, end), True) == ("hold", 0.0, end)
    assert merged_action(None, True) == ("hold", 0.0, None)
    assert merged_action(("grid_charge", 3.0, end), True) == ("grid_charge", 3.0, end)
    assert merged_action(("self_consume", 0.0, end), False) == ("self_consume", 0.0, end)


def test_selling_from_the_battery_is_off_by_default_also_for_the_test_action() -> None:
    assert trial_refusal("export_discharge", Options()) == "sell_not_allowed"
    assert trial_refusal("export_discharge", Options(allow_sell=True)) is None
    assert trial_refusal("hold", Options()) is None and trial_refusal("curtail_export", Options()) is None
    assert trial_refusal("explode", Options()) == "unknown_action"


def test_the_car_backs_off_first_when_its_departure_has_room() -> None:
    assert departure_has_room(NOW + timedelta(hours=8), NOW, 10.0, 7.0)
    assert not departure_has_room(NOW + timedelta(hours=3), NOW, 10.0, 7.0)
    assert not departure_has_room(None, NOW, 10.0, 7.0)
    assert battery_yields(True, False) is True
    assert battery_yields(True, True) is False
    assert battery_yields(False, False) is False, "battery first: the site's load balancing backs the car off"
