"""The battery control's gate (`battery/gate.py`): the site's options, the verification report per firmware, the
actions the report allows, when the routine may run, the grid charge cap and the fast shedding order."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.spotnav.battery.commands import control_for
from custom_components.spotnav.core.electric import fuse_kw
from custom_components.spotnav.battery.gate import (
    CONF_ACCEPT_HA_DOWN,
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
    grid_charge_cap_kw,
    grid_charge_phase_caps_kw,
    ha_down_accepted_cap_kw,
    import_limit_holds,
    import_limit_max_kw,
    import_limit_phase_holds,
    import_limit_phase_max_kw,
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
    valid_change,
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
    assert allowed_actions(passed, Options(mode="self_consumption")) == {"hold", "grid_charge", "discharge_house"}
    assert "export_discharge" in allowed_actions(passed, Options(mode="max_profit")), "selling only when allowed"
    assert "export_discharge" in allowed_actions(passed, Options()), "Balanced (the default) sells through its gate"
    no_house = _report(hold="obeyed", charge="obeyed", discharge_house="ignored", normal="restored")
    assert allowed_actions(no_house, Options()) == {"hold", "grid_charge"}, "selling needs the house step"
    only_hold = _report(hold="obeyed", charge="ignored", discharge_house="not_judgeable:x", normal="restored")
    assert report_status(only_hold) == "passed" and allowed_actions(only_hold, Options()) == {"hold"}
    assert modes_to_run(only_hold) == ("discharge_house",) and needs_run(only_hold)
    failed = _report(hold="ignored", charge="obeyed", normal="restored")
    assert report_status(failed) == "failed" and allowed_actions(failed, Options()) == frozenset()
    assert failed_mode(failed) == "hold" and not needs_run(failed)
    assert report_status(None) == "none" and allowed_actions(None, Options()) == frozenset()


def test_a_discharge_to_the_grid_on_a_signed_target_needs_the_charge_step_too() -> None:
    """The house step is the plant's own mode: it shows nothing of the sign a discharge to the grid writes."""
    for sell in (Options(mode="max_profit"), Options(mode="balanced")):
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
    assert allowed_actions(spent, Options(mode="self_consumption")) == {"hold", "discharge_house"}


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


def _phase_held_a(limit_kw: float, house_a: float, cars_a: float, out_a: float = 0.0) -> float:
    """A phase under a plant that holds each phase at a third of its limit: at L / 3 while the house and the cars
    leave room there (the charge fills it up to that), beyond that the house and the cars less what the plant puts
    out there (`out_a`; it charges nothing on that phase)."""
    volts = 400.0 / 3 ** 0.5
    return max(limit_kw * 1000.0 / volts / 3.0, house_a + cars_a - out_a)


def test_a_limit_held_per_phase_guards_up_to_three_times_the_fuse_less_the_margin() -> None:
    volts = 400.0 / 3 ** 0.5
    rule = {"margin_a": 2.0, "cars_a": 0.0}
    most = import_limit_phase_max_kw(20.0, 400.0, **rule)
    # The owner's 20 A fuse: 3 x 18 A x 231 V = 12.47 kW; the 13.8 kW found on the plant is the fuse itself.
    assert most == pytest.approx(3 * 18.0 * volts / 1000) and round(most, 2) == 12.47
    assert import_limit_phase_holds(12.4, 20.0, 400.0, **rule) is True
    assert import_limit_phase_holds(most, 20.0, 400.0, **rule) is True
    assert import_limit_phase_holds(13.8, 20.0, 400.0, **rule) is False
    # Every phase stays under the fuse less the margin, whatever the house does within its peaks.
    for house_a in ((4000 / volts, 0.0, 0.0), (0.0, 4000 / volts, 0.0), (2.0, 4000 / volts, 5.0)):
        assert max(_phase_held_a(most, h, 0.0) for h in house_a) <= 18.0 + 1e-9
    # The total-only rule leaves a third of that on the same fuse (4.47 kW).
    assert import_limit_max_kw(20.0, 400.0, house_peak_kw=None, **rule) < most / 2


