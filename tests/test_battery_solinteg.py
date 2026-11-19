"""Solinteg's control recipe (`commands.SOLINTEG_CONTROL`, the Solinteg plugin of `solax_modbus`) through the
executor against a fake inverter that does what the plugin's source and the owners' field notes say: the recipe as
data, the writes and their read-back, the hand-back's order, a charge that keeps the fuse when Home Assistant stops
(the inverter has no dead-man), the same per phase for an unbalanced house, the dongle's stalls, and the day's writes.
Nothing here has run on a device."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.spotnav.battery import optin
from custom_components.spotnav.battery.adapters import SOLINTEG, adapter_for
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    NORMAL,
    READBACK_STALL_S,
    SIGENERGY_CONTROL,
    SOLAX_CONTROL,
    SOLINTEG_CONTROL,
    SOLINTEG_PHASES,
    control_for,
    phase_table,
    table,
)
from custom_components.spotnav.battery.executor import (
    HEADROOM_MARGIN_A,
    REARM_S,
    ExecInputs,
    ExecState,
    Executor,
    StepResult,
    preview,
)
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.gate import grid_charge_cap_kw, grid_charge_phase_caps_kw
from custom_components.spotnav.battery.ledger import READBACK_DEADLINE_S, Ledger
from custom_components.spotnav.battery.monitor import Monitor
from tests.battery_control_fakes import SIGENERGY_STATES, T0, TICK, FakeBattery, events, run, written

VOLTS = 400.0 / 3 ** 0.5
FUSE_A = 20.0
#: An unbalanced house: the learned peaks, L2 at 18 A on a 20 A fuse.
PEAKS = {"L1": 13.6, "L2": 18.0, "L3": 17.4}
#: The plugin's keys as its unique ids carry them (`<hub name>_<key>`), per domain.
KEYS = {
    "working_mode": ("select", "solinteg_working_mode"),
    "battery_target": ("number", "solinteg_battery_charge_discharge_power_target"),
    "acctrl_mode": ("select", "solinteg_ems_acctrl_mode"),
    "unbalanced_output": ("select", "solinteg_grid_unbalanced_output"),
    "phase_l1_target": ("number", "solinteg_ems_acctrl_phase_a_power"),
    "phase_l2_target": ("number", "solinteg_ems_acctrl_phase_b_power"),
    "phase_l3_target": ("number", "solinteg_ems_acctrl_phase_c_power"),
}
STATES = {"working_mode": "General", "battery_target": "0.0", "acctrl_mode": "Off"}
TARGETS = SOLINTEG_PHASES.names()
PHASED = SOLINTEG_CONTROL.with_phases(True)
PHASED_STATES = {**STATES, **dict.fromkeys(TARGETS, "0.0"), "unbalanced_output": "on"}


@dataclass
class Inverter(FakeBattery):
    """A Solinteg hybrid as the plugin shows it. In EMS BattCtrl the battery follows its target (negative charges);
    in EMS ACCtrl with the scheduling mode per phase and unbalanced output on, the inverter's AC power on each phase
    follows that phase's target; in General it covers the house. It keeps whatever it was last told: no dead-man."""

    house_a: list[float] = field(default_factory=lambda: [2.0, 3.0, 2.0])
    #: The dongle does not answer: every entity keeps the value it had (the plugin's numbers and selects are always
    #: available), and what is written meanwhile shows only once it answers again.
    stalled: bool = False
    shown: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.shown = dict(self.states)

    def reads(self) -> dict[str, Any]:
        reads = super().reads()
        if self.stalled:
            return {name: replace(read, state=self.shown.get(name, read.state)) for name, read in reads.items()}
        self.shown = dict(self.states)
        return reads

    def apply(self, op: Any) -> bool:
        taken = super().apply(op)
        if op.method == "number" and op.role in self.states and op.role not in self.drops and op.role not in self.raises:
            self.states[op.role] = str(float(op.value))  # the plugin publishes the float it was given
        return taken

    def management(self) -> Management:
        mode = self.shown.get("working_mode", self.states["working_mode"]) if self.stalled else self.states["working_mode"]
        if mode == "General":
            return Management("free", "self_consumption", {"working mode": mode})
        return Management("self_managed", f"working mode:{mode}", {"working mode": mode})

    def number(self, name: str) -> float:
        return float(self.states.get(name, 0))

    def plant_kw(self) -> tuple[float, float, float]:
        """The inverter's AC power per phase (kW, + output). A BattCtrl charge is taken as all on L1: nothing says
        how the inverter spreads it."""
        mode = self.states["working_mode"]
        if mode == "EMS ACCtrl" and self.states["acctrl_mode"] == "Power Setting on each Phase" and self.obeys:
            return (self.number(TARGETS[0]), self.number(TARGETS[1]), self.number(TARGETS[2]))
        if mode == "EMS BattCtrl" and self.obeys:
            return (self.number("battery_target"), 0.0, 0.0)
        return (0.0, 0.0, 0.0)

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        """The battery's power (kW, + charging) from what the inverter holds, whatever the executor thinks."""
        if not self.obeys:
            return self.idle_kw
        mode = self.states["working_mode"]
        if mode in ("EMS BattCtrl", "EMS ACCtrl"):
            return -sum(self.plant_kw())
        return -2.0

    def phase_a(self, house_a: list[float] | None = None) -> list[float]:
        """Each phase's current: the house and what the inverter takes on it."""
        house = self.house_a if house_a is None else house_a
        return [amps + max(-power, 0.0) * 1000 / VOLTS for amps, power in zip(house, self.plant_kw(), strict=True)]

    def look(self, now: datetime, action: str | None, rate_kw: float, **overrides: Any) -> ExecInputs:
        """A look with the fuse's headroom per phase from the house and the inverter."""
        plant = self.plant_kw()
        headroom = [FUSE_A - amps for amps in self.phase_a()]
        charge = max(-sum(plant), 0.0)
        base = ExecInputs(
            now=now, facts=self.facts(self.power_kw()), reads=self.reads(), action=action, rate_kw=rate_kw,
            battery_age_s=5.0, measurement_ok=True, headroom_a=min(headroom),
            grid_import_kw=sum(self.house_a) * VOLTS / 1000 + charge,
            phase_headroom_a=(headroom[0], headroom[1], headroom[2]), plant_phase_kw=plant,
        )
        return replace(base, **overrides)


