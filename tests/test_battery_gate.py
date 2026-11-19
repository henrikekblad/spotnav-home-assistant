"""The battery control's gate (`battery/gate.py`): the site's options, the verification report per firmware, the
actions the report allows, when the routine may run, the grid charge cap and the fast shedding order."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.spotnav.battery.commands import control_for
from custom_components.spotnav.battery.gate import (
    CONF_ENABLED,
    CONF_SHADOW_ONLY,
    HOUSE_PEAK_DEFAULT_KW,
    MAX_ATTEMPTS,
    MIN_RATE_KW,
    Moment,
    Options,
    allowed_actions,
    battery_yields,
    departure_has_room,
    export_discharge_cap_kw,
    failed_mode,
    firmware_key,
    fuse_kw,
    grid_charge_cap_kw,
    grid_charge_phase_caps_kw,
    import_limit_holds,
    import_limit_max_kw,
    keep_reports,
    merge_report,
    merged_action,
    mode_verdict,
    modes_to_run,
    needs_run,
    phase_split_kw,
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
        ("aborted:call_timeout:remote_ems", "pending"),
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
    assert "export_discharge" in allowed_actions(passed, Options(mode="max_profit")), "selling only when allowed"
    only_hold = _report(hold="obeyed", charge="ignored", discharge_house="not_judgeable:x", normal="restored")
    assert report_status(only_hold) == "passed" and allowed_actions(only_hold, Options()) == {"hold"}
    assert modes_to_run(only_hold) == ("discharge_house",) and needs_run(only_hold)
    failed = _report(hold="ignored", charge="obeyed", normal="restored")
    assert report_status(failed) == "failed" and allowed_actions(failed, Options()) == frozenset()
    assert failed_mode(failed) == "hold" and not needs_run(failed)
    assert report_status(None) == "none" and allowed_actions(None, Options()) == frozenset()


def test_a_discharge_to_the_grid_on_a_signed_target_needs_the_charge_step_too() -> None:
    """The house step is the plant's own mode: it shows nothing of the sign a discharge to the grid writes."""
    sell = Options(mode="max_profit")
    no_charge = _report(hold="obeyed", charge="ignored", discharge_house="obeyed", normal="restored")
    assert "export_discharge" in allowed_actions(no_charge, sell)
    assert allowed_actions(no_charge, sell, export_needs_charge=True) == {"hold", "discharge_house"}
    both = _report(hold="obeyed", charge="obeyed", discharge_house="obeyed", normal="restored")
    assert "export_discharge" in allowed_actions(both, sell, export_needs_charge=True)
    signed = {brand for brand in ("sigenergy", "solaredge", "huawei", "solax", "solinteg")
              if (control := control_for(brand)) is not None and control.export_signed_by_charge()}
    assert signed == {"solax", "solinteg"}, "one signed power target for both directions"


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


def test_a_run_for_normal_alone_runs_hold_again_so_that_something_is_handed_back() -> None:
    left = _report(hold="obeyed", charge="obeyed", discharge_house="obeyed", normal="skipped")
    assert modes_to_run(left) == ("hold",) and needs_run(left)
    assert modes_to_run(_report(hold="obeyed", charge="obeyed", discharge_house="obeyed", normal="restored")) == ()


def test_a_mode_left_pending_after_hold_and_normal_passed_is_tried_at_most_max_attempts_runs() -> None:
    modes = {"hold": "obeyed", "charge": "aborted:fuse_headroom", "discharge_house": "obeyed", "normal": "restored"}
    assert needs_run(_report(attempts=MAX_ATTEMPTS - 1, **modes))
    spent = _report(attempts=MAX_ATTEMPTS, **modes)
    assert report_status(spent) == "passed" and not needs_run(spent), "control runs on; the charge mode stays off"
    assert allowed_actions(spent, Options()) == {"hold", "discharge_house"}


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


def _cap(fuse_a: float | None = 25.0, *, cars_a: float = 0.0, peak: float | None = None, phases: int = 3,
         limit: float | None = None, acts: bool = False) -> float | None:
    return grid_charge_cap_kw(fuse_a, 400.0, margin_a=2.0, cars_a=cars_a, house_peak_kw=peak, plant_phases=phases,
                              import_limit_kw=limit, import_limit_acts=acts)


