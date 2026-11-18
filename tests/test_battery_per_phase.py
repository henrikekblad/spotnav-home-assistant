"""A grid charge per phase (`commands.PhaseControl`) through the executor against a fake plant that follows its
targets: the arithmetic, the writes and their read-back, the hand-back's order, the fallback to the balanced charge,
and an unbalanced house (L2 at 18 A on a 20 A fuse) that never passes the fuse on any phase when Home Assistant
stops."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.spotnav.battery.commands import SIGENERGY_CONTROL, SIGENERGY_PHASES, phase_table
from custom_components.spotnav.battery.executor import (
    RoleRead,
    HEADROOM_MARGIN_A,
    RATE_UP_DWELL_S,
    REARM_S,
    ExecInputs,
    ExecState,
    Executor,
    StepResult,
    phase_charge_kw,
    preview,
)
from custom_components.spotnav.battery.gate import grid_charge_cap_kw, grid_charge_phase_caps_kw
from custom_components.spotnav.battery.ledger import Ledger
from custom_components.spotnav.battery.monitor import (
    PHASE_GAP_S,
    PHASE_GRACE_S,
    PHASE_OVER_GAP_S,
    PHASE_OVER_GRACE_S,
    Monitor,
    judge_phases,
)
from tests.battery_control_fakes import SIGENERGY_STATES, T0, TICK, FakeBattery, events, written

VOLTS = 400.0 / 3 ** 0.5
FUSE_A = 20.0
#: The owner's house (2026-11-18): the learned peaks, L2 at 18 A.
PEAKS = {"L1": 13.6, "L2": 18.0, "L3": 17.4}
TARGETS = SIGENERGY_PHASES.names()
PHASED = SIGENERGY_CONTROL.with_phases(True)
PHASED_STATES = {**SIGENERGY_STATES, "phase_l1_target": "0.0", "phase_l2_target": "0.0", "phase_l3_target": "0.0",
                 "phase_control": "on", "power_target": "0.0"}


def _caps(peaks: dict[str, float] | None = PEAKS, cars_a: float = 0.0) -> tuple[float, float, float]:
    return grid_charge_phase_caps_kw(FUSE_A, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=cars_a, house_peak_kw=None,
                                     house_phase_peaks_a=peaks)


@dataclass
class Site:
    """A house on three phases and a plant that follows its targets per phase while Remote EMS is in PCS Remote
    Control (negative: it takes from the grid on that phase)."""

    battery: FakeBattery
    house_a: list[float] = field(default_factory=lambda: [2.0, 3.0, 2.0])
    #: What the plant does instead of following: a factor on each target, and a draw it adds on a phase (kW).
    follows: float = 1.0
    stray_kw: tuple[float, float, float] = (0.0, 0.0, 0.0)
    plant_known: bool = True

    def targets(self) -> tuple[float, float, float]:
        states = self.battery.states
        return (float(states.get(TARGETS[0], 0)), float(states.get(TARGETS[1], 0)), float(states.get(TARGETS[2], 0)))

    def plant_kw(self) -> tuple[float, float, float]:
        states = self.battery.states
        following = states["remote_ems"] == "on" and states["control_mode"] == "PCS Remote Control"
        if not following:
            return (0.0, 0.0, 0.0)
        out = [target * self.follows - stray for target, stray in zip(self.targets(), self.stray_kw, strict=True)]
        return (out[0], out[1], out[2])

    def phase_a(self, house_a: list[float] | None = None) -> list[float]:
        """Each phase's current: the house and what the plant takes on it."""
        house = self.house_a if house_a is None else house_a
        return [amps + max(-power, 0.0) * 1000 / VOLTS for amps, power in zip(house, self.plant_kw(), strict=True)]

    def inputs(self, now: datetime, action: str | None, rate_kw: float, **overrides: Any) -> ExecInputs:
        plant = self.plant_kw()
        headroom = [FUSE_A - amps for amps in self.phase_a()]
        charge = max(-sum(plant), 0.0)
        base = ExecInputs(
            now=now, facts=self.battery.facts(charge * 0.8), reads=self.battery.reads(), action=action,
            rate_kw=rate_kw, battery_age_s=5.0, measurement_ok=True, headroom_a=min(headroom),
            grid_import_kw=sum(self.house_a) * VOLTS / 1000 + charge,
            phase_headroom_a=(headroom[0], headroom[1], headroom[2]),
            plant_phase_kw=plant if self.plant_known else None, phase_caps_kw=_caps(),
        )
        return replace(base, **overrides)


def _site(control=PHASED, **kwargs: Any) -> tuple[Executor, Site]:
    states = dict(PHASED_STATES if control.phased else SIGENERGY_STATES)
    # 40031 as SpotNav's own hand-back leaves it (a plant never in Remote EMS holds 0, PCS Remote Control: below).
    battery = FakeBattery(control, states, register_40031="Maximum Self Consumption")
    return Executor(control), Site(battery, **kwargs)