def _plant(control=SOLINTEG_CONTROL, **kwargs: Any) -> tuple[Executor, Inverter]:
    states = dict(PHASED_STATES if control.phased else STATES)
    return Executor(control), Inverter(control, states, **kwargs)


def _caps(peaks: dict[str, float] | None = PEAKS, cars_a: float = 0.0) -> tuple[float, float, float]:
    return grid_charge_phase_caps_kw(FUSE_A, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=cars_a, house_peak_kw=None,
                                     house_phase_peaks_a=peaks)


def _run(executor: Executor, plant: Inverter, start: datetime, looks: int, action: str | None, rate_kw: float = 0.0,
         **overrides: Any) -> tuple[datetime, list[StepResult]]:
    """Looks on the site's own phases (`Inverter.look`), each step's writes made before the next."""
    now = start
    results: list[StepResult] = []
    failed: tuple[str, ...] = ()
    for _ in range(looks):
        result = executor.step(plant.look(now, action, rate_kw, failed=failed, **overrides))
        results.append(result)
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        now += TICK
    return now, results


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_the_working_mode_and_the_battery_s_target_negative_for_a_charge() -> None:
    control = SOLINTEG_CONTROL
    assert control_for("solinteg") is control and control in CONTROLS and control.adapter is SOLINTEG
    assert adapter_for("solax_modbus", "Gabriel C.") is SOLINTEG and control_for("solax") is SOLAX_CONTROL
    assert control.wanted("hold", 0.0) == {"working_mode": "EMS BattCtrl", "battery_target": 0.0}
    assert control.wanted("grid_charge", 3.456) == {"working_mode": "EMS BattCtrl", "battery_target": -3.45}
    assert control.wanted("export_discharge", 2.0) == {"working_mode": "EMS BattCtrl", "battery_target": 2.0}
    # No EMS mode covers the house: that is the plant's own mode, and so is normal.
    assert control.wanted("discharge_house", 2.0) == {"working_mode": "General", "battery_target": 0.0}
    assert control.wanted("self_consume", 0.0) == {}
    assert NORMAL["solinteg"] == {"working_mode": "General", "battery_target": 0.0, **dict.fromkeys(TARGETS, 0.0)}
    rows = [row for row in table() if row["brand"] == "solinteg"]
    assert {row["action"] for row in rows} == {"hold", "grid_charge", "discharge_house", "export_discharge", "self_consume"}
    # The hold leaves the battery idle (it stores no sun): a hold that charges did not take.
    assert control.variant_names() == ("battctrl_idle",) and control.hold_idle()
    # Taken only in General, and its own modes are not "the plant's own management" while it runs.
    assert [rule.free for rule in SOLINTEG.modes] == [frozenset({"General"})]
    assert control.own_states == {"working mode": frozenset({"EMS BattCtrl", "EMS ACCtrl"})}
    assert control.requires == (("working_mode", frozenset({"General"}), "not_in_general"),)