def test_a_limit_held_per_phase_is_not_lowered_by_the_house_or_the_cars() -> None:
    volts = 400.0 / 3 ** 0.5
    most = 3 * 18.0 * volts / 1000
    # The cautious house (4 kW on one phase, 17.3 A) and a car's last 6 A: 23.3 A on that phase. The battery's charge
    # adds nothing there (the plant charges on the other phases), so the phase carries what it would without a
    # battery; the limit still guards up to three times the fuse less the margin.
    assert import_limit_phase_max_kw(20.0, 400.0, margin_a=2.0, cars_a=6.0) == pytest.approx(most)
    assert import_limit_phase_holds(12.4, 20.0, 400.0, margin_a=2.0, cars_a=6.0) is True
    assert _phase_held_a(most, 4000 / volts, 6.0) == pytest.approx(4000 / volts + 6.0), "the house and the car alone"
    # Learned peaks above the fuse less the margin on a phase do not bring the cap back either.
    for peaks in ({"L1": 10.0, "L2": 18.5, "L3": 3.0}, {"L1": 10.0, "L2": 17.5, "L3": 3.0}):
        assert grid_charge_cap_kw(20.0, 400.0, margin_a=2.0, cars_a=6.0, house_peak_kw=None, plant_phases=3,
                                  house_phase_peaks_a=peaks, import_limit_kw=12.4, import_limit_per_phase=True) is None
    # Never more than the house and the cars alone on a phase, never more than the fuse on one they leave room on.
    for house_a in (0.0, 5.0, 10.0, 17.9, 18.0, 25.0):
        for cars_a in (0.0, 6.0, 16.0):
            assert _phase_held_a(most, house_a, cars_a) <= max(18.0, house_a + cars_a) + 1e-9


def test_the_owners_site_holds_with_the_limit_at_the_fuse_and_the_halo_at_16_a() -> None:
    volts = 400.0 / 3 ** 0.5
    # 20 A fuse, the site's margin 0 A, learned peaks 15.7 / 18.0 / 18.4 A, the HALO allowed 16 A: 34.4 A on L3 from
    # the house and the car alone, which the battery adds nothing to.
    peaks = {"L1": 15.7, "L2": 18.0, "L3": 18.4}
    rule = {"margin_a": 0.0, "cars_a": 16.0}
    cap = {"margin_a": 2.0, "cars_a": 16.0, "house_peak_kw": None, "house_phase_peaks_a": peaks, "plant_phases": 3,
           "import_limit_per_phase": True, "phase_margin_a": 0.0}
    most = import_limit_phase_max_kw(20.0, 400.0, **rule)
    assert most == pytest.approx(3 * 20.0 * volts / 1000) and round(most, 2) == 13.86
    assert import_limit_phase_holds(13.8, 20.0, 400.0, **rule) is True
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=13.8, **cap) is None
    # Condition (1) still refuses a limit above three times the fuse less the margin.
    assert import_limit_phase_holds(14.0, 20.0, 400.0, **rule) is False
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=14.0, **cap) == 0.0
    assert import_limit_phase_holds(13.8, 20.0, 400.0, **{**rule, "margin_a": 1.0}) is False
    # The chargers' currents must still read.
    assert import_limit_phase_max_kw(20.0, 400.0, **{**rule, "cars_a": float("nan")}) is None
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=13.8, **{**cap, "cars_a": float("nan")}) == 0.0


@pytest.mark.parametrize("limit", [None, float("nan"), float("inf"), -1.0, 4294967.295, True])
def test_a_limit_held_per_phase_that_reads_nothing_or_no_limit_guards_nothing(limit) -> None:
    assert import_limit_phase_holds(limit, 20.0, 400.0, margin_a=2.0) is False


def test_a_limit_held_per_phase_guards_nothing_with_the_fuse_or_the_chargers_unknown() -> None:
    assert import_limit_phase_max_kw(None, 400.0, margin_a=2.0, cars_a=0.0) is None
    assert import_limit_phase_max_kw(20.0, 400.0, margin_a=2.0, cars_a=float("nan")) is None
    assert import_limit_phase_max_kw(2.0, 400.0, margin_a=2.0, cars_a=0.0) is None