def _run(executor: Executor, site: Site, start: datetime, looks: int, action: str | None, rate_kw: float = 0.0,
         **overrides: Any) -> tuple[datetime, list[StepResult]]:
    now = start
    results: list[StepResult] = []
    failed: tuple[str, ...] = ()
    for _ in range(looks):
        result = executor.step(site.inputs(now, action, rate_kw, failed=failed, **overrides))
        results.append(result)
        failed = tuple(op.role for op in result.ops if not site.battery.apply(op))
        now += TICK
    return now, results


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_data_a_phased_control_holds_the_targets_and_every_other_action_writes_them_to_zero() -> None:
    assert SIGENERGY_CONTROL.phased is False and PHASED.phased is True
    assert PHASED.with_phases(False) is SIGENERGY_CONTROL and PHASED.with_phases(True) is PHASED
    # Negative charges; the size is rounded down to the step; a discharge (the verification's probe) is positive.
    assert PHASED.wanted("grid_charge", 9.0, phases=(1.019, 0.0, 0.138)) == {
        "charge_limit": "<snapshot>", "discharge_limit": "<snapshot>", "remote_ems": True,
        "control_mode": "PCS Remote Control", "phase_l1_target": -1.01, "phase_l2_target": 0.0,
        "phase_l3_target": -0.13, "power_target": 0.0,
    }
    assert PHASED.wanted("grid_charge", 0.0, phases=(0.0, -1.0, 0.0))["phase_l2_target"] == 1.0
    # Without a power per phase the phased control runs the brand's own balanced charge, the targets at 0.
    assert PHASED.wanted("grid_charge", 3.5) == {
        **SIGENERGY_CONTROL.wanted("grid_charge", 3.5), **dict.fromkeys((*TARGETS, "power_target"), 0.0)
    }
    for action in ("hold", "discharge_house", "export_discharge"):
        assert PHASED.wanted(action, 2.0) == {**SIGENERGY_CONTROL.wanted(action, 2.0), **dict.fromkeys((*TARGETS, "power_target"), 0.0)}
    # The hold variant is kept through the change, and a brand without targets is itself either way.
    mode4 = SIGENERGY_CONTROL.with_hold("pv_first").with_phases(True)
    assert mode4.variant == "pv_first" and mode4.wanted("hold", 0.0)["control_mode"] == "Command Charging (PV First)"
    assert mode4.with_phases(False).variant == "pv_first"
    assert PHASED.with_hold("pv_first").phased is True
    assert SIGENERGY_CONTROL.wanted("grid_charge", 3.5, phases=(1.0, 1.0, 1.0)) == SIGENERGY_CONTROL.wanted("grid_charge", 3.5)
    # The base control knows nothing of the targets' roles: a site without them is asked for nothing new.
    assert not {*TARGETS, "power_target"} & {role.name for role in SIGENERGY_CONTROL.roles}
    assert phase_table()[0]["writes"]["phase_l1_target"] == -1.0


# --- the arithmetic --------------------------------------------------------------------------------------------------


def test_each_phase_gets_what_its_own_headroom_allows_within_the_plan_s_power_and_the_rating() -> None:
    _executor, site = _site()
    caps = _caps()
    assert [round(cap, 3) for cap in caps] == [1.016, 0.0, 0.139]
    look = site.inputs(T0, "grid_charge", 12.0)
    assert phase_charge_kw(look, None) == ((0.92, 0.0, 0.0), None)
    # The plan's power bounds the sum, spread as evenly as the caps allow.
    assert phase_charge_kw(replace(look, rate_kw=0.8), None) == ((0.69, 0.0, 0.0), None)
    # Whole amps: L1's 4.4 A of room is 4 A (0.92 kW), L3's 0.6 A is nothing.
    assert phase_charge_kw(replace(look, rate_kw=0.6), None) == (None, "fuse_headroom")
    # The battery's own limit bounds it too (the fake's rating is 10 kW; a balanced house on a larger fuse).
    even = grid_charge_phase_caps_kw(63.0, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None,
                                     house_phase_peaks_a={"L1": 9.0, "L2": 9.0, "L3": 9.0})
    wide = replace(look, phase_caps_kw=even, phase_headroom_a=(60.0, 60.0, 60.0), rate_kw=40.0)
    assert 10.0 - 3 * VOLTS / 1000 < sum(phase_charge_kw(wide, None)[0]) <= 10.0, "whole amps under the rating"
    # A car at 4 A leaves L1 0.4 A: too little to run.
    assert phase_charge_kw(replace(look, phase_caps_kw=_caps(cars_a=4.0)), None) == (None, "fuse_headroom")
    # The live room: a phase whose headroom is short gets less, at once; an unknown phase stops it.
    busy = replace(look, phase_headroom_a=(5.0, 17.0, 18.0))
    assert phase_charge_kw(busy, None) == ((0.69, 0.0, 0.0), None)
    assert phase_charge_kw(replace(look, phase_headroom_a=None), None) == (None, "headroom_unknown")
    # What the plant already takes on a phase is counted back, at most what was commanded and what it measures.
    running = replace(look, phase_headroom_a=(3.0, 17.0, 17.4), plant_phase_kw=(-0.92, 0.0, 0.0))
    assert phase_charge_kw(running, (0.92, 0.0, 0.0)) == ((0.92, 0.0, 0.0), None)
    assert phase_charge_kw(replace(running, plant_phase_kw=(-0.5, 0.0, -0.13)), (0.92, 0.0, 0.0)) == (
        (0.69, 0.0, 0.0), None)
    assert phase_charge_kw(replace(running, plant_phase_kw=None), (0.92, 0.0, 0.0)) == (None, "fuse_headroom")
    # A held car with "car first": the sum yields as the balanced charge does.
    held = replace(running, car_held=True, car_first=True, facts=replace(running.facts, power_kw=1.1), grid_import_kw=3.0)
    assert phase_charge_kw(held, (0.92, 0.0, 0.0), yield_now=True) == (None, "fuse_headroom")
    # Between its steps it keeps what it has, to the rounding (no write: within the tolerance).
    assert phase_charge_kw(held, (0.92, 0.0, 0.0), yield_now=False) == ((0.92, 0.0, 0.0), None)