def test_every_role_is_found_by_the_plugins_key_and_no_other_plugins_entity() -> None:
    roles = {role.name: role for role in (*PHASED.roles,)}
    assert set(roles) == set(KEYS)
    for name, (domain, unique_id) in KEYS.items():
        assert roles[name].match.matches(domain, unique_id), name
        other = "number" if domain == "select" else "select"
        assert not roles[name].match.matches(other, unique_id), name
        assert [key for key, role in roles.items() if role.match.matches(domain, unique_id)] == [name]
    # The inverter-side AC caps, the total AC schedule and the site's import limit are no role: never written.
    for domain, unique_id in (
        ("number", "solinteg_ems_battctrl_min_ac_power_limit"), ("number", "solinteg_ems_battctrl_max_ac_power_limit"),
        ("number", "solinteg_ems_acctrl_total_power"), ("number", "solinteg_import_limit_value"),
        ("select", "solinteg_import_limit_switch"), ("number", "solinteg_peak_shaving_max_grid_import_power"),
        ("select", "solinteg_ems_battctrl_priority_power_output"), ("select", "solinteg_charger_use_mode"),
    ):
        assert not any(role.match.matches(domain, unique_id) for role in roles.values()), unique_id
    # SolaX's own remote control is another plugin's and another recipe.
    assert not any(role.match.matches("select", "solax_remotecontrol_power_control") for role in roles.values())
    # The list the card offers to enable: every role, in order, and nothing else.
    assert optin.ENABLE_KEYS["solinteg"] == (
        ("select", "working_mode"), ("number", "battery_charge_discharge_power_target"),
        ("select", "ems_acctrl_mode"), ("select", "grid_unbalanced_output"), ("number", "ems_acctrl_phase_a_power"),
        ("number", "ems_acctrl_phase_b_power"), ("number", "ems_acctrl_phase_c_power"),
    )
    for role in roles.values():
        assert any(role.match.matches(domain, f"hub_{key}") for domain, key in optin.ENABLE_KEYS["solinteg"])
    # The balanced control needs only the working mode and the target: the per-phase scheduling mode is the phased
    # control's, so a plant without it shows no missing control.
    assert [role.name for role in SOLINTEG_CONTROL.roles] == ["working_mode", "battery_target"]


def test_no_dead_man_is_counted_on_and_the_caps_are_the_cautious_ones() -> None:
    control = SOLINTEG_CONTROL
    # No watchdog to renew: the command stays, so only the HA-down cap bounds a charge.
    assert control.watchdog_roles == () and PHASED.watchdog_roles == ()
    # The site import limit is never the guard, and the whole charge is taken as on one phase.
    assert control.import_limit is None and control.grid_charge_phases == 1 and control.output_type is None
    # Setpoints 500 a day (nobody says whether they are kept in flash), settings 96.
    assert control.write_cap("setpoint") == 500 and control.write_cap("setting") == 96
    classes = {role.name: role.write_class for role in PHASED.roles}
    assert classes == {
        "working_mode": "setting", "acctrl_mode": "setting", "unbalanced_output": "setting",
        "battery_target": "setpoint", **dict.fromkeys(TARGETS, "setpoint"),
    }
    # The dongle stalls: a read-back may come later than on any other brand.
    assert control.readback_s == READBACK_STALL_S == 180.0 > READBACK_DEADLINE_S
    # Only a cloud waits longer (Tesla's).
    assert all(other.readback_s is None for other in CONTROLS if other is not control and not other.slow)


# --- the writes and their read-back ----------------------------------------------------------------------------------