def test_the_cap_is_lifted_by_a_limit_held_per_phase_only_where_the_shell_found_every_condition() -> None:
    rule = {"margin_a": 2.0, "cars_a": 0.0, "house_peak_kw": None, "plant_phases": 3}
    capped = grid_charge_cap_kw(20.0, 400.0, **rule)
    assert capped is not None and capped < MIN_RATE_KW, "the 20 A fuse leaves no grid charge under the cap"
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=12.4, import_limit_per_phase=True, **rule) is None
    # The shell's conditions not met (the fact unrecorded, the switch off, the sensor offline): today's rules.
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=12.4, import_limit_per_phase=False, **rule) == capped
    # Too high a limit: today's rules, the total rule too where the person said the limit acts.
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=13.8, import_limit_per_phase=True, **rule) == capped
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=4.4, import_limit_per_phase=True,
                              import_limit_acts=True, **rule) is None
    # The per-phase rule's own margin (the site's): 0 A lets the fuse itself guard, the total rule keeps `margin_a`.
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=13.8, import_limit_per_phase=True, phase_margin_a=0.0,
                              **rule) is None
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=13.8, import_limit_per_phase=True, phase_margin_a=1.0,
                              **rule) == capped
    # A car's last current does not bring the cap back: the battery adds nothing on the phase it loads.
    car = {**rule, "cars_a": 6.0}
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=12.4, import_limit_per_phase=True, **car) is None
    assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=13.8, import_limit_per_phase=True, **car) == 0.0


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


def test_selling_from_the_battery_follows_the_mode_also_for_the_test_action() -> None:
    assert trial_refusal("export_discharge", Options(mode="self_consumption")) == "sell_not_allowed"
    assert trial_refusal("export_discharge", Options()) is None, "Balanced sells through its gate"
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

    assert g.Options.from_config({}).mode == "balanced" and g.Options.from_config({}).allow_sell
    assert not g.Options.from_config({g.CONF_MODE: "self_consumption"}).allow_sell
    for mode in ("self_consumption", "balanced", "max_profit"):
        assert g.Options.from_config({g.CONF_MODE: mode}).mode == mode
    assert g.Options.from_config({g.CONF_MODE: "export"}).mode == "balanced"
    assert g.Options.from_config({g.CONF_MODE: 3}).mode == "balanced"
    assert g.legacy_mode({g.CONF_ALLOW_SELL: True}) == "max_profit"
    assert g.legacy_mode({g.CONF_ALLOW_SELL: False}) is None and g.legacy_mode({}) is None
    assert g.legacy_mode({g.CONF_ALLOW_SELL: True, g.CONF_MODE: "balanced"}) is None, "a chosen mode wins"
    assert g.Options.from_config({g.CONF_ALLOW_SELL: True}).mode == "balanced", "migrated at load, not read here"
    assert g.Options.from_config({g.CONF_ALLOW_SELL: True, g.CONF_MODE: "balanced"}).mode == "balanced"
    assert g.valid_change("battery_mode", "max_profit") and not g.valid_change("battery_mode", "x")
    assert not g.valid_change(g.CONF_ALLOW_SELL, True)
    every = _report(hold="obeyed", charge="obeyed", discharge_house="obeyed", normal="restored")
    assert "export_discharge" not in g.allowed_actions(every, g.Options(mode="self_consumption"))
    for mode in ("balanced", "max_profit"):
        assert "export_discharge" in g.allowed_actions(every, g.Options(mode=mode))
    assert g.CONF_ALLOW_SELL not in g.WRITABLE and g.CONF_MODE in g.WRITABLE


def test_the_ha_down_acceptance_is_an_option_off_by_default_written_as_a_boolean() -> None:
    assert Options().accept_ha_down is False and Options.from_config({}).accept_ha_down is False
    assert Options.from_config({CONF_ACCEPT_HA_DOWN: True}).accept_ha_down is True
    assert Options.from_config({CONF_ACCEPT_HA_DOWN: "yes"}).accept_ha_down is False
    assert Options(accept_ha_down=True).as_dict()[CONF_ACCEPT_HA_DOWN] is True
    assert valid_change(CONF_ACCEPT_HA_DOWN, True) and not valid_change(CONF_ACCEPT_HA_DOWN, 1)