# --- the writes ------------------------------------------------------------------------------------------------------


def test_a_charge_per_phase_writes_the_targets_then_remote_ems_then_the_mode_each_read_back() -> None:
    executor, site = _site()
    _now, results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    assert results[0].persist_first
    # L2 has no room: its target stays 0 and is not written.
    assert written(results) == [
        ("phase_l1_target", -0.92), ("remote_ems", True),
        ("control_mode", "PCS Remote Control"),
    ]
    assert [len(result.ops) for result in results[:4]] == [1, 1, 1, 0], "one write a look, each read back first"
    assert len(events(results, "confirmed")) == 3
    state = executor.state
    assert state.phase == "active" and state.phase_kw == (0.92, 0.0, 0.0) and state.rate_kw == 0.92
    assert state.phased is True and events(results, "enter")[0]["phase_kw"] == [0.92, 0.0, 0.0]
    # The battery limits are left as found; the plant draws on L1 and L3 only.
    assert site.battery.states["charge_limit"] == "unknown" and site.battery.states["discharge_limit"] == "23.04"
    assert site.plant_kw() == (-0.92, 0.0, 0.0)
    # What it would write is what the dry run shows.
    seen = preview(PHASED, _site()[1].inputs(T0, "grid_charge", 12.0))
    assert [(op.role, op.value) for op in seen.writes] == written(results)
    assert [op.role for op in seen.restores] == ["phase_l1_target", "control_mode", "remote_ems"]


def test_the_hand_back_sets_the_targets_to_zero_then_the_mode_then_remote_ems_off_each_read_back() -> None:
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    now, results = _run(executor, site, now, 8, "self_consume")
    assert written(results) == [
        ("phase_l1_target", 0.0), ("control_mode", "Maximum Self Consumption"),
        ("remote_ems", False),
    ]
    assert [len(result.ops) for result in results[:4]] == [1, 1, 1, 0]
    assert all(op.restore for result in results for op in result.ops)
    state = executor.state
    assert state.phase == "idle" and not state.holds_snapshot and state.phase_kw is None
    assert site.targets() == (0.0, 0.0, 0.0) and site.battery.states["remote_ems"] == "off"


def test_a_restart_hands_a_charge_per_phase_back_with_its_own_roles() -> None:
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    stored = executor.state.as_dict()
    assert stored["phased"] is True and stored["phase_kw"] == [0.92, 0.0, 0.0]
    # The store names no control: the brand's plain one is made phased by the stored state.
    again = Executor(SIGENERGY_CONTROL, Ledger(), Monitor(), ExecState.from_dict(stored))
    assert again.control.phased is True
    again.restart(now)
    now, results = _run(again, site, now, 8, None, stopping="restart")
    assert [role for role, _value in written(results)] == [
        "phase_l1_target", "control_mode", "remote_ems"
    ]
    assert again.state.phase == "idle" and site.targets() == (0.0, 0.0, 0.0)
    assert ExecState.from_dict({"phase_kw": [1.0, "x", 0.0]}).phase_kw is None