def test_the_grid_charge_cap_keeps_every_phase_under_the_fuse_with_home_assistant_down() -> None:
    volts = 400.0 / 3 ** 0.5
    fuse = fuse_kw(20.0, 400.0, 2.0)
    assert fuse == pytest.approx(12.47, abs=0.01)
    # 25 A: the default 4 kW house peak all on one phase (17.3 A) leaves 5.7 A on it; a balanced three-phase
    # charge may take 5.7 A on each phase.
    cap = _cap()
    assert cap == pytest.approx((25.0 - 2.0 - HOUSE_PEAK_DEFAULT_KW * 1000 / volts) * 3 * volts / 1000)
    assert round(cap, 2) == 3.93
    # On the worst phase with HA down: the house's peak + the battery's third stays under the fuse less the margin.
    assert HOUSE_PEAK_DEFAULT_KW * 1000 / volts + cap * 1000 / 3 / volts == pytest.approx(23.0)
    # A quiet hour's p90 never lowers the peak below the default; a larger one raises it.
    assert _cap(peak=0.6) == pytest.approx(cap)
    assert _cap(peak=5.0) < cap
    # The chargers' last currents count on that phase too.
    assert _cap(cars_a=3.0) == pytest.approx((25.0 - 2.0 - 3.0 - HOUSE_PEAK_DEFAULT_KW * 1000 / volts) * 3 * volts / 1000)
    assert _cap(cars_a=6.0) == 0.0
    # A single-phase plant puts its whole charge on one phase.
    assert _cap(phases=1) == pytest.approx(cap / 3)
    # 20 A with the default peak leaves no grid charge worth running.
    assert _cap(20.0) < MIN_RATE_KW
    assert _cap(None) == 0.0, "an unknown fuse allows no grid charge"
    assert _cap(cars_a=float("inf")) == 0.0, "chargers that cannot be read allow none"


def test_a_discharge_to_the_grid_leaves_the_sun_s_export_under_the_fuse_on_the_worst_phase() -> None:
    volts = 400.0 / 3 ** 0.5

    def cap(exports, share=0.0, phases=3):
        return export_discharge_cap_kw(25.0, 400.0, margin_a=2.0, phase_export_a=exports, battery_share_a=share,
                                       plant_phases=phases)

    assert cap([0.0, 0.0, 0.0]) == pytest.approx(23.0 * 3 * volts / 1000)
    assert cap([15.0, 3.0, 0.0]) == pytest.approx(8.0 * 3 * volts / 1000)
    assert cap([15.0, 3.0, 0.0], phases=1) == pytest.approx(8.0 * volts / 1000)
    # SpotNav's own running discharge is not the sun's.
    assert cap([15.0, 15.0, 15.0], share=10.0) == pytest.approx(18.0 * 3 * volts / 1000)
    assert cap([24.0, 0.0, 0.0]) == 0.0
    assert cap(None) == 0.0 and export_discharge_cap_kw(None, 400.0, margin_a=2.0, phase_export_a=[0.0],
                                                        battery_share_a=0.0, plant_phases=3) == 0.0
    slot = ("export_discharge", 9.0, None)
    assert restricted(slot, {"export_discharge"}, None, 4.0) == (("export_discharge", 4.0, None), "export_fuse_cap")
    assert restricted(slot, {"export_discharge"}, None, 0.1) == (("self_consume", 0.0, None), "export_fuse_cap")