def test_the_wear_is_a_number_from_zero_to_ten_times_the_currency_s_default_or_null_for_the_default() -> None:
    from custom_components.spotnav.battery import gate as g

    assert g.CONF_WEAR in g.WRITABLE
    assert g.valid_change(g.CONF_WEAR, None) and g.valid_change(g.CONF_WEAR, 0) and g.valid_change(g.CONF_WEAR, 150)
    assert not g.valid_change(g.CONF_WEAR, 150.5), "ten times the SEK figure when no currency is told"
    assert g.valid_change(g.CONF_WEAR, 14, wear_default=1.4) and not g.valid_change(g.CONF_WEAR, 14.1, wear_default=1.4)
    for bad in (-0.1, True, "15", float("nan"), float("inf"), [15]):
        assert not g.valid_change(g.CONF_WEAR, bad), bad
    assert Options.from_config({}).wear_minor_per_kwh is None
    assert Options.from_config({g.CONF_WEAR: 20}).wear_minor_per_kwh == 20.0
    assert Options.from_config({g.CONF_WEAR: -3}).wear_minor_per_kwh is None
    assert Options.from_config({g.CONF_WEAR: True}).wear_minor_per_kwh is None
    assert Options().as_dict()[g.CONF_WEAR] is None
    assert g.site_wear({}, "SEK") == (15.0, False) and g.site_wear({}, "EUR") == (1.4, False)
    assert g.site_wear({}, "XYZ") == (15.0, True), "the SEK figure stands in, marked"
    assert g.site_wear({g.CONF_WEAR: 3}, "XYZ") == (3.0, False), "the site's own needs no currency"
    assert g.site_wear({g.CONF_WEAR: 0}, "EUR") == (0.0, False)


def test_the_accepted_ha_down_risk_caps_a_grid_charge_at_the_fuse_of_an_empty_house() -> None:
    volts = 400.0 / 3 ** 0.5
    rule = {"margin_a": 2.0, "cars_a": 0.0, "house_peak_kw": None, "plant_phases": 3}
    off = grid_charge_cap_kw(20.0, 400.0, **rule)
    assert off is not None and off < MIN_RATE_KW, "off: the HA-down cap as before"
    assert grid_charge_cap_kw(20.0, 400.0, accept_ha_down=False, **rule) == off
    # On: each phase's fuse less the site's own margin, the house's peak and the chargers not counted.
    on = grid_charge_cap_kw(20.0, 400.0, accept_ha_down=True, phase_margin_a=1.0, **rule)
    assert on == pytest.approx(3 * 19.0 * volts / 1000) and round(on, 2) == 13.16
    loaded = {**rule, "cars_a": 10.0, "house_peak_kw": 9.0}
    assert grid_charge_cap_kw(20.0, 400.0, accept_ha_down=True, phase_margin_a=1.0, house_phase_peaks_a=UNBALANCED,
                              **loaded) == pytest.approx(on)
    # The site's margin of 0 A: the fuse itself; no site margin given: the caller's margin.
    assert grid_charge_cap_kw(20.0, 400.0, accept_ha_down=True, phase_margin_a=0.0, **rule) == pytest.approx(
        3 * 20.0 * volts / 1000)
    assert grid_charge_cap_kw(20.0, 400.0, accept_ha_down=True, **rule) == pytest.approx(3 * 18.0 * volts / 1000)
    # A plant whose charge may all be on one phase: one phase's room, never three.
    one = {**rule, "plant_phases": 1}
    assert grid_charge_cap_kw(20.0, 400.0, accept_ha_down=True, phase_margin_a=1.0, **one) == pytest.approx(
        19.0 * volts / 1000)
    # The fuse or the chargers unknown: no grid charge, accepted or not.
    assert grid_charge_cap_kw(None, 400.0, accept_ha_down=True, phase_margin_a=1.0, **rule) == 0.0
    assert grid_charge_cap_kw(20.0, 400.0, accept_ha_down=True, phase_margin_a=1.0,
                              **{**rule, "cars_a": float("nan")}) == 0.0
    assert ha_down_accepted_cap_kw(None, 400.0, margin_a=1.0, plant_phases=3) == 0.0
    assert ha_down_accepted_cap_kw(1.0, 400.0, margin_a=2.0, plant_phases=3) == 0.0