def test_a_change_of_action_stops_the_targets_before_anything_else_and_a_hold_keeps_them_at_zero() -> None:
    executor, site = _site()
    now, results = _run(executor, site, T0, 6, "hold")
    assert written(results) == [("discharge_limit", 0.0), ("remote_ems", True)], "the targets read 0: not written"
    assert site.battery.states["control_mode"] == "Maximum Self Consumption"
    now, results = _run(executor, site, now, 8, "grid_charge", 12.0)
    assert written(results) == [("discharge_limit", 23.04), ("phase_l1_target", -0.92),
                                ("control_mode", "PCS Remote Control")]
    now, results = _run(executor, site, now, 8, "hold")
    assert written(results) == [("phase_l1_target", 0.0), ("control_mode", "Maximum Self Consumption"),
                                ("discharge_limit", 0.0)]
    assert executor.state.phase_kw is None and executor.state.action == "hold"


# --- the fuse ----------------------------------------------------------------------------------------------------------


def test_an_unbalanced_house_stays_under_the_fuse_on_every_phase_when_home_assistant_stops() -> None:
    """L2 at 18 A on a 20 A fuse. The balanced charge has no room at all; per phase L1 and L3 charge. Then Home
    Assistant stops: the targets stay, the house goes to its learned peak on every phase at once."""
    assert grid_charge_cap_kw(FUSE_A, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                              plant_phases=3, house_phase_peaks_a=PEAKS) == 0.0
    executor, site = _site()
    _run(executor, site, T0, 8, "grid_charge", 12.0)
    assert site.targets() == (-0.92, 0.0, 0.0)
    # No more looks: nothing writes the plant again. The house at its peaks, and each phase alone at its peak.
    peak = list(PEAKS.values())
    for house in (peak, [peak[0], 0.0, 0.0], [0.0, peak[1], 0.0], [0.0, 0.0, peak[2]], [peak[0], peak[1], 0.0]):
        amps = site.phase_a(house)
        assert max(amps) <= FUSE_A - HEADROOM_MARGIN_A + 1e-9, (house, amps)
    amps = site.phase_a(peak)
    assert amps[0] == pytest.approx(13.6 + 920 / VOLTS, abs=0.02) and amps[0] < 18.0
    assert amps[1] == 18.0 and amps[2] == 17.4, "nothing is added to L2, and L3's 0.6 A is no whole amp"
    # The same holds for any plan power, any house within its peaks while the targets were set, and with a car.
    for cars_a in (0.0, 2.0, 4.0):
        for rate in (0.6, 1.0, 5.0, 25.0):
            for house_now in ([0.0, 0.0, 0.0], [13.0, 2.0, 2.0], [2.0, 17.5, 2.0], [2.0, 2.0, 17.0]):
                executor, site = _site(house_a=list(house_now))
                _run(executor, site, T0, 8, "grid_charge", rate, phase_caps_kw=_caps(cars_a=cars_a))
                stopped = [amps + cars_a for amps in site.phase_a(peak)]
                # Every phase the charge is on; a phase without a target carries the house and the car alone.
                charged = [amps for amps, target in zip(stopped, site.targets(), strict=True) if target != 0.0]
                assert all(amps <= FUSE_A - HEADROOM_MARGIN_A + 1e-9 for amps in charged), (
                    cars_a, rate, house_now, stopped)
                assert all(target <= 0.0 for target in site.targets()), "never a discharge"


def test_a_phase_whose_room_falls_gets_less_at_once_and_its_share_back_after_the_dwell() -> None:
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    # L1's house passes its learned peak: 14.5 A and the charge's 4.4 A leave 1.1 A of headroom, under the 2 A kept.
    site.house_a[0] = 14.5
    now, results = _run(executor, site, now, 3, "grid_charge", 12.0)
    assert written(results) == [("phase_l1_target", -0.69)]
    assert events(results, "rate")[0]["was_phase_kw"] == [0.92, 0.0, 0.0]
    assert max(site.phase_a()) <= FUSE_A - HEADROOM_MARGIN_A + 0.05
    # The kettle off: back to what the caps allow, but only after the dwell.
    site.house_a[0] = 2.0
    now, results = _run(executor, site, now, 2, "grid_charge", 12.0)
    assert written(results) == []
    now, results = _run(executor, site, now + timedelta(seconds=RATE_UP_DWELL_S), 3, "grid_charge", 12.0)
    assert written(results) == [("phase_l1_target", -0.92)]
    # A small wobble writes nothing.
    site.house_a[0] = 2.1
    assert written(_run(executor, site, now, 3, "grid_charge", 12.0)[1]) == []


def test_a_phase_near_the_fuse_hands_the_charge_back_at_once_every_role_in_one_look() -> None:
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    # L2, which the charge is not on, at 19.5 A: handed back all the same, so the plant covers that phase again.
    site.house_a[1] = 19.5
    result = executor.step(site.inputs(now, "grid_charge", 12.0))
    assert [event["reason"] for event in result.events if event["kind"] == "hand_back"] == ["fuse_headroom"]
    assert [(op.role, op.value) for op in result.ops] == [
        ("phase_l1_target", 0.0), ("control_mode", "Maximum Self Consumption"),
        ("remote_ems", False),
    ]