def test_the_import_limit_lifts_the_cap_only_while_it_keeps_the_worst_phase_under_the_fuse() -> None:
    volts = 400.0 / 3 ** 0.5
    # 25 A, nothing learned: 4 kW on one phase (17.3 A). The plant's limit is a total, so the most it may read is
    # 3 x 23 A less twice that peak: 34.4 A in all, 7.93 kW, not the fuse's 17.3 kW.
    most = import_limit_max_kw(25.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None)
    assert most == pytest.approx((3 * 23.0 - 2 * HOUSE_PEAK_DEFAULT_KW * 1000 / volts) * volts / 1000)
    assert round(most, 2) == 7.93
    assert _cap(acts=True, limit=7.9) is None
    assert _cap(acts=True, limit=most) is None
    assert _cap(acts=True, limit=8.0) == pytest.approx(_cap()), "a total that lets one phase pass the fuse guards nothing"
    assert _cap(acts=True, limit=fuse_kw(25.0, 400.0, 0.0)) == pytest.approx(_cap()), "the fuse as three-phase power is too much"
    assert _cap(acts=True, limit=None) == pytest.approx(_cap()), "a limit that reads nothing protects nothing"
    assert _cap(acts=False, limit=5.0) == pytest.approx(_cap()), "not shown to act on site"
    # The chargers' last currents count as one phase's load: 3 A leaves 6 A less in all.
    assert import_limit_max_kw(25.0, 400.0, margin_a=2.0, cars_a=3.0, house_peak_kw=None) == pytest.approx(
        most - 6.0 * volts / 1000
    )
    assert _cap(acts=True, limit=7.9, cars_a=3.0) == pytest.approx(_cap(cars_a=3.0))
    # A limit too small to leave any charge beside it is no rule at all; an unknown fuse has none.
    assert import_limit_holds(0.05, 20.0, 400.0, margin_a=2.0, cars_a=9.5) is False
    assert import_limit_max_kw(None, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None) is None
    assert import_limit_max_kw(25.0, 400.0, margin_a=2.0, cars_a=float("inf"), house_peak_kw=None) is None
    slot = ("grid_charge", 15.0, None)
    assert restricted(slot, {"grid_charge"}, 9.0) == (("grid_charge", 9.0, None), "grid_charge_cap")
    assert restricted(slot, {"grid_charge"}, 0.2) == (("self_consume", 0.0, None), "grid_charge_cap")
    assert restricted(slot, {"grid_charge"}, None) == (slot, None)


#: The owner's house (2026-11-18): the learned peaks on a 20 A fuse, L2 at 18 A.
UNBALANCED = {"L1": 13.6, "L2": 18.0, "L3": 17.4}


def _worst_phase_a(limit_kw: float, house_a: tuple[float, float, float], charge_kw: float = 20.0) -> float:
    """The most loaded phase (A) with Home Assistant down: the plant spreads the charge evenly and cuts it until the
    total import is at its limit."""
    volts = 400.0 / 3 ** 0.5
    room_a = max(limit_kw * 1000 / volts - sum(house_a), 0.0)
    charge_a = min(charge_kw * 1000 / volts, room_a) / 3
    return max(house_a) + charge_a


def _house_states(peaks: dict[str, float]) -> list[tuple[float, float, float]]:
    """Every phase quiet or at its peak, and the halves between."""
    steps = (0.0, 0.5, 1.0)
    values = list(peaks.values())
    return [(values[0] * a, values[1] * b, values[2] * c) for a in steps for b in steps for c in steps]