def test_a_hold_writes_the_mode_alone_and_a_charge_the_mode_then_the_target_each_read_back() -> None:
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 6, "hold")
    assert results[0].persist_first
    assert written(results) == [("working_mode", "EMS BattCtrl")], "the target reads 0: not written"
    assert executor.state.phase == "active" and plant.power_kw() == 0.0
    assert executor.state.setpoint_writes_today == 0 and executor.state.writes_today == 1
    # The plan changes to a grid charge: the target alone.
    now, results = run(executor, plant, now, 6, "grid_charge", 3.0)
    assert written(results) == [("battery_target", -3.0)] and plant.power_kw() == 3.0
    # From nothing: the mode first (a value written before the switch may not act), then the target.
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    assert written(results) == [("working_mode", "EMS BattCtrl"), ("battery_target", -3.0)]
    assert [len(result.ops) for result in results[:3]] == [1, 1, 0], "one write a look, each read back first"
    assert len(events(results, "confirmed")) == 2
    assert executor.state.setpoint_writes_today == 1 and executor.state.writes_today == 1
    # What it would write is what the dry run shows, and what handing back would.
    seen = preview(SOLINTEG_CONTROL, _plant()[1].look(T0, "grid_charge", 3.0))
    assert [(op.role, op.value) for op in seen.writes] == written(results)
    assert [(op.role, op.value) for op in seen.restores] == [("working_mode", "General"), ("battery_target", 0.0)]
    # A lower rate is written at once.
    now, results = run(executor, plant, now, 3, "grid_charge", 1.5)
    assert written(results) == [("battery_target", -1.5)]
    # A discharge to the grid is the target the other way.
    executor, plant = _plant()
    _now, results = run(executor, plant, T0, 6, "export_discharge", 2.0)
    assert written(results) == [("working_mode", "EMS BattCtrl"), ("battery_target", 2.0)]


def test_a_discharge_to_the_house_is_the_plants_own_mode_and_writes_nothing() -> None:
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 6, "discharge_house", 2.0)
    assert written(results) == [] and executor.state.phase == "active"
    # After a charge it writes the mode back first, then the target to 0.
    now, _results = run(executor, plant, now, 6, "grid_charge", 3.0)
    now, results = run(executor, plant, now, 6, "discharge_house", 2.0)
    assert written(results) == [("working_mode", "General"), ("battery_target", 0.0)]
    assert executor.state.phase == "active" and executor.state.action == "discharge_house"


def test_it_starts_only_in_general_and_never_over_a_target_left_standing() -> None:
    executor, plant = _plant()
    plant.states["working_mode"] = "Economic"
    _now, results = run(executor, plant, T0, 2, "hold")
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "self_managed:working mode:Economic"
    # Someone else's EMS control is not taken either.
    plant.states["working_mode"] = "EMS BattCtrl"
    _now, results = run(executor, plant, T0, 2, "hold")
    assert written(results) == [] and executor.state.phase == "idle"
    # A target left by someone would run between the mode's write and the target's own.
    executor, plant = _plant()
    plant.states["battery_target"] = "-5.0"
    _now, results = run(executor, plant, T0, 2, "grid_charge", 3.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "stale_target:battery_target"
    plant.states["battery_target"] = "0.0"
    _now, results = run(executor, plant, T0, 4, "grid_charge", 3.0)
    assert executor.state.phase == "active"


# --- the hand-back -----------------------------------------------------------------------------------------------------


def test_the_hand_back_writes_general_first_then_the_target_to_zero_each_read_back() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    now, results = run(executor, plant, now, 6, "self_consume")
    assert written(results) == [("working_mode", "General"), ("battery_target", 0.0)]
    assert [len(result.ops) for result in results[:3]] == [1, 1, 0]
    assert all(op.restore for result in results for op in result.ops)
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot
    assert plant.states == STATES, "as found"
    # A hold wrote only the mode, so only the mode goes back.
    now, _results = run(executor, plant, now + timedelta(seconds=REARM_S), 6, "hold")
    _now, results = run(executor, plant, now, 6, None)
    assert written(results) == [("working_mode", "General")]


def test_a_restart_and_a_stop_hand_back_before_anything_else() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    stored = executor.as_dict()
    again = Executor(
        SOLINTEG_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        ExecState.from_dict(stored["state"]),
    )
    again.restart(now)
    _now, results = run(again, plant, now, 6, "grid_charge", 3.0, stopping="restart")
    assert written(results) == [("working_mode", "General"), ("battery_target", 0.0)]
    assert again.state.phase == "idle" and plant.states == STATES


def test_a_phase_near_the_fuse_hands_back_in_one_look_and_a_holds_peak_is_covered_by_general() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    _now, results = run(executor, plant, now, 1, "grid_charge", 3.0, headroom_a=0.5)
    assert written(results) == [("working_mode", "General"), ("battery_target", 0.0)], "both at once, no read-back awaited"
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    # Under a hold a peak is covered by the plant's own mode, one write.
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "hold")
    now, results = run(executor, plant, now, 2, "hold", headroom_a=1.2)
    assert written(results) == [("working_mode", "General")] and events(results, "peak_cover")
    assert executor.state.phase == "active" and executor.state.covered_at is not None