def test_too_little_room_starts_nothing_and_ends_a_running_charge() -> None:
    executor, site = _site(house_a=[17.0, 3.0, 17.9])
    _now, results = _run(executor, site, T0, 3, "grid_charge", 12.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "fuse_headroom"
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    site.house_a[0] = 17.5
    now, results = _run(executor, site, now, 8, "grid_charge", 12.0)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom" and site.targets() == (0.0, 0.0, 0.0)


# --- obeyed? ---------------------------------------------------------------------------------------------------------


def test_each_phase_is_judged_on_the_plant_s_own_power_against_its_own_target() -> None:
    targets = (1.0, 0.0, 0.2)
    assert judge_phases(targets, (-1.0, 0.0, -0.2)) == "ok"
    assert judge_phases(targets, (-0.9, 0.1, -0.25)) == "ok"
    assert judge_phases(targets, (-1.0, -0.4, -0.2)) == "over", "a phase without a target takes from the grid"
    assert judge_phases(targets, (-1.4, 0.0, -0.2)) == "over"
    assert judge_phases(targets, (0.0, 0.0, 0.0)) == "no_effect"
    assert judge_phases(targets, (0.5, 0.2, 0.9)) == "no_effect", "the plant covers the house instead"
    assert judge_phases(targets, (-0.4, 0.0, -0.2)) == "partial"
    assert judge_phases(targets, (0.0, 0.0, 0.0), tapering=True) == "ok", "near full a small draw says nothing"
    assert judge_phases(targets, (-1.4, 0.0, 0.0), tapering=True) == "over"
    assert judge_phases((0.1, 0.0, 0.0), (0.0, 0.0, 0.0)) == "ok", "a target too small to judge"


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ({"stray_kw": (0.0, 0.6, 0.0)}, "not_obeyed:grid_charge:phase_over"),
        ({"follows": 0.0}, "no_effect:grid_charge"),
    ],
)
def test_a_plant_that_does_not_follow_its_targets_is_handed_back_and_blocked(fault: dict, reason: str) -> None:
    executor, site = _site(**fault)
    looks = int((PHASE_GRACE_S + 3 * PHASE_GAP_S) / TICK.total_seconds()) + 12
    now, results = _run(executor, site, T0, looks, "grid_charge", 12.0)
    assert [event["reason"] for event in events(results, "hand_back")] == [reason]
    assert executor.state.phase == "blocked" and executor.state.blocked == reason
    assert site.targets() == (0.0, 0.0, 0.0) and site.battery.states["remote_ems"] == "off"
    # Not before three readings in a row, the first after the grace.
    first = next(index for index, result in enumerate(results) if events([result], "hand_back"))
    if "phase_over" in reason:
        # The runaway is judged soon: 20 s after the writes were taken, three readings 10 s apart.
        assert PHASE_OVER_GRACE_S + 2 * PHASE_OVER_GAP_S <= first * TICK.total_seconds() <= 90
    else:
        assert first * TICK.total_seconds() >= PHASE_GRACE_S + 2 * PHASE_GAP_S


def test_a_plant_that_follows_is_never_handed_back_and_one_without_a_reading_is_not_judged() -> None:
    executor, site = _site()
    _now, results = _run(executor, site, T0, 60, "grid_charge", 12.0)
    assert executor.state.phase == "active" and not events(results, "monitor")
    executor, site = _site(follows=0.0, plant_known=False)
    _now, results = _run(executor, site, T0, 60, "grid_charge", 12.0)
    assert not events(results, "monitor")


# --- the fallback ----------------------------------------------------------------------------------------------------


def test_without_caps_per_phase_the_phased_control_runs_the_balanced_charge_and_the_plain_control_ignores_them() -> None:
    executor, site = _site()
    _now, results = _run(executor, site, T0, 8, "grid_charge", 3.0, phase_caps_kw=None)
    assert written(results) == [("charge_limit", 3.0), ("remote_ems", True),
                                ("control_mode", "Command Charging (Grid First)")]
    assert executor.state.phase_kw is None and site.targets() == (0.0, 0.0, 0.0)
    executor, site = _site(SIGENERGY_CONTROL)
    _now, results = _run(executor, site, T0, 8, "grid_charge", 3.0)
    assert written(results) == [("charge_limit", 3.0), ("remote_ems", True),
                                ("control_mode", "Command Charging (Grid First)")]
    assert executor.state.phase_kw is None and executor.state.phased is False