def test_the_accepted_ha_down_risk_changes_nothing_where_the_plant_s_import_limit_guards() -> None:
    rule = {"margin_a": 2.0, "cars_a": 0.0, "house_peak_kw": None, "plant_phases": 3}
    for accept in (False, True):
        assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=12.4, import_limit_per_phase=True,
                                  accept_ha_down=accept, phase_margin_a=1.0, **rule) is None
        assert grid_charge_cap_kw(20.0, 400.0, import_limit_kw=4.4, import_limit_acts=True,
                                  accept_ha_down=accept, phase_margin_a=1.0, **rule) is None


def test_the_accepted_ha_down_risk_gives_each_phase_the_fuse_less_the_site_s_margin_after_a_recipe_s_dead_man() -> None:
    volts = 400.0 / 3 ** 0.5
    base = {"margin_a": 2.0, "cars_a": 4.0, "house_peak_kw": None, "house_phase_peaks_a": UNBALANCED}
    assert grid_charge_phase_caps_kw(20.0, 400.0, accept_ha_down=False, site_margin_a=1.0, **base) == (
        grid_charge_phase_caps_kw(20.0, 400.0, **base))
    assert grid_charge_phase_caps_kw(20.0, 400.0, accept_ha_down=True, site_margin_a=1.0, **base) == pytest.approx(
        (19.0 * volts / 1000,) * 3)
    # A recipe's own dead-man comes first: its own rule (the caller's margin), the acceptance not needed.
    assert grid_charge_phase_caps_kw(20.0, 400.0, expiry_lifts=True, accept_ha_down=True, site_margin_a=0.0,
                                     **base) == pytest.approx((18.0 * volts / 1000,) * 3)
    assert grid_charge_phase_caps_kw(None, 400.0, accept_ha_down=True, site_margin_a=1.0, **base) == (0.0, 0.0, 0.0)


# --- a command's own expiry (`expiry_lifts`) ------------------------------------------------------------------------

_KEY = "fw1 | brand 1.0"


def _expiry(kind: str, renewer: str, window: float | None, *, evidence: str = "measured", **kwargs: Any) -> Any:
    from custom_components.spotnav.battery.commands import Expiry  # noqa: PLC0415

    roles = ("r",) if renewer == "spotnav" and kind in ("device_deadman", "device_lease", "soc_bound") else ()
    if roles and kind != "soc_bound" and window is not None and window <= 900:
        kwargs.setdefault("renew_s", 20)
    return Expiry(kind, renewer, window, roles, evidence=evidence,  # type: ignore[arg-type]
                  measured_on=frozenset({_KEY}) if evidence == "measured" else frozenset(), **kwargs)