def test_a_mode_changed_by_the_app_or_the_cloud_is_given_up_and_never_written_over() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    plant.states["working_mode"] = "Economic"
    _now, results = run(executor, plant, now, 40, "grid_charge", 3.0, management=Management("free", "self_consumption"))
    assert events(results, "hand_back")[0]["reason"] == "changed_outside_spotnav:working_mode"
    assert plant.states["working_mode"] == "Economic", "not fought"
    assert plant.states["battery_target"] == "0.0", "its own target is still taken back"
    assert executor.state.phase == "blocked"


def test_a_hold_that_charges_and_a_charge_that_does_nothing_are_handed_back() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "hold")
    _now, results = run(executor, plant, now, 10, "hold", power_kw=1.5)
    assert events(results, "hand_back")[0]["reason"] == "not_obeyed:hold:charging"
    assert plant.states["working_mode"] == "General"
    executor, plant = _plant(obeys=False)
    _now, results = run(executor, plant, T0, 80, "grid_charge", 3.0)
    assert events(results, "hand_back")[0]["reason"] == "no_effect:grid_charge" and executor.state.phase == "blocked"
    assert plant.states == STATES


# --- the fuse with Home Assistant stopped ----------------------------------------------------------------------------


def test_a_balanced_charge_keeps_the_fuse_when_home_assistant_stops_even_all_on_one_phase() -> None:
    """The inverter keeps its target (no dead-man), and nothing says how it spreads a BattCtrl charge over the
    phases: the cap takes it as all on the phase where the house peaks."""
    phases = SOLINTEG_CONTROL.grid_charge_phases
    for fuse_a, peaks in ((20.0, PEAKS), (25.0, PEAKS), (25.0, {"L1": 6.0, "L2": 9.0, "L3": 4.0}), (35.0, None)):
        cap = grid_charge_cap_kw(fuse_a, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                                 plant_phases=phases, house_phase_peaks_a=peaks)
        assert cap is not None
        worst_a = max(peaks.values()) if peaks else 4000.0 / VOLTS
        assert worst_a + cap * 1000 / VOLTS <= fuse_a - HEADROOM_MARGIN_A + 1e-9, (fuse_a, peaks, cap)
        # Sigenergy's rule (three phases, evenly) would allow three times as much.
        assert cap == pytest.approx(grid_charge_cap_kw(
            fuse_a, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None, plant_phases=3,
            house_phase_peaks_a=peaks) / 3)
    # The unbalanced house on 20 A leaves the balanced charge nothing at all.
    assert grid_charge_cap_kw(FUSE_A, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                              plant_phases=phases, house_phase_peaks_a=PEAKS) == 0.0
    # The plan's charge capped so, run, and Home Assistant stops: the target stays, the house goes to its peak.
    calm = {"L1": 6.0, "L2": 9.0, "L3": 4.0}
    cap = grid_charge_cap_kw(25.0, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                             plant_phases=phases, house_phase_peaks_a=calm)
    executor, plant = _plant()
    run(executor, plant, T0, 6, "grid_charge", min(12.0, cap))
    charge_a = -float(plant.states["battery_target"]) * 1000 / VOLTS
    assert 13.0 < charge_a <= 14.0
    assert max(calm.values()) + charge_a <= 25.0 - HEADROOM_MARGIN_A, "all of it on the worst phase"
    assert plant.states["working_mode"] == "EMS BattCtrl", "nothing takes it back by itself"


def test_the_live_clamp_takes_the_charge_as_all_on_the_tightest_phase() -> None:
    """While Home Assistant runs, the charge is held under the least headroom counted on one phase, as the HA-down
    cap counts it: never three times that, as a balanced plant would be allowed."""
    room_a = FUSE_A - HEADROOM_MARGIN_A - 12.0
    first: dict[int, float] = {}
    for phases in (SOLINTEG_CONTROL.grid_charge_phases, 3):
        executor, plant = _plant()
        plant.house_a = [12.0, 2.0, 2.0]
        _now, results = _run(executor, plant, T0, 12, "grid_charge", 6.0, charge_phases=phases)
        first[phases] = -next(value for role, value in written(results) if role == "battery_target") * 1000 / VOLTS
        if phases == 1:
            assert max(plant.phase_a()) <= FUSE_A - HEADROOM_MARGIN_A + 1e-6 and executor.state.phase == "active"
    assert first[1] == pytest.approx(room_a, abs=0.5), "the one phase's room, used"
    assert first[3] > FUSE_A - 12.0, "counted as three phases, the first command alone would open the fuse"