def test_the_kind_of_charge_never_changes_while_it_runs() -> None:
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    now, results = _run(executor, site, now, 8, "grid_charge", 3.0, phase_caps_kw=None)
    assert events(results, "hand_back")[0]["reason"] == "per_phase_changed"
    assert executor.state.phase == "idle" and site.targets() == (0.0, 0.0, 0.0)
    # Re-armed, it starts as the balanced charge.
    now, results = _run(executor, site, now + timedelta(seconds=REARM_S), 8, "grid_charge", 3.0, phase_caps_kw=None)
    assert site.battery.states["control_mode"] == "Command Charging (Grid First)"
    now, results = _run(executor, site, now, 8, "grid_charge", 12.0)
    assert events(results, "hand_back")[0]["reason"] == "per_phase_changed"


def test_the_control_changes_between_phased_and_plain_only_while_it_holds_nothing() -> None:
    executor, site = _site(SIGENERGY_CONTROL)
    site.battery.states.update(PHASED_STATES)
    assert executor.use_phases(False) is True
    assert executor.use_phases(True) is True and executor.control.phased and executor.state.phased
    site.battery.control = executor.control
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    assert executor.use_phases(False) is False and executor.control.phased, "not while it charges"
    assert executor.use_variant("pv_first") is False
    now, _results = _run(executor, site, now, 8, "self_consume")
    assert executor.use_phases(False) is True and executor.control is SIGENERGY_CONTROL


def test_per_phase_needs_its_controls_and_the_independent_phase_switch_on() -> None:
    executor, site = _site()
    site.battery.states["phase_control"] = "off"
    _now, results = _run(executor, site, T0, 2, "grid_charge", 12.0)
    assert events(results, "not_started")[0]["reason"] == "phase_control_off" and written(results) == []
    executor, site = _site()
    site.battery.missing = {"phase_l2_target"}
    _now, results = _run(executor, site, T0, 2, "grid_charge", 12.0)
    assert events(results, "not_started")[0]["reason"] == "control_not_found:phase_l2_target"


def test_a_target_that_is_not_taken_hands_back_and_blocks() -> None:
    executor, site = _site()
    site.battery.drops = {"phase_l1_target"}
    _now, results = _run(executor, site, T0, 20, "grid_charge", 12.0)
    assert events(results, "hand_back")[0]["reason"].startswith("write_not_taken:phase_l1_target")
    assert executor.state.phase == "blocked" and site.targets() == (0.0, 0.0, 0.0)


def test_the_probe_writes_a_discharge_on_one_phase_as_it_is_and_a_charge_only_within_its_room() -> None:
    executor, site = _site()
    now, results = _run(executor, site, T0, 6, "grid_charge", 0.0, phase_caps_kw=None, phase_probe_kw=(0.0, -1.0, 0.0))
    assert written(results) == [("phase_l2_target", 1.0), ("remote_ems", True), ("control_mode", "PCS Remote Control")]
    assert executor.state.phase_kw == (0.0, -1.0, 0.0) and site.plant_kw() == (0.0, 1.0, 0.0)
    now, results = _run(executor, site, now, 4, "grid_charge", 0.0, phase_caps_kw=None, phase_probe_kw=(0.0, 0.0, -1.0))
    assert written(results) == [("phase_l3_target", 1.0), ("phase_l2_target", 0.0)]
    # It starts only where a hold could: the battery's discharge now lands on the grid once the targets are 0.
    fresh, quiet = _site()
    look = quiet.inputs(T0, "grid_charge", 0.0, phase_caps_kw=None, phase_probe_kw=(0.0, 0.0, 0.0), headroom_a=12.0)
    look = replace(look, facts=replace(look.facts, power_kw=-3.0))
    assert events([fresh.step(look)], "not_started")[0]["reason"] == "headroom_too_small"
    # A charge in a probe has no cap here: nothing; a larger discharge is bounded.
    look = site.inputs(now, "grid_charge", 0.0, phase_caps_kw=None, phase_probe_kw=(2.0, -5.0, 0.0))
    assert phase_charge_kw(look, None) == ((0.0, -1.0, 0.0), None)


# --- targets left standing, the writes a day, and the recipe's other forms ---------------------------------------


def test_the_plants_total_target_is_owned_too_written_to_zero_before_anything_else() -> None:
    executor, site = _site()
    site.battery.states["power_target"] = "-3.0"  # left there by someone
    _now, results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    assert written(results)[:2] == [("power_target", 0.0), ("phase_l1_target", -0.92)]
    assert site.battery.states["remote_ems"] == "on", "Remote EMS only after the total reads 0"
    assert [role for role, _value in written(results)].index("remote_ems") == 2
    # A hold on the phased control clears it the same way.
    executor, site = _site()
    site.battery.states["power_target"] = "2.0"
    _now, results = _run(executor, site, T0, 8, "hold")
    assert written(results)[0] == ("power_target", 0.0)