@pytest.mark.parametrize(
    ("kind", "renewer", "window", "evidence", "slow", "fixed", "lifts"),
    [
        ("none", "nobody", None, "stated", False, False, False),
        ("device_deadman", "spotnav", 60.0, "measured", False, False, True),
        ("device_deadman", "spotnav", 30.0, "measured", False, False, True),
        ("device_deadman", "spotnav", 61.0, "measured", False, False, False),
        ("device_deadman", "spotnav", 1800.0, "measured", False, False, False),
        ("device_deadman", "spotnav", 60.0, "stated", False, False, False),
        # An integration's own renewal loop never lifts, even measured (it keeps going while SpotNav stalls).
        ("device_deadman", "integration", 30.0, "measured", False, False, False),
        ("device_lease", "spotnav", 60.0, "measured", False, False, True),
        ("device_lease", "spotnav", 60.0, "stated", False, False, False),
        ("device_lease", "spotnav", 1800.0, "measured", False, False, False),
        ("device_lease", "nobody", 60.0, "measured", False, False, False),
        ("device_lease", "integration", 60.0, "measured", False, False, False),
        # A SoC bound or a clock window is never a fuse argument.
        ("soc_bound", "spotnav", 60.0, "measured", False, False, False),
        ("clock_window", "nobody", 60.0, "measured", False, False, False),
        # A slow control or one whose charge SpotNav cannot set: the live clamp cannot lower it in seconds.
        ("device_deadman", "spotnav", 60.0, "measured", True, False, False),
        ("device_lease", "spotnav", 60.0, "measured", False, True, False),
    ],
)
def test_only_a_measured_expiry_spotnav_renews_within_the_minute_lifts_the_ha_down_cap(
    kind: str, renewer: str, window: float | None, evidence: str, slow: bool, fixed: bool, lifts: bool,
) -> None:
    from custom_components.spotnav.battery.gate import expiry_lifts, expiry_measured  # noqa: PLC0415

    expiry = _expiry(kind, renewer, window, evidence=evidence)
    measured = expiry_measured(expiry, _KEY)
    assert measured == (evidence == "measured")
    assert expiry_lifts(expiry, measured=measured, slow=slow, fixed_charge=fixed) is lifts
    # Measured on another firmware, or the firmware not read: never.
    assert not expiry_lifts(expiry, measured=expiry_measured(expiry, "fw2 | brand 1.0"), slow=slow, fixed_charge=fixed)
    assert not expiry_measured(expiry, None) and not expiry_measured(expiry, "unknown | brand 1.0")


def test_the_expiry_lift_comes_after_both_import_limit_rules_and_the_fuse_and_before_the_acceptance() -> None:
    volts = 400.0 / 3 ** 0.5
    base = {"margin_a": 2.0, "cars_a": 10.0, "house_peak_kw": 3.0, "house_phase_peaks_a": UNBALANCED}
    plain = grid_charge_cap_kw(20.0, 400.0, plant_phases=3, **base)
    lifted = grid_charge_cap_kw(20.0, 400.0, plant_phases=3, expiry_lifts=True, **base)
    assert lifted == pytest.approx(18.0 * 3 * volts / 1000) and plain is not None and plain < lifted
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=1, expiry_lifts=True, **base) == pytest.approx(
        18.0 * volts / 1000)
    # Before the acceptance (the caller's margin, not the site's).
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, expiry_lifts=True, accept_ha_down=True, phase_margin_a=0.0,
                              **base) == pytest.approx(lifted)
    # The fuse unknown: no grid charge, lifted or not.
    assert grid_charge_cap_kw(None, 400.0, plant_phases=3, expiry_lifts=True, **base) == 0.0
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, expiry_lifts=True, **{**base, "cars_a": float("nan")}) == 0.0
    # Either import-limit rule that holds comes first: no cap of this kind.
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, expiry_lifts=True, import_limit_kw=4.0,
                              import_limit_per_phase=True, phase_margin_a=1.0, **base) is None
    # Off: as before.
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, expiry_lifts=False, **base) == plain


def test_a_phase_lift_needs_the_fuse_known() -> None:
    base = {"margin_a": 2.0, "cars_a": 0.0, "house_peak_kw": None}
    assert grid_charge_phase_caps_kw(None, 400.0, expiry_lifts=True, **base) == (0.0, 0.0, 0.0)
    assert grid_charge_phase_caps_kw(20.0, 400.0, expiry_lifts=True, **{**base, "cars_a": float("nan")}) == (
        0.0, 0.0, 0.0)