def test_an_unbalanced_house_refuses_an_import_limit_that_would_let_a_phase_pass_the_fuse() -> None:
    volts = 400.0 / 3 ** 0.5
    rule = {"margin_a": 2.0, "cars_a": 0.0, "house_peak_kw": None, "house_phase_peaks_a": UNBALANCED}
    most = import_limit_max_kw(20.0, 400.0, **rule)
    # 3 x 18 A less twice L2's 18 A: 18 A in all, 4.16 kW.
    assert most == pytest.approx(18.0 * volts / 1000)
    # The old rule (at most the fuse as three-phase power) and the form that takes every phase at its peak at once
    # both let L2 pass the fuse when it peaks alone: refused now.
    old_rule = fuse_kw(20.0, 400.0, 0.0)
    all_at_peak = (3 * 18.0 - sum(18.0 - peak for peak in UNBALANCED.values())) * volts / 1000
    assert round(all_at_peak, 1) == 11.3
    for limit in (old_rule, all_at_peak, most + 0.1):
        assert import_limit_holds(limit, 20.0, 400.0, **rule) is False
        assert _worst_phase_a(limit, (0.0, 18.0, 0.0)) > 20.0 - 2.0
        assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, import_limit_kw=limit, import_limit_acts=True,
                                  **rule) == 0.0
    assert _worst_phase_a(all_at_peak, (0.0, 18.0, 0.0)) == pytest.approx(18.0 + (49.0 - 18.0) / 3)  # 28.3 A
    # The largest limit the rule accepts keeps every phase at or under 18 A whatever the house does within its peaks.
    assert import_limit_holds(most, 20.0, 400.0, **rule) is True
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, import_limit_kw=most, import_limit_acts=True, **rule) is None
    for house in _house_states(UNBALANCED):
        assert _worst_phase_a(most, house) <= 18.0 + 1e-9, house
    assert _worst_phase_a(most, (0.0, 18.0, 0.0)) == pytest.approx(18.0), "L2 alone at its peak: no charge is left"
    assert _worst_phase_a(most, (0.0, 0.0, 0.0)) == pytest.approx(6.0), "a quiet house: 4.16 kW of charge"
    # Until the peaks are learned: 4 kW on one phase, 19.4 A in all.
    cautious = import_limit_max_kw(20.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None)
    assert cautious == pytest.approx((54.0 - 2 * 4000 / volts) * volts / 1000)
    assert round(cautious, 2) == 4.47
    for house_a in ((4000 / volts, 0.0, 0.0), (0.0, 4000 / volts, 0.0), (2.0, 4000 / volts, 5.0)):
        assert _worst_phase_a(cautious, house_a) <= 18.0 + 1e-9
    # A colder hour's forecast counts: its p90 spread over the phases is the least any phase is taken at.
    assert import_limit_max_kw(20.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=13.0,
                               house_phase_peaks_a=UNBALANCED) < most


def test_the_per_phase_caps_leave_each_phase_its_own_room_and_an_unbalanced_house_never_passes_the_fuse() -> None:
    volts = 400.0 / 3 ** 0.5
    caps = grid_charge_phase_caps_kw(20.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None,
                                     house_phase_peaks_a=UNBALANCED)
    # L1 has 4.4 A of room, L2 none, L3 0.6 A.
    assert caps == pytest.approx((4.4 * volts / 1000, 0.0, 0.6 * volts / 1000))
    assert [round(cap, 2) for cap in caps] == [1.02, 0.0, 0.14]
    # Home Assistant stops with the house at its learned peak on every phase: each phase at most 18 A of 20 A.
    for peak, cap in zip(UNBALANCED.values(), caps, strict=True):
        assert peak + cap * 1000 / volts <= 20.0 - 2.0 + 1e-9
    assert UNBALANCED["L2"] + caps[1] * 1000 / volts == pytest.approx(18.0)
    # The balanced charge of the same house gets nothing: L2 is the house on every phase.
    assert grid_charge_cap_kw(20.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None, plant_phases=3,
                              house_phase_peaks_a=UNBALANCED) == 0.0
    # Never less than the balanced cap in all, and the same for a balanced house.
    even = {"L1": 9.0, "L2": 9.0, "L3": 9.0}
    assert sum(grid_charge_phase_caps_kw(25.0, 400.0, margin_a=2.0, cars_a=3.0, house_peak_kw=None,
                                         house_phase_peaks_a=even)) == pytest.approx(
        grid_charge_cap_kw(25.0, 400.0, margin_a=2.0, cars_a=3.0, house_peak_kw=None, plant_phases=3,
                           house_phase_peaks_a=even))
    # The chargers' last currents count on every phase; a p90 above a phase's learned peak raises that phase.
    with_car = grid_charge_phase_caps_kw(20.0, 400.0, margin_a=2.0, cars_a=4.0, house_peak_kw=None,
                                         house_phase_peaks_a=UNBALANCED)
    assert with_car == pytest.approx((0.4 * volts / 1000, 0.0, 0.0))
    cold = grid_charge_phase_caps_kw(20.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=10.5,
                                     house_phase_peaks_a=UNBALANCED)
    assert cold[0] == pytest.approx((18.0 - 10500 / 3 / volts) * volts / 1000) and cold[0] < caps[0]
    # Nothing learned: 4 kW may be on any phase, so each has 0.7 A; the fuse or the chargers unknown: nothing.
    cautious = grid_charge_phase_caps_kw(20.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None)
    assert cautious == pytest.approx(((18.0 - 4000 / volts) * volts / 1000,) * 3) and sum(cautious) < MIN_RATE_KW
    assert grid_charge_phase_caps_kw(None, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None) == (0.0, 0.0, 0.0)
    assert grid_charge_phase_caps_kw(20.0, 400.0, margin_a=2.0, cars_a=float("inf"), house_peak_kw=None) == (0.0,) * 3
    # A phase missing among the learned peaks is never a phase without load.
    assert grid_charge_phase_caps_kw(20.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None,
                                     house_phase_peaks_a={"L1": 1.0, "L3": 1.0}) == pytest.approx(cautious)