def test_per_phase_writes_the_targets_then_the_scheduling_mode_then_the_working_mode() -> None:
    assert SOLINTEG_CONTROL.phased is False and PHASED.phased is True and PHASED.with_phases(False) is SOLINTEG_CONTROL
    assert SOLINTEG_PHASES.target_kind == "plant" and SOLINTEG_PHASES.charge_sign == -1.0
    assert PHASED.wanted("grid_charge", 9.0, phases=(1.019, 0.0, 0.138)) == {
        "battery_target": 0.0, "acctrl_mode": "Power Setting on each Phase", "working_mode": "EMS ACCtrl",
        "phase_l1_target": -1.01, "phase_l2_target": 0.0, "phase_l3_target": -0.13,
    }
    for action in ("hold", "discharge_house", "export_discharge", "grid_charge"):
        assert PHASED.wanted(action, 2.0) == {**SOLINTEG_CONTROL.wanted(action, 2.0), **dict.fromkeys(TARGETS, 0.0)}
    row = next(row for row in phase_table() if row["brand"] == "solinteg")
    assert row["writes"]["phase_l1_target"] == -1.0 and row["requires"] == {"unbalanced_output": ["on"]}
    executor, plant = _plant(PHASED)
    now, results = _run(executor, plant, T0, 8, "grid_charge", 12.0, phase_caps_kw=_caps())
    assert written(results) == [
        ("phase_l1_target", -0.92), ("acctrl_mode", "Power Setting on each Phase"), ("working_mode", "EMS ACCtrl"),
    ], "whole amps: L1 4 A, L2 and L3 none; the mode finds the targets in place"
    assert [len(result.ops) for result in results[:4]] == [1, 1, 1, 0]
    assert executor.state.phase_kw == (0.92, 0.0, 0.0) and plant.plant_kw() == (-0.92, 0.0, 0.0)
    assert executor.state.setpoint_writes_today == 1 and executor.state.writes_today == 2
    # The plan changes to a hold: the working mode first (one write stops all three phases), then the target.
    now, results = _run(executor, plant, now, 8, "hold", phase_caps_kw=_caps())
    assert written(results) == [("working_mode", "EMS BattCtrl"), ("phase_l1_target", 0.0)]
    assert plant.power_kw() == 0.0
    # And back to a charge per phase, then handed back: General first, the targets to 0; the scheduling mode stays.
    now, _results = _run(executor, plant, now, 8, "grid_charge", 12.0, phase_caps_kw=_caps())
    assert plant.states["working_mode"] == "EMS ACCtrl"
    now, results = _run(executor, plant, now, 8, "self_consume", phase_caps_kw=_caps())
    assert written(results) == [("working_mode", "General"), ("phase_l1_target", 0.0)]
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot
    assert plant.states == {**PHASED_STATES, "acctrl_mode": "Power Setting on each Phase"}


def test_an_unbalanced_house_stays_under_the_fuse_on_every_phase_when_home_assistant_stops() -> None:
    """L2 at 18 A on a 20 A fuse: per phase L1 charges. Then Home Assistant stops: the inverter keeps its targets
    and the house goes to its learned peak on every phase at once."""
    executor, plant = _plant(PHASED)
    _run(executor, plant, T0, 8, "grid_charge", 12.0, phase_caps_kw=_caps())
    assert plant.plant_kw() == (-0.92, 0.0, 0.0)
    peak = list(PEAKS.values())
    for house in (peak, [peak[0], 0.0, 0.0], [0.0, peak[1], 0.0], [0.0, 0.0, peak[2]], [peak[0], peak[1], 0.0]):
        amps = plant.phase_a(house)
        assert max(amps) <= FUSE_A - HEADROOM_MARGIN_A + 1e-9, (house, amps)
    amps = plant.phase_a(peak)
    assert amps[0] == pytest.approx(13.6 + 920 / VOLTS, abs=0.02) and amps[1] == 18.0 and amps[2] == 17.4
    # Any plan power, any house within its peaks while the targets were set, with and without a car.
    for cars_a in (0.0, 2.0, 4.0):
        for rate in (0.6, 1.0, 5.0, 25.0):
            for house_now in ([0.0, 0.0, 0.0], [13.0, 2.0, 2.0], [2.0, 17.5, 2.0], [2.0, 2.0, 17.0]):
                executor, plant = _plant(PHASED, house_a=list(house_now))
                _run(executor, plant, T0, 8, "grid_charge", rate, phase_caps_kw=_caps(cars_a=cars_a))
                targets = plant.plant_kw() if plant.states["working_mode"] == "EMS ACCtrl" else (0.0, 0.0, 0.0)
                stopped = [amps + cars_a for amps in plant.phase_a(peak)]
                charged = [amps for amps, target in zip(stopped, targets, strict=True) if target != 0.0]
                assert all(amps <= FUSE_A - HEADROOM_MARGIN_A + 1e-9 for amps in charged), (
                    cars_a, rate, house_now, stopped)
                assert all(target <= 0.0 for target in targets), "never a discharge"
                assert plant.number("battery_target") == 0.0, "the battery's own target stays 0 beside them"