def test_the_plain_control_starts_nothing_while_a_target_it_does_not_own_reads_other_than_zero() -> None:
    executor, site = _site(SIGENERGY_CONTROL)
    assert {role.name for role, _value, _why in SIGENERGY_CONTROL.watched} == {*TARGETS, "power_target"}
    assert PHASED.watched == (), "the phased control owns them"

    def reads(**states: str):
        return {**site.battery.reads(), **{
            name: RoleRead(entity_id=f"number.{name}", state=state, found=True, enabled=True, step=0.001)
            for name, state in states.items()
        }}

    for name in (*TARGETS, "power_target"):
        _now, results = _run(executor, site, T0, 2, "hold", reads=reads(**{name: "-1.5"}))
        assert events(results, "not_started")[0]["reason"] == f"stale_target:{name}" and written(results) == []
    # Reading 0, reading nothing, disabled or not there at all: no reason against starting.
    disabled = {"phase_l1_target": RoleRead(entity_id="number.x", state="-1.5", found=True, enabled=False)}
    for extra in (reads(phase_l1_target="0.0", power_target="0"), reads(phase_l2_target="unavailable"),
                  {**site.battery.reads(), **disabled}, site.battery.reads()):
        fresh, plant = _site(SIGENERGY_CONTROL)
        _now, results = _run(fresh, plant, T0, 2, "hold", reads=extra)
        assert fresh.state.phase == "active", extra


def test_setpoints_and_settings_have_their_own_writes_a_day_and_a_spent_setpoint_budget_stops_only_the_charge() -> None:
    assert PHASED.write_cap("setpoint") == 500 and PHASED.write_cap("setting") == 96
    assert PHASED.write_cap("anything_else") == 96, "a class not checked is a setting"
    assert {role.write_class for role in PHASED.roles if role.name in (*TARGETS, "power_target")} == {"setpoint"}
    assert all(role.write_class == "setting" for role in SIGENERGY_CONTROL.roles)
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    assert executor.state.setpoint_writes_today == 1 and executor.state.writes_today == 2
    assert ExecState.from_dict(executor.state.as_dict()).setpoint_writes_today == 1
    # The runaway guard: at the cap the charge is handed back, not blocked, and nothing is logged at every look.
    executor.state = replace(executor.state, setpoint_writes_today=500)
    now, results = _run(executor, site, now, 8, "grid_charge", 12.0)
    assert events(results, "hand_back")[0]["reason"] == "setpoint_write_cap"
    assert executor.state.phase == "idle" and site.targets() == (0.0, 0.0, 0.0)
    now, results = _run(executor, site, now + timedelta(seconds=REARM_S), 3, "grid_charge", 12.0)
    assert written(results) == [] and events(results, "not_started") == []
    # A hold still starts, and so does the balanced charge.
    now, results = _run(executor, site, now, 6, "hold")
    assert executor.state.phase == "active" and executor.state.action == "hold"
    # The next day the budget is whole again.
    executor, site = _site()
    executor.state = replace(executor.state, day=T0.date().isoformat(), setpoint_writes_today=500)
    _now, results = _run(executor, site, T0 + timedelta(days=1), 8, "grid_charge", 12.0)
    assert executor.state.phase == "active" and executor.state.setpoint_writes_today == 1


def test_a_grid_charge_leaves_the_last_setting_writes_of_the_day_to_a_hold() -> None:
    from custom_components.spotnav.battery.executor import CHARGE_WRITE_RESERVE, DAILY_WRITE_CAP

    for control in (PHASED, SIGENERGY_CONTROL):
        executor, site = _site(control)
        spent = ExecState(day=T0.date().isoformat(), writes_today=DAILY_WRITE_CAP - CHARGE_WRITE_RESERVE)
        executor.state = replace(spent, phased=control.phased, variant=control.variant)
        _now, results = _run(executor, site, T0, 3, "grid_charge", 3.0)
        assert written(results) == [] and executor.state.phase == "idle"
        _now, results = _run(executor, site, T0, 6, "hold")
        assert executor.state.phase == "active", "the reserve is the hold's"
    # A running charge is handed back when it reaches the reserve, without a block.
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    executor.state = replace(executor.state, writes_today=DAILY_WRITE_CAP - CHARGE_WRITE_RESERVE)
    now, results = _run(executor, site, now, 8, "grid_charge", 12.0)
    assert events(results, "hand_back")[0]["reason"] == "charge_write_budget" and executor.state.phase == "idle"