def test_a_charge_is_spread_as_evenly_as_each_phase_s_cap_allows() -> None:
    assert phase_split_kw(3.0, (2.0, 2.0, 2.0)) == pytest.approx((1.0, 1.0, 1.0))
    assert phase_split_kw(3.0, (1.02, 0.0, 0.14)) == pytest.approx((1.02, 0.0, 0.14)), "the caps are all there is"
    assert phase_split_kw(1.0, (1.02, 0.0, 0.14)) == pytest.approx((0.86, 0.0, 0.14))
    assert phase_split_kw(3.0, (0.5, 4.0, 4.0)) == pytest.approx((0.5, 1.25, 1.25))
    assert phase_split_kw(0.0, (2.0, 2.0, 2.0)) == (0.0, 0.0, 0.0)
    for total, caps in ((2.5, (1.0, 0.2, 3.0)), (9.0, (1.0, 0.2, 3.0)), (0.3, (0.0, 0.0, 5.0))):
        split = phase_split_kw(total, caps)
        assert all(part <= cap + 1e-9 for part, cap in zip(split, caps, strict=True))
        assert sum(split) == pytest.approx(min(total, sum(caps)))


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
    assert trial_refusal("export_discharge", Options(mode="max_profit")) is None
    assert trial_refusal("hold", Options()) is None and trial_refusal("curtail_export", Options()) is None
    assert trial_refusal("explode", Options()) == "unknown_action"


def test_the_car_backs_off_first_when_its_departure_has_room() -> None:
    assert departure_has_room(NOW + timedelta(hours=8), NOW, 10.0, 7.0)
    assert not departure_has_room(NOW + timedelta(hours=3), NOW, 10.0, 7.0)
    assert not departure_has_room(None, NOW, 10.0, 7.0)
    assert battery_yields(True, False) is True
    assert battery_yields(True, True) is False
    assert battery_yields(False, False) is False, "battery first: the site's load balancing backs the car off"


def test_the_mode_decides_selling_and_a_stored_legacy_sell_option_is_only_the_migration_source() -> None:
    from custom_components.spotnav.battery import gate as g

    assert g.Options.from_config({}).mode == "balanced" and not g.Options.from_config({}).allow_sell
    for mode in ("self_consumption", "balanced", "max_profit"):
        assert g.Options.from_config({g.CONF_MODE: mode}).mode == mode
    assert g.Options.from_config({g.CONF_MODE: "export"}).mode == "balanced"
    assert g.Options.from_config({g.CONF_MODE: 3}).mode == "balanced"
    assert g.legacy_mode({g.CONF_ALLOW_SELL: True}) == "max_profit"
    assert g.legacy_mode({g.CONF_ALLOW_SELL: False}) is None and g.legacy_mode({}) is None
    assert g.legacy_mode({g.CONF_ALLOW_SELL: True, g.CONF_MODE: "balanced"}) is None, "a chosen mode wins"
    assert g.Options.from_config({g.CONF_ALLOW_SELL: True}).mode == "max_profit"
    assert g.Options.from_config({g.CONF_ALLOW_SELL: True, g.CONF_MODE: "balanced"}).mode == "balanced"
    assert g.valid_change("battery_mode", "max_profit") and not g.valid_change("battery_mode", "x")
    assert not g.valid_change(g.CONF_ALLOW_SELL, True)
    every = _report(hold="obeyed", charge="obeyed", discharge_house="obeyed", normal="restored")
    for mode in ("self_consumption", "balanced"):
        assert "export_discharge" not in g.allowed_actions(every, g.Options(mode=mode))
    assert g.CONF_ALLOW_SELL not in g.WRITABLE and g.CONF_MODE in g.WRITABLE