def test_per_phase_needs_unbalanced_output_on_and_a_phase_over_its_target_is_handed_back() -> None:
    executor, plant = _plant(PHASED)
    plant.states["unbalanced_output"] = "off"
    _now, results = _run(executor, plant, T0, 2, "grid_charge", 12.0, phase_caps_kw=_caps())
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "unbalanced_output_off"
    assert all(role.name != "unbalanced_output" or not role.restored for role in PHASED.roles)
    # A runaway phase: the mode back to General, at once.
    executor, plant = _plant(PHASED)
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 12.0, phase_caps_kw=_caps())
    _now, results = _run(executor, plant, now, 12, "grid_charge", 12.0, phase_caps_kw=_caps(),
                         plant_phase_kw=(-2.5, 0.0, 0.0))
    assert events(results, "hand_back")[0]["reason"] == "not_obeyed:grid_charge:phase_over"
    assert plant.states["working_mode"] == "General" and plant.number(TARGETS[0]) == 0.0


def test_the_plugin_reads_no_power_per_phase_of_the_inverter_so_the_shell_cannot_run_per_phase_yet() -> None:
    assert SOLINTEG.plant_phase_power is None
    assert SIGENERGY_CONTROL.adapter.plant_phase_power is not None


# --- the dongle's stalls -----------------------------------------------------------------------------------------------


def test_a_read_back_that_comes_a_stall_late_is_still_a_write_taken() -> None:
    """The dongle stops answering: what was written shows only once it answers again. The ledger's own 60 s would
    call the write not taken; Solinteg's wait is three minutes."""
    executor, plant = _plant()
    plant.stalled = True
    now, results = run(executor, plant, T0, 9, "grid_charge", 3.0)
    assert written(results) == [("working_mode", "EMS BattCtrl")] and executor.state.pending == "working_mode"
    assert (now - T0).total_seconds() > READBACK_DEADLINE_S and not events(results, "not_taken")
    plant.stalled = False
    now, results = run(executor, plant, now, 4, "grid_charge", 3.0)
    assert events(results, "confirmed")[0]["reason"] == "working_mode"
    assert written(results) == [("battery_target", -3.0)] and executor.state.phase == "active"
    # The same late read-back on a brand without the longer wait is a write not taken.
    other = Executor(SIGENERGY_CONTROL)
    sigen = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), drops={"discharge_limit"})
    _now, results = run(other, sigen, T0, 9, "hold")
    assert events(results, "not_taken")[0]["why"] == "no_read_back"


def test_a_write_never_read_back_is_not_taken_after_the_longer_wait_and_one_that_raised_at_once() -> None:
    executor, plant = _plant(drops={"working_mode"})
    now, results = run(executor, plant, T0, 17, "grid_charge", 3.0)
    assert not events(results, "not_taken") and (now - T0).total_seconds() < READBACK_STALL_S
    _now, results = run(executor, plant, now, 4, "grid_charge", 3.0)
    assert events(results, "not_taken")[0]["why"] == "no_read_back"
    assert events(results, "hand_back")[0]["reason"] == "write_not_taken:working_mode:no_read_back"
    assert written(results) == [], "nothing was taken, so nothing is written back"
    assert executor.state.phase == "blocked" and plant.states == STATES
    # The plugin raises when the inverter did not acknowledge the write (a timeout in a stall).
    executor, plant = _plant(raises={"battery_target"})
    _now, results = run(executor, plant, T0, 8, "grid_charge", 3.0)
    assert events(results, "hand_back")[0]["reason"] == "write_not_taken:battery_target:service_failed"
    assert plant.states["working_mode"] == "General" and executor.state.phase == "blocked"