def test_a_raise_is_given_ten_seconds_before_a_fallen_room_counts_and_falling_phases_are_written_first() -> None:
    even = {"L1": 9.0, "L2": 9.0, "L3": 9.0}
    caps = _caps(even)
    executor, site = _site()
    now, _results = _run(executor, site, T0, 10, "grid_charge", 12.0, phase_caps_kw=caps)
    assert executor.state.phase_kw == (2.07, 2.07, 2.07)
    # L1 loses room while L2 was lower than it may be: L1 down and L2 up in one change, the falling one first.
    executor.state = replace(executor.state, phase_kw=(2.07, 1.15, 2.07))
    site.house_a[0] = 9.5  # 9.5 A and the charge's 9 A: 1.5 A of headroom, under the 2 A kept
    result = executor.step(site.inputs(now, "grid_charge", 12.0, phase_caps_kw=caps))
    assert [event["phase_kw"] for event in result.events if event["kind"] == "rate"] == [[1.84, 2.07, 2.07]]
    assert executor.state.lead == ("phase_l1_target",) and [op.role for op in result.ops] == ["phase_l1_target"]
    assert executor.state.phase_raised_at == now and executor.state.phase_was_kw == (2.07, 1.15, 2.07)
    # Right after a raise the plant's own power lags: a room that reads short is not acted on for 10 s.
    executor, site = _site()
    now, _results = _run(executor, site, T0, 10, "grid_charge", 12.0, phase_caps_kw=caps)
    executor.state = replace(executor.state, phase_raised_at=now)
    lagging = site.inputs(now, "grid_charge", 12.0, phase_caps_kw=caps, plant_phase_kw=(-1.0, -2.07, -2.07),
                          phase_headroom_a=(2.5, 9.0, 9.0))
    assert executor.step(lagging).ops == ()
    later = replace(lagging, now=now + timedelta(seconds=11))
    assert [(op.role, op.value) for op in executor.step(later).ops] == [("phase_l1_target", -0.92)]


def test_the_runaway_is_judged_against_the_larger_of_the_old_and_new_target_and_a_retarget_does_not_reset_it() -> None:
    executor, site = _site(stray_kw=(0.0, 0.6, 0.0))
    now, _results = _run(executor, site, T0, 4, "grid_charge", 12.0)
    assert executor.state.phase == "active" and executor.state.phase_over == 1
    counted = executor.state.phase_over
    # L1's room falls: the targets change, the count goes on (L2 still takes 0.6 kW with no target).
    site.house_a[0] = 14.5
    now, results = _run(executor, site, now, 1, "grid_charge", 12.0)
    assert events(results, "rate") and executor.state.phase_over == counted + 1
    now, results = _run(executor, site, now, 4, "grid_charge", 12.0)
    assert events(results, "hand_back")[0]["reason"] == "not_obeyed:grid_charge:phase_over"
    # While a lowered target settles the phase is held to the old one: no false runaway from the plant's ramp.
    executor, site = _site()
    now, _results = _run(executor, site, T0, 8, "grid_charge", 12.0)
    executor.state = replace(executor.state, phase_kw=(0.46, 0.0, 0.0), phase_was_kw=(0.92, 0.0, 0.0),
                             rate_changed_at=now, target={**executor.state.target, "phase_l1_target": -0.92})
    for _ in range(5):
        result = executor.step(site.inputs(now, "grid_charge", 12.0, plant_phase_kw=(-0.92, 0.0, 0.0),
                                           phase_headroom_a=(3.0, 17.0, 18.0)))
        now += TICK
    assert executor.state.phase_over == 0 and executor.state.phase == "active"


def test_a_recipe_may_set_the_grid_s_power_have_a_state_that_depends_on_the_charge_and_renew_a_watchdog() -> None:
    from custom_components.spotnav.battery.commands import Want

    assert SIGENERGY_PHASES.target_kind == "plant" and SIGENERGY_PHASES.watchdog_roles == ()
    # A state that depends on the charge (a mode per direction), and a watchdog the phased charge keeps alive.
    spec = replace(
        SIGENERGY_PHASES, target_kind="grid", watchdog_roles=("control_mode",),
        follow=lambda phases: (Want("remote_ems", True), Want("control_mode", "in" if sum(phases) >= 0 else "out")),
    )
    control = replace(SIGENERGY_CONTROL, phase_control=spec).with_phases(True)
    assert control.wanted("grid_charge", 0.0, phases=(1.0, 0.0, 0.0))["control_mode"] == "in"
    assert control.wanted("grid_charge", 0.0, phases=(-1.0, 0.0, 0.0))["control_mode"] == "out"
    assert control.watchdog_roles == ("control_mode",) and SIGENERGY_CONTROL.with_phases(True).watchdog_roles == ()
    # Judged on the site meter's phases where the targets are the grid's: the plant's own power says nothing here.
    executor = Executor(control)
    look = _site()[1].inputs(T0, "grid_charge", 12.0, grid_phase_kw=(1.0, 0.9, 0.0), plant_phase_kw=(0.0, 0.0, 0.0))
    assert executor._phase_measured(look) == (-1.0, -0.9, -0.0)
    assert Executor(PHASED)._phase_measured(look) == (0.0, 0.0, 0.0)
    assert executor._phase_measured(replace(look, grid_phase_kw=None)) is None