def test_the_energy_a_grid_charge_may_take_after_home_assistant_dies() -> None:
    from custom_components.spotnav.battery.commands import NO_EXPIRY, Expiry  # noqa: PLC0415
    from custom_components.spotnav.battery.gate import expiry_energy_kwh, expiry_note  # noqa: PLC0415

    deadman = Expiry("device_deadman", "spotnav", 60.0, ("r",), renew_s=20)
    lease = Expiry("device_lease", "spotnav", 1800.0, ("forcible",))
    block = Expiry("soc_bound", "spotnav", 1800.0, ("reserve",))
    window = Expiry("clock_window", "nobody", 900.0)
    looped = Expiry("device_deadman", "integration", 30.0, integration="ecoflow")
    assert expiry_energy_kwh(deadman, 6.0) == pytest.approx(0.1)
    assert expiry_energy_kwh(lease, 3.0) == pytest.approx(1.5)
    assert expiry_energy_kwh(block, 5.0) == pytest.approx(2.5)
    assert expiry_energy_kwh(window, 4.0, slot_left_s=450.0) == pytest.approx(0.5)
    assert expiry_energy_kwh(window, 4.0, slot_left_s=5000.0) == pytest.approx(1.0)
    # Unbounded: no expiry, a window whose end is not known, an integration's own loop, a rate not read.
    assert expiry_energy_kwh(NO_EXPIRY, 4.0) is None and expiry_energy_kwh(window, 4.0) is None
    assert expiry_energy_kwh(looped, 4.0) is None and expiry_energy_kwh(lease, float("nan")) is None
    assert expiry_note(deadman, measured=False) == "dead-man stated, not measured"
    assert expiry_note(deadman, measured=True) is None
    assert expiry_note(looped, measured=True) == "continues while HA runs"
    assert expiry_note(block, measured=False) == "charges at most one block"
    assert expiry_note(lease, measured=False) is None and expiry_note(NO_EXPIRY, measured=False) is None


def test_an_expiry_row_counts_only_for_its_own_expiry_and_never_decides_the_report() -> None:
    from custom_components.spotnav.battery.gate import (  # noqa: PLC0415
        MODE_EXPIRY,
        expiry_due,
        expiry_measured,
        merge_report,
        report_status,
    )

    phase = _expiry("device_deadman", "spotnav", 60.0, evidence="stated", renew_s=20)
    other = _expiry("device_lease", "spotnav", 60.0, evidence="stated", renew_s=20)
    now = datetime(2026, 11, 23, 20, 0, tzinfo=timezone.utc)
    passed = {"hold": {"outcome": "obeyed"}, "normal": {"outcome": "restored"}}
    run = {"started_at": now.isoformat(),
           "modes": {MODE_EXPIRY: {"outcome": "obeyed", "expiry_id": phase.expiry_id, "fallback_s": 45.0}}}
    once = merge_report({"modes": passed}, run, key=_KEY, attempts=2, now=now)
    # One passing run: pending, tried again; two: measured.
    assert once["modes"][MODE_EXPIRY]["outcome"] == "obeyed_once" and once["modes"][MODE_EXPIRY]["passes"] == 1
    assert not expiry_measured(phase, _KEY, once) and expiry_due(once, phase)
    report = merge_report(once, run, key=_KEY, attempts=3, now=now)
    assert report["modes"][MODE_EXPIRY]["passes"] == 2
    assert expiry_measured(phase, _KEY, report) and not expiry_measured(other, _KEY, report)
    assert not expiry_measured(phase, "fw2 | brand 1.0", report)
    assert not expiry_due(report, phase) and expiry_due(report, other)
    # Ignored: decided, never lifts, and the report still passes.
    failed = merge_report(
        {"modes": passed}, {"modes": {MODE_EXPIRY: {"outcome": "ignored", "expiry_id": phase.expiry_id}}},
        key=_KEY, attempts=2, now=now,
    )
    assert report_status(failed) == "passed" and not expiry_due(failed, phase)
    assert allowed_actions(failed, Options()) == allowed_actions({"modes": passed}, Options())
    # A pending outcome between two passes keeps the first; an ignored one starts again (and decides).
    late = {"modes": {MODE_EXPIRY: {"outcome": "not_judgeable:late_look", "expiry_id": phase.expiry_id}}}
    kept = merge_report(once, late, key=_KEY, attempts=3, now=now)
    assert kept["modes"][MODE_EXPIRY]["passes"] == 1
    assert expiry_measured(phase, _KEY, merge_report(kept, run, key=_KEY, attempts=4, now=now))
    ignored = {"modes": {MODE_EXPIRY: {"outcome": "ignored", "expiry_id": phase.expiry_id}}}
    reset = merge_report(once, ignored, key=_KEY, attempts=3, now=now)
    assert reset["modes"][MODE_EXPIRY]["passes"] == 0 and not expiry_due(reset, phase)
    # A run for another expiry (the recipe changed) replaces the decided row, its passes counted afresh.
    other_run = {"modes": {MODE_EXPIRY: {"outcome": "obeyed", "expiry_id": other.expiry_id, "fallback_s": 70.0}}}
    again = merge_report(failed, other_run, key=_KEY, attempts=3, now=now)
    assert not expiry_measured(other, _KEY, again)
    again = merge_report(again, other_run, key=_KEY, attempts=4, now=now)
    assert expiry_measured(other, _KEY, again) and not expiry_measured(phase, _KEY, again)