def test_a_hand_back_or_a_peak_cover_the_dongle_dropped_is_sent_again_after_the_ledgers_own_wait() -> None:
    """Only a write that starts or changes a command waits the stall's three minutes: General is idempotent, and
    the house stands on the grid without the battery until it lands."""
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "hold")
    plant.drops = {"working_mode"}
    now, results = run(executor, plant, now, 2, "hold", headroom_a=1.2)
    assert written(results) == [("working_mode", "General")] and events(results, "peak_cover")
    covered = now
    now, results = run(executor, plant, now, 7, "hold", headroom_a=1.2)
    assert events(results, "not_taken")[0]["why"] == "no_read_back"
    assert (now - covered).total_seconds() <= READBACK_DEADLINE_S + 3 * TICK.total_seconds() < READBACK_STALL_S
    plant.drops = set()
    now, results = run(executor, plant, now, 12, None)
    assert ("working_mode", "General") in written(results) and executor.state.phase != "active"
    assert plant.states["working_mode"] == "General"
    # A plain hand-back dropped once is sent again after the 30 s retry, not after three minutes.
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    plant.drops = {"working_mode"}
    start = now
    now, results = run(executor, plant, now, 7, None)
    assert events(results, "not_taken")[0]["why"] == "no_read_back"
    plant.drops = set()
    now, results = run(executor, plant, now, 6, None)
    assert ("working_mode", "General") in written(results)
    assert plant.states == STATES and (now - start).total_seconds() < READBACK_STALL_S


def test_a_stall_never_delays_the_fuse() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    # A new rate is written and the dongle stalls before it shows; a phase then nears the fuse.
    plant.stalled = True
    now, results = run(executor, plant, now, 2, "grid_charge", 1.5)
    assert executor.state.pending == "battery_target"
    _now, results = run(executor, plant, now, 1, "grid_charge", 1.5, headroom_a=0.5)
    assert ("working_mode", "General") in written(results), "the hand-back does not wait for the read-back"
    assert plant.states["working_mode"] == "General"


# --- the day's writes --------------------------------------------------------------------------------------------------


def test_a_spent_setpoint_budget_hands_a_charge_back_and_leaves_the_hold() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "grid_charge", 3.0)
    executor.state = replace(executor.state, setpoint_writes_today=500)
    # A rate that must fall is never left unwritten: the charge is handed back instead.
    now, results = run(executor, plant, now, 6, "grid_charge", 1.5)
    assert events(results, "hand_back")[0]["reason"] == "setpoint_write_cap"
    assert written(results) == [("working_mode", "General"), ("battery_target", 0.0)], "a restore is never held back"
    assert executor.state.phase == "idle" and executor.state.blocked is None
    now, results = run(executor, plant, now + timedelta(seconds=REARM_S), 3, "grid_charge", 3.0)
    assert written(results) == [] and events(results, "not_started") == []
    _now, results = run(executor, plant, now, 3, "export_discharge", 2.0)
    assert written(results) == []
    # A hold writes the target only to 0, if at all: it still runs.
    now, results = run(executor, plant, now, 6, "hold")
    assert executor.state.phase == "active" and executor.state.action == "hold"
    # The next day the budget is whole again.
    executor, plant = _plant()
    executor.state = replace(executor.state, day=T0.date().isoformat(), setpoint_writes_today=500)
    _now, results = run(executor, plant, T0 + timedelta(days=1), 6, "grid_charge", 3.0)
    assert executor.state.phase == "active" and executor.state.setpoint_writes_today == 1
    # A brand whose rate is a setting is not touched by the setpoints' count.
    other = Executor(SIGENERGY_CONTROL)
    other.state = replace(other.state, day=T0.date().isoformat(), setpoint_writes_today=500)
    sigen = FakeBattery(SIGENERGY_CONTROL, dict(SIGENERGY_STATES), register_40031="Maximum Self Consumption")
    run(other, sigen, T0, 8, "grid_charge", 3.0)
    assert other.state.phase == "active"


# --- the check before control ------------------------------------------------------------------------------------------


def test_the_verification_runs_every_mode_through_the_recipe_and_leaves_the_inverter_as_found() -> None:
    from tests.test_battery_verify import _outcomes, _verify

    executor, plant = _plant()
    verification = _verify(executor, plant)
    assert verification.done
    assert _outcomes(verification) == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"
    }
    report = verification.report()
    assert report["brand"] == "solinteg" and report["modes"]["hold"]["variant"] == "battctrl_idle"
    assert plant.states == STATES and executor.state.phase == "idle" and not executor.state.holds_snapshot
    # An inverter that takes the writes and does nothing fails the check; nothing is left written.
    executor, plant = _plant(obeys=False)
    outcomes = _outcomes(_verify(executor, plant, looks=600))
    assert outcomes["hold"] in ("ignored", "aborted:no_effect:hold") and plant.states == STATES