def test_a_plants_own_import_comes_off_every_ha_down_cap_but_never_off_the_import_limit_rules() -> None:
    volts = 400.0 / 3 ** 0.5
    base = {"margin_a": 2.0, "cars_a": 0.0, "house_peak_kw": None, "house_phase_peaks_a": {"L1": 4.0, "L2": 4.0,
                                                                                           "L3": 4.0}}
    plain = grid_charge_cap_kw(20.0, 400.0, plant_phases=3, **base)
    own = grid_charge_cap_kw(20.0, 400.0, plant_phases=3, own_import_a=3.0, **base)
    assert plain is not None and own == pytest.approx(plain - 3.0 * 3 * volts / 1000)
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, own_import_a=float("inf"), **base) == 0.0
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, own_import_a=float("nan"), **base) == 0.0
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, own_import_a=3.0, accept_ha_down=True, phase_margin_a=1.0,
                              **base) == pytest.approx(16.0 * 3 * volts / 1000)
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, own_import_a=3.0, expiry_lifts=True, **base) == (
        pytest.approx(15.0 * 3 * volts / 1000))
    assert ha_down_accepted_cap_kw(20.0, 400.0, margin_a=1.0, plant_phases=1, own_import_a=4.0) == pytest.approx(
        15.0 * volts / 1000)
    # The import-limit rules are the plant's own measure of everything it imports: unchanged.
    assert grid_charge_cap_kw(20.0, 400.0, plant_phases=3, own_import_a=3.0, import_limit_kw=4.0,
                              import_limit_per_phase=True, phase_margin_a=1.0, **base) is None
    phases = grid_charge_phase_caps_kw(20.0, 400.0, own_import_a=3.0, **base)
    assert phases == pytest.approx(tuple(cap - 3.0 * volts / 1000 for cap in grid_charge_phase_caps_kw(
        20.0, 400.0, **base)))
    assert grid_charge_phase_caps_kw(20.0, 400.0, own_import_a=float("inf"), expiry_lifts=True, **base) == (
        0.0, 0.0, 0.0)


def test_a_phase_dead_man_lifts_only_the_phases_it_was_proven_on() -> None:
    volts = 400.0 / 3 ** 0.5
    base = {"margin_a": 2.0, "cars_a": 0.0, "house_peak_kw": None, "house_phase_peaks_a": UNBALANCED}
    kept = grid_charge_phase_caps_kw(20.0, 400.0, **base)
    lifted = 18.0 * volts / 1000
    l1 = grid_charge_phase_caps_kw(20.0, 400.0, expiry_lifts=(True, False, False), **base)
    assert l1[0] == pytest.approx(lifted) and l1[1:] == pytest.approx(kept[1:])
    assert grid_charge_phase_caps_kw(20.0, 400.0, expiry_lifts=(True, True, True), **base) == pytest.approx(
        (lifted,) * 3)
    assert grid_charge_phase_caps_kw(20.0, 400.0, expiry_lifts=True, **base) == pytest.approx((lifted,) * 3)
    # The unproven phases keep the accepted rule where the person accepted the HA-down risk.
    accepted = grid_charge_phase_caps_kw(20.0, 400.0, expiry_lifts=(False, True, False), accept_ha_down=True,
                                         site_margin_a=1.0, **base)
    assert accepted == pytest.approx((19.0 * volts / 1000, lifted, 19.0 * volts / 1000))
