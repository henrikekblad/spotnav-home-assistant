"""Victron ESS (`commands.VICTRON_CONTROL`, not tested on a plant): the recipe's data, the balanced actions in Mode 2
(the grid setpoint is the house plus the rate), the charge per phase in external control (a setpoint per phase is
the load behind the Multi plus the charge), the 60 s watchdog's renewal and what it is worth to the gate, the
hand-back, and an unbalanced house that never passes the fuse. The plant here is a model of Victron's own page
"ESS mode 2 and 3" (see `commands.py`), not a recording."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.spotnav.battery import gate, optin
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    SETPOINT_WRITES_WATCHED,
    SETTING_WRITES,
    SIGENERGY_CONTROL,
    VICTRON_CONTROL,
    VICTRON_PHASES,
    VICTRON_RENEW_S,
    VICTRON_SILENCE_S,
    control_for,
    phase_table,
    table,
)
from custom_components.spotnav.battery.executor import (
    HEADROOM_MARGIN_A,
    PHASE_GRID_MARGIN_A,
    PHASE_TRACK_S,
    ExecInputs,
    Executor,
    Op,
    RoleRead,
    StepResult,
)
from custom_components.spotnav.battery.facts import BatteryFacts, Management
from custom_components.spotnav.battery.ledger import MIN_DIVERGENCE_S
from custom_components.spotnav.battery.monitor import Capability
from custom_components.spotnav.battery.verify import Sample, Verification

T0 = datetime.fromisoformat("2026-11-18T02:00:00+01:00")
TICK = timedelta(seconds=10)
VOLTS = 400.0 / 3 ** 0.5
FUSE_A = 20.0
TARGETS = ("phase_l1_target", "phase_l2_target", "phase_l3_target")
#: The owner's house (2026-11-18): the learned peaks on a 20 A fuse.
PEAKS = {"L1": 13.6, "L2": 18.0, "L3": 17.4}


def _caps(peaks: dict[str, float] = PEAKS, **kwargs: Any) -> tuple[float, float, float]:
    return gate.grid_charge_phase_caps_kw(
        FUSE_A, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None, house_phase_peaks_a=peaks, **kwargs
    )


def _shipped(peaks: dict[str, float] = PEAKS) -> tuple[float, float, float]:
    """The caps the recipe as shipped gets: its dead-man is not stated, so the house counts at its peak."""
    return _caps(peaks, silence_fallback_s=VICTRON_PHASES.silence_fallback_s)


def _lifted() -> tuple[float, float, float]:
    """The caps a recipe with a dead-man seen on a plant would get (the fuse less the margin on each phase). No
    recipe ships it; the executor does not know where its caps come from, so these exercise its live clamp alone."""
    return _caps(silence_fallback_s=VICTRON_SILENCE_S)


@dataclass
class Plant:
    """A three-phase Victron ESS as the page describes it. In its own modes the GX holds the site's total at the
    grid setpoint, within the charge and discharge power, spread evenly, and writes the setpoints per phase itself.
    In external control each Multi holds its AC input at its setpoint, the GX's last one until SpotNav writes its
    own, for 60 s after the last write of it; then it is in passthru (`fallback` false: a firmware or a flow on the
    GX where that never happens, the setpoints stand). `out_kw`: the loads on the Multis' AC outputs; `beside_kw`:
    the loads wired beside them."""

    out_kw: list[float] = field(default_factory=lambda: [0.3, 0.2, 1.0])
    beside_kw: list[float] = field(default_factory=lambda: [0.5, 0.5, 0.5])
    states: dict[str, str] = field(default_factory=lambda: {
        "charge_power": "-1", "discharge_power": "-1", "grid_setpoint": "50", "ess_mode": "phase_compensation_enabled",
        "disable_charge": "off", "disable_feed_in": "off",
    })
    written_at: dict[str, datetime] = field(default_factory=dict)
    writes: list[tuple[datetime, Op]] = field(default_factory=list)
    rating_kw: float = 10.0
    fallback: bool = True

    @property
    def external(self) -> bool:
        return self.states["ess_mode"] == "external_control"

    def input_kw(self, now: datetime) -> list[float]:
        """The power on each Multi's AC input (+ from the grid)."""
        if self.external:
            out: list[float] = []
            for index, name in enumerate(TARGETS):
                # Until a setpoint is written, the GX's last one, written as external control was entered.
                at = self.written_at.get(name, self.written_at.get("ess_mode"))
                fresh = at is not None and (not self.fallback or (now - at).total_seconds() <= VICTRON_SILENCE_S)
                out.append(float(self.states.get(name, "0")) / 1000.0 if fresh else self.out_kw[index])
            return out
        limit = {name: float(self.states[name]) for name in ("charge_power", "discharge_power")}
        most = self.rating_kw if limit["charge_power"] < 0 else limit["charge_power"] / 1000.0
        least = -(self.rating_kw if limit["discharge_power"] < 0 else limit["discharge_power"] / 1000.0)
        house = sum(self.out_kw) + sum(self.beside_kw)
        battery = min(max(float(self.states["grid_setpoint"]) / 1000.0 - house, least), most)
        return [load + battery / 3 for load in self.out_kw]

    def settle(self, now: datetime) -> None:
        """Outside external control the GX writes the setpoints per phase itself."""
        if not self.external:
            for name, power in zip(TARGETS, self.input_kw(now), strict=True):
                self.states[name] = f"{round(power * 1000)}"

    def grid_kw(self, now: datetime) -> list[float]:
        return [beside + power for beside, power in zip(self.beside_kw, self.input_kw(now), strict=True)]

    def amps(self, now: datetime) -> list[float]:
        return [power * 1000.0 / VOLTS for power in self.grid_kw(now)]

    def battery_kw(self, now: datetime) -> float:
        return sum(power - load for power, load in zip(self.input_kw(now), self.out_kw, strict=True))

    def apply(self, now: datetime, op: Op) -> None:
        self.writes.append((now, op))
        self.written_at[op.role] = now
        if op.method == "switch":
            self.states[op.role] = "on" if op.value is True else "off"
        elif op.method == "number":
            self.states[op.role] = f"{float(op.value):g}"
        else:
            self.states[op.role] = str(op.value)

    def reads(self, executor: Executor, now: datetime) -> dict[str, RoleRead]:
        sensors = {
            **{f"phase_l{n + 1}_load": f"{load * 1000:.1f}" for n, load in enumerate(self.out_kw)},
            **{f"phase_l{n + 1}_input": f"{power * 1000:.1f}" for n, power in enumerate(self.input_kw(now))},
        }
        return {
            role.name: RoleRead(
                entity_id=f"{role.method}.victron_{role.name}", state=(self.states | sensors).get(role.name),
                found=True, enabled=True, step=role.step, device_id="GX",
            )
            for role in executor.control.roles
        }

    def inputs(self, executor: Executor, now: datetime, action: str | None, rate_kw: float = 0.0, **more: Any) -> ExecInputs:
        self.settle(now)
        mode = "external_control" if self.external else "optimized_battery_life"
        management = Management(
            "self_managed" if self.external else "free", f"ESS mode:{mode}", {"ESS mode": mode, "Dynamic ESS": "off"}
        )
        power, out, grid = self.input_kw(now), self.out_kw, self.grid_kw(now)
        headroom = tuple(FUSE_A - amps for amps in self.amps(now))
        facts = BatteryFacts(
            battery_entity_id="sensor.battery_power", integration="victron_gx", brand="victron", soc_pct=50.0,
            power_kw=self.battery_kw(now), rated_charge_kw=self.rating_kw, rated_discharge_kw=self.rating_kw,
            firmware="v3.60", on_grid=True, management=management,
        )
        values: dict[str, Any] = {
            "battery_age_s": 5.0, "measurement_ok": True, "headroom_a": min(headroom), "grid_import_kw": sum(grid),
            "phase_headroom_a": headroom, "grid_phase_kw": tuple(grid),
            "plant_phase_kw": tuple(load - value for load, value in zip(out, power, strict=True)),
            "phase_behind_kw": tuple(out), "phase_point_kw": tuple(power),
        } | more
        return ExecInputs(now=now, facts=facts, reads=self.reads(executor, now), action=action, rate_kw=rate_kw, **values)


def _run(
    executor: Executor, plant: Plant, now: datetime, looks: int, action: str | None, rate_kw: float = 0.0, **more: Any
) -> tuple[datetime, list[StepResult]]:
    results: list[StepResult] = []
    for _ in range(looks):
        result = executor.step(plant.inputs(executor, now, action, rate_kw, **more))
        for op in result.ops:
            plant.apply(now, op)
        results.append(result)
        assert max(plant.amps(now)) <= FUSE_A + 1e-6, (now, plant.amps(now))
        now += TICK
    return now, results


def _written(results: list[StepResult]) -> list[tuple[str, Any]]:
    return [(op.role, op.value) for result in results for op in result.ops]


def _events(results: list[StepResult], kind: str) -> list[dict[str, Any]]:
    return [event for result in results for event in result.events if event["kind"] == kind]


def _phased() -> Executor:
    executor = Executor(VICTRON_CONTROL)
    assert executor.use_phases(True)
    return executor


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_names_the_integrations_entities_units_and_signs() -> None:
    assert control_for("victron") is not None and control_for("victron_modbus") is None
    assert VICTRON_CONTROL.adapter.platforms == ("victron_gx", "victron_mqtt")
    phased = VICTRON_CONTROL.with_phases(True)
    keys = {
        "charge_power": ("number", "system_ess_max_charge_power"),
        "discharge_power": ("number", "system_ess_max_inverter_power_limit"),
        "grid_setpoint": ("number", "system_ac_power_set_point"),
        "ess_mode": ("select", "system_ess_mode"),
        "disable_charge": ("switch", "vebus_hub4_disable_charge"),
        "disable_feed_in": ("switch", "vebus_hub4_disable_feed_in"),
        **{f"phase_l{n}_target": ("number", f"vebus_ac_power_setpoint_l{n}") for n in (1, 2, 3)},
        **{f"phase_l{n}_load": ("sensor", f"vebus_inverter_output_power_l{n}") for n in (1, 2, 3)},
        **{f"phase_l{n}_input": ("sensor", f"vebus_inverter_input_power_l{n}") for n in (1, 2, 3)},
    }
    assert {role.name for role in phased.roles} == set(keys)
    for role in phased.roles:
        domain, key = keys[role.name]
        # `<installation>_<device>_<short id>`, as `victron_gx` builds it.
        assert role.match.matches(domain, f"c0619ab12345_vebus_276_{key}"), role.name
        assert (domain, key) in optin.ENABLE_KEYS["victron"]
    # Watts, and positive takes from the grid (the page: "Positive: take power from grid").
    assert all(role.scale == 1000.0 for role in phased.roles if role.method in ("number", "sensor"))
    assert VICTRON_PHASES.target_kind == "grid" and VICTRON_PHASES.charge_sign == 1.0
    assert VICTRON_PHASES.value(0, 1.0, 0.4) == 1400.0 and VICTRON_PHASES.value(0, -1.0, 0.4) == -600.0
    # Never beyond the entities' range: a write past it would be refused.
    assert VICTRON_PHASES.value(0, 9.0, 4.0) == 10000.0 and VICTRON_PHASES.value(0, -12.0, 0.5) == -10000.0
    assert VICTRON_CONTROL.wanted("grid_charge", 6.0, house_kw=7.0)["grid_setpoint"] == 10000.0
    assert VICTRON_CONTROL.wanted("export_discharge", 12.0, house_kw=1.0)["grid_setpoint"] == -10000.0
    # The ESS mode select is not the derived "ESS mode" the adapter's rule reads.
    assert not phased.role("ess_mode").match.matches("select", "x_system_0_system_ess_batterylife_state")


def test_the_setpoints_are_setpoints_with_a_cap_that_fits_a_minute_and_the_rest_are_settings() -> None:
    phased = VICTRON_CONTROL.with_phases(True)
    assert {role.name for role in phased.roles if role.write_class == "setpoint"} == set(TARGETS)
    assert phased.write_cap("setpoint") == SETPOINT_WRITES_WATCHED == 3000 > 24 * 60
    assert phased.write_cap("setting") == SETTING_WRITES == 96
    # Sigenergy's caps are its own.
    assert SIGENERGY_CONTROL.write_cap("setpoint") == 500


def test_the_watchdog_is_recipe_data_and_only_the_phased_control_has_it() -> None:
    # The page's 60 s fallback is not stated until a field test has seen it: the HA-down cap is not lifted on it.
    assert VICTRON_PHASES.silence_fallback_s is None and VICTRON_SILENCE_S == 60
    assert _shipped() == _caps() and _shipped() != _lifted()
    assert VICTRON_PHASES.watchdog_renew_s == VICTRON_RENEW_S
    # Three roles, one written a look: each is written again within the renewal plus three looks.
    assert VICTRON_RENEW_S + 3 * TICK.total_seconds() < VICTRON_SILENCE_S
    assert VICTRON_CONTROL.watchdog_roles == () and VICTRON_CONTROL.watchdog_renew_s is None
    phased = VICTRON_CONTROL.with_phases(True)
    assert phased.watchdog_roles == TARGETS and phased.watchdog_renew_s == VICTRON_RENEW_S
    assert phased.own_states == {"ESS mode": frozenset({"external_control"})} and VICTRON_CONTROL.own_states == {}
    # No recipe says its plant falls back by itself.
    assert [c.brand for c in CONTROLS if c.phase_control and c.phase_control.silence_fallback_s] == []
    assert SIGENERGY_CONTROL.phase_control is not None and SIGENERGY_CONTROL.phase_control.park


def test_the_tables_show_every_action_and_the_phases() -> None:
    rows = {row["action"]: row["writes"] for row in table() if row["brand"] == "victron"}
    assert rows["hold"] == {"discharge_power": 0.0}
    assert rows["discharge_house"] == {"charge_power": 0.0}
    assert rows["grid_charge"] == {"charge_power": 5000.0, "grid_setpoint": 5000.0}
    assert rows["export_discharge"] == {"grid_setpoint": -5000.0}
    row = next(row for row in phase_table() if row["brand"] == "victron")
    assert row["writes"] == {
        "ess_mode": "external_control", "disable_charge": False, "disable_feed_in": False,
        "phase_l1_target": 1000.0, "phase_l2_target": 1000.0, "phase_l3_target": 1000.0,
    }


def test_the_targets_are_never_parked_and_never_handed_back() -> None:
    phased = VICTRON_CONTROL.with_phases(True)
    for action in ("hold", "discharge_house", "export_discharge", "self_consume"):
        assert not set(TARGETS) & set(phased.wanted(action, 2.0, house_kw=1.0)), action
    assert phased.wanted("hold", 0.0)["ess_mode"] == "<snapshot>"
    assert phased.restore_order[0] == "ess_mode" and phased.peak_first["grid_charge"] == ("ess_mode",)
    assert not set(TARGETS) & set(phased.restore_order)


# --- Mode 2 ----------------------------------------------------------------------------------------------------------


def test_a_hold_is_the_discharge_power_at_0_and_is_handed_back_as_found() -> None:
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    now, results = _run(executor, plant, T0, 4, "hold")
    assert _written(results) == [("discharge_power", 0.0)]
    assert plant.battery_kw(now) == pytest.approx(0.0)
    now, results = _run(executor, plant, now, 4, "self_consume")
    assert _written(results) == [("discharge_power", -1.0)] and executor.state.phase == "idle"


def test_a_balanced_charge_bounds_the_battery_and_sets_the_grid_at_the_house_plus_the_rate() -> None:
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    house = sum(plant.out_kw) + sum(plant.beside_kw)
    # The battery covers the house when it starts: the house's net load is the import less the battery's power.
    now, results = _run(executor, plant, T0, 5, "grid_charge", 2.0)
    assert _written(results) == [("charge_power", 2000.0), ("grid_setpoint", round((house + 2.0) * 100) * 10.0)]
    assert plant.battery_kw(now) == pytest.approx(2.0, abs=0.02)
    # The house falls: the charge power, not the setpoint, bounds what the battery takes.
    plant.out_kw = [0.0, 0.0, 0.0]
    assert plant.battery_kw(now) == pytest.approx(2.0)
    # The house rises by more than the rate: the battery gives way, and the monitor does not read that as a charge
    # without effect (the setpoint is not moved to follow the house: it is a setting on the GX).
    plant.out_kw = [1.5, 1.5, 1.5]
    now, results = _run(executor, plant, now, 60, "grid_charge", 2.0)
    assert _written(results) == [] and executor.state.phase == "active" and not _events(results, "hand_back")
    # Back: the setpoint first, then the limit.
    now, results = _run(executor, plant, now, 5, "self_consume")
    assert _written(results) == [("grid_setpoint", 50.0), ("charge_power", -1.0)]


def test_a_balanced_charge_does_not_start_without_the_houses_load() -> None:
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    _now, results = _run(executor, plant, T0, 2, "grid_charge", 2.0, grid_import_kw=None)
    assert _written(results) == [] and _events(results, "not_started")[0]["reason"] == "house_unknown"


def test_a_discharge_for_the_house_stores_nothing_and_one_to_the_grid_is_the_house_less_the_rate() -> None:
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    now, results = _run(executor, plant, T0, 3, "discharge_house")
    assert _written(results) == [("charge_power", 0.0)]
    house = sum(plant.out_kw) + sum(plant.beside_kw)
    now, results = _run(executor, plant, now, 4, "export_discharge", 3.0)
    assert _written(results) == [("charge_power", -1.0), ("grid_setpoint", -round((3.0 - house) * 100) * 10.0)]
    assert plant.battery_kw(now) == pytest.approx(-3.0, abs=0.02)


# --- the charge per phase ----------------------------------------------------------------------------------------------


def test_a_charge_per_phase_is_written_as_the_load_behind_each_multi_plus_the_charge() -> None:
    plant, executor = Plant(), _phased()
    now, results = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=_lifted())
    charge = executor.state.phase_kw
    assert charge is not None and sum(charge) == pytest.approx(3.0, abs=0.3)
    writes = _written(results)
    # The ESS mode first: the Multis follow a setpoint only in external control, and the GX writes them until then.
    assert writes[0] == ("ess_mode", "external_control") and [name for name, _ in writes[1:4]] == list(TARGETS)
    for index, (_name, value) in enumerate(writes[1:4]):
        assert value == pytest.approx((plant.out_kw[index] + charge[index]) * 1000.0, abs=10.0)
    # What the battery takes on each phase is the charge, whatever is on the Multi's output.
    taken = [power - load for power, load in zip(plant.input_kw(now), plant.out_kw, strict=True)]
    assert taken == pytest.approx(list(charge), abs=0.011)
    assert executor.state.phase == "active" and not _events(results, "hand_back")


def test_a_target_follows_the_load_behind_it_and_a_load_that_falls_is_no_runaway() -> None:
    plant, executor = Plant(), _phased()
    caps = _lifted()
    now, _ = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=caps)
    before = float(plant.states["phase_l3_target"])
    # The load on L3's output falls by 0.8 kW: the Multi keeps its input, so the battery takes it for a while.
    plant.out_kw[2] -= 0.8
    now, results = _run(executor, plant, now, int(PHASE_TRACK_S / 10) + 4, "grid_charge", 3.0, phase_caps_kw=caps)
    assert ("phase_l3_target", before - 800.0) in _written(results)
    assert executor.state.phase == "active" and not _events(results, "hand_back")
    # It is counted as a setpoint write; the settings' budget is untouched by it.
    assert executor.state.setpoint_writes_today >= 4 and executor.state.writes_today == 1


def test_a_load_that_moves_on_one_phase_rewrites_that_phase_s_target_alone() -> None:
    plant, executor = Plant(), _phased()
    caps = _lifted()
    now, _ = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=caps)
    counted = executor.state.setpoint_writes_today
    # L3's output falls by 0.8 kW, the others wobble by less than the tracking step.
    plant.out_kw = [plant.out_kw[0] + 0.2, plant.out_kw[1] - 0.2, plant.out_kw[2] - 0.8]
    now, results = _run(executor, plant, now, int(PHASE_TRACK_S / 10) + 4, "grid_charge", 3.0, phase_caps_kw=caps)
    assert _events(results, "rate") and executor.state.setpoint_writes_today == counted + 1


def test_each_setpoint_is_written_again_well_inside_the_minute_and_renewals_are_not_counted() -> None:
    plant, executor = Plant(), _phased()
    caps = _lifted()
    now, _ = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=caps)
    counted = executor.state.setpoint_writes_today
    start = len(plant.writes)
    now, results = _run(executor, plant, now, 60, "grid_charge", 3.0, phase_caps_kw=caps)
    for name in TARGETS:
        times = [at for at, op in plant.writes if op.role == name]
        gaps = [(later - earlier).total_seconds() for earlier, later in zip(times, times[1:], strict=False)]
        assert len(gaps) >= 15 and max(gaps) <= 40.0, (name, gaps)
    # Nothing but the three setpoints is renewed, with the values they hold.
    assert {op.role for _at, op in plant.writes[start:]} == set(TARGETS)
    assert executor.state.setpoint_writes_today == counted
    assert _events(results, "renew") and not _events(results, "hand_back")
    # The plant never fell back while Home Assistant ran.
    assert plant.battery_kw(now) == pytest.approx(sum(executor.state.phase_kw or ()), abs=0.02)


def test_the_plant_falls_back_by_itself_when_the_writes_stop() -> None:
    plant, executor = Plant(), _phased()
    now, _ = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=_lifted())
    assert plant.battery_kw(now) > 2.5
    # Home Assistant stops: nothing is written. Within the minute the charge still runs; after it, passthru.
    later = now + timedelta(seconds=VICTRON_SILENCE_S + 10)
    assert plant.battery_kw(later) == pytest.approx(0.0)
    assert plant.grid_kw(later) == pytest.approx([b + o for b, o in zip(plant.beside_kw, plant.out_kw, strict=True)])
    # Back again, the stored state hands the mode back before anything else.
    restarted = Executor(VICTRON_CONTROL, state=type(executor.state).from_dict(executor.state.as_dict()))
    assert restarted.control.phased
    restarted.restart(later)
    _now, results = _run(restarted, plant, later, 4, "grid_charge", 3.0, phase_caps_kw=_lifted())
    assert _written(results)[0] == ("ess_mode", "phase_compensation_enabled")


def test_the_hand_back_is_the_ess_mode_alone_and_the_gx_writing_its_setpoints_after_is_no_ones_change() -> None:
    plant, executor = Plant(), _phased()
    now, _ = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=_lifted())
    now, results = _run(executor, plant, now, 4, "self_consume")
    assert _written(results) == [("ess_mode", "phase_compensation_enabled")]
    assert executor.state.phase == "idle" and executor.ledger.entity_ids() == []
    # The flags and the setpoints are the GX's again: never written to 0, never read as changed by someone else.
    assert plant.states["phase_l1_target"] != "0"


def test_a_change_to_a_hold_leaves_the_setpoints_to_the_gx_without_a_block() -> None:
    plant, executor = Plant(), _phased()
    now, _ = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=_lifted())
    looks = int(MIN_DIVERGENCE_S / 10) + 12
    now, results = _run(executor, plant, now, looks, "hold")
    writes = _written(results)
    # The mode first (it stops the charge on all three phases), then the hold's own limit.
    assert writes[0] == ("ess_mode", "phase_compensation_enabled") and ("discharge_power", 0.0) in writes
    assert not set(TARGETS) & {name for name, _ in writes[1:]}
    assert executor.state.phase == "active" and executor.state.action == "hold" and executor.state.blocked is None
    assert not _events(results, "external") and not _events(results, "hand_back") and not _events(results, "renew")


def test_someone_else_in_external_control_is_not_taken() -> None:
    plant, executor = Plant(), _phased()
    plant.states["ess_mode"] = "external_control"
    _now, results = _run(executor, plant, T0, 2, "grid_charge", 3.0, phase_caps_kw=_lifted())
    assert _written(results) == [] and _events(results, "not_started")[0]["reason"].startswith("self_managed")


def test_a_charge_per_phase_does_not_run_without_the_load_behind_the_multis() -> None:
    plant, executor = Plant(), _phased()
    _now, results = _run(
        executor, plant, T0, 2, "grid_charge", 3.0, phase_caps_kw=_lifted(), phase_behind_kw=None
    )
    assert _written(results) == [] and _events(results, "not_started")[0]["reason"] == "phase_load_unknown"


def test_a_multi_that_takes_more_than_its_setpoint_is_a_runaway_and_one_in_passthru_a_charge_without_effect() -> None:
    caps = _lifted()

    class Greedy(Plant):
        def input_kw(self, now: datetime) -> list[float]:
            power = super().input_kw(now)
            return [value + 1.0 for value in power] if self.external and self.written_at else power

    plant, executor = Greedy(), _phased()
    _now, results = _run(executor, plant, T0, 12, "grid_charge", 3.0, phase_caps_kw=caps)
    assert _events(results, "hand_back")[0]["reason"] == "not_obeyed:grid_charge:phase_over"
    assert executor.state.blocked == "not_obeyed:grid_charge:phase_over"

    plant, executor = Plant(), _phased()
    plant.written_at = _Never()
    _now, results = _run(executor, plant, T0, 40, "grid_charge", 3.0, phase_caps_kw=caps)
    assert _events(results, "hand_back")[0]["reason"] == "no_effect:grid_charge"


# --- the fuse ----------------------------------------------------------------------------------------------------------


def test_the_gate_lifts_the_ha_down_cap_only_for_a_fallback_within_the_minute() -> None:
    kept = _caps()
    # The targets stay: each phase's own peak bounds it (L2 has no room at all on the owner's house).
    assert kept == pytest.approx(tuple((FUSE_A - 2.0 - peak) * VOLTS / 1000 for peak in PEAKS.values()))
    lifted = _caps(silence_fallback_s=VICTRON_SILENCE_S)
    assert lifted == pytest.approx(((FUSE_A - 2.0) * VOLTS / 1000,) * 3)
    for silence in (None, 0, 61, 1800, float("nan"), True):
        assert _caps(silence_fallback_s=silence) == kept, silence
    # The fuse or the chargers unknown: no charge either way.
    assert gate.grid_charge_phase_caps_kw(
        None, 400.0, margin_a=2.0, cars_a=0.0, house_peak_kw=None, silence_fallback_s=60
    ) == (0.0, 0.0, 0.0)
    assert not gate.silence_guards(None) and gate.silence_guards(60) and not gate.silence_guards(60.5)


def test_an_unbalanced_house_never_passes_the_fuse_on_any_phase() -> None:
    # L2 carries 15 A beside the Multi; the battery covers the house when the charge starts.
    plant, executor = Plant(out_kw=[0.3, 0.2, 1.0], beside_kw=[1.0, 3.3, 0.2]), _phased()
    caps = _lifted()
    most = FUSE_A - PHASE_GRID_MARGIN_A + 0.1
    now, results = _run(executor, plant, T0, 12, "grid_charge", 9.0, phase_caps_kw=caps)
    charge = executor.state.phase_kw
    # Each phase is filled to the margin under the fuse and no further: L2, loaded, gets what is left there.
    assert charge is not None and charge[1] < 0.5 < charge[0] and charge[1] < charge[2]
    assert max(plant.amps(now)) <= most and min(plant.amps(now)) >= most - 2.1
    # A kettle beside the Multi on L1: that phase's charge gives way at the next look; nothing is handed back.
    plant.beside_kw[0] += 0.5
    now, results = _run(executor, plant, now, 12, "grid_charge", 9.0, phase_caps_kw=caps)
    lowered = executor.state.phase_kw
    assert lowered is not None and lowered[0] < charge[0] - 0.4 and lowered[1:] == charge[1:]
    assert max(plant.amps(now)) <= most and not _events(results, "hand_back")
    # The oven on L3's own output: the Multi holds its input, so the grid sees nothing; the battery takes less
    # there, and the setpoint stays where it was (within the whole amp a charge is rounded to).
    before = float(plant.states["phase_l3_target"])
    plant.out_kw[2] += 2.0
    now, results = _run(executor, plant, now, 12, "grid_charge", 9.0, phase_caps_kw=caps)
    assert abs(float(plant.states["phase_l3_target"]) - before) <= 231.0 and not _events(results, "hand_back")
    assert (executor.state.phase_kw or (0, 0, 0))[2] == pytest.approx(lowered[2] - 2.0, abs=0.25)
    assert max(plant.amps(now)) <= most and executor.state.phase == "active"
    # A load that takes a phase to its last amp hands the charge back at once: one write, the ESS mode, and the
    # GX covers every phase again. Every look in between stayed under the fuse (`_run` asserts it).
    plant.beside_kw[1] += 1.0
    now, results = _run(executor, plant, now, 3, "grid_charge", 9.0, phase_caps_kw=caps)
    assert _events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert _written(results)[0] == ("ess_mode", "phase_compensation_enabled")


def test_home_assistant_stopping_leaves_no_phase_over_the_fuse_after_the_minute_even_at_the_houses_peak() -> None:
    plant, executor = Plant(out_kw=[0.3, 0.2, 1.0], beside_kw=[1.0, 3.3, 0.2]), _phased()
    now, _ = _run(executor, plant, T0, 12, "grid_charge", 9.0, phase_caps_kw=_lifted())
    # Home Assistant stops and the house goes to its learned peak on every phase.
    plant.out_kw = [0.0, 0.0, 0.0]
    plant.beside_kw = [peak * VOLTS / 1000 for peak in PEAKS.values()]
    later = now + timedelta(seconds=VICTRON_SILENCE_S + 1)
    assert plant.amps(later) == pytest.approx(list(PEAKS.values()))
    assert max(plant.amps(later)) <= FUSE_A


def test_as_shipped_a_plant_whose_fallback_never_acts_stays_under_the_fuse_with_the_house_at_its_peak() -> None:
    # The review's case: a firmware, or a flow on the GX, where the 60 s rule does not act, so the targets stand.
    # The house's learned peak is 10 A on every phase; 1 kW sits behind each Multi when the charge is written.
    peaks = {"L1": 10.0, "L2": 10.0, "L3": 10.0}
    plant, executor = Plant(out_kw=[1.0, 1.0, 1.0], beside_kw=[0.2, 0.2, 0.2], fallback=False), _phased()
    caps = _shipped(peaks)
    now, results = _run(executor, plant, T0, 12, "grid_charge", 9.0, phase_caps_kw=caps)
    charge = executor.state.phase_kw
    assert charge is not None and executor.state.phase == "active" and sum(charge) >= 1.5
    # The cap bounds the whole target that would stand (the load behind plus the charge), not the charge alone.
    for index, name in enumerate(TARGETS):
        assert float(plant.states[name]) / 1000.0 <= caps[index] + 1e-6, name
    # Home Assistant stops. Hours later the house is at its peak on every phase, all of it wired beside the
    # Multis, the loads behind them gone: each phase carries its peak plus the target that stood.
    plant.out_kw = [0.0, 0.0, 0.0]
    plant.beside_kw = [peak * VOLTS / 1000 for peak in peaks.values()]
    later = now + timedelta(hours=3)
    assert plant.battery_kw(later) > 1.5, "the targets still stand"
    assert max(plant.amps(later)) <= FUSE_A - HEADROOM_MARGIN_A + 1e-6


def test_as_shipped_the_owners_house_gets_no_charge_on_a_phase_its_peak_fills() -> None:
    # 20 A, L2's peak 18 A: with the margin nothing is left there, whatever L2 has free now.
    caps = _shipped()
    assert caps[1] == 0.0
    plant, executor = Plant(fallback=False), _phased()
    _now, _results = _run(executor, plant, T0, 12, "grid_charge", 9.0, phase_caps_kw=caps)
    charge = executor.state.phase_kw
    assert charge is None or charge[1] == 0.0


# --- the verification ------------------------------------------------------------------------------------------------


def _verify(executor: Executor, plant: Plant, modes: tuple[str, ...], looks: int = 600) -> Verification:
    now = T0
    verification = Verification(executor, now, firmware="v3.60", modes=modes)
    capability = Capability(rated_charge_kw=plant.rating_kw, rated_discharge_kw=plant.rating_kw)
    for _ in range(looks):
        seen = plant.inputs(executor, now, None)
        deficit = max((seen.grid_import_kw or 0.0) - (seen.facts.power_kw or 0.0), 0.0)
        sample = Sample(
            seen.facts.power_kw, 5.0, 50.0, deficit, seen.grid_import_kw, seen.grid_phase_kw, seen.plant_phase_kw
        )
        action, rate = verification.command(now, sample, plant.rating_kw)
        result = executor.step(plant.inputs(
            executor, now, action, rate, phase_probe_kw=verification.probe(), house_deficit_kw=deficit
        ))
        for op in result.ops:
            plant.apply(now, op)
        verification.after(now, result, sample, capability)
        assert max(plant.amps(now)) <= FUSE_A + 1e-6
        now += TICK
        if verification.done and executor.state.phase in ("idle", "blocked") and not result.ops:
            break
    return verification


def test_the_verification_passes_every_mode_on_a_plant_that_does_as_the_page_says() -> None:
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    found = dict(plant.states)
    verification = _verify(executor, plant, ("hold", "charge", "discharge_house", "per_phase"))
    assert verification.done
    outcomes = {mode: row["outcome"] for mode, row in verification.report()["modes"].items()}
    assert outcomes == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "per_phase": "obeyed", "normal": "restored"
    }
    # The probe's discharge on one phase is that phase's load less 1 kW, the others at their own load.
    probes = [(op.role, op.value) for _at, op in plant.writes if op.role in TARGETS]
    assert ("phase_l1_target", -700.0) in probes and ("phase_l3_target", 0.0) in probes
    # Everything SpotNav may restore is as found.
    plant.settle(T0)
    assert {name: plant.states[name] for name in ("charge_power", "discharge_power", "grid_setpoint", "ess_mode")} == {
        name: found[name] for name in ("charge_power", "discharge_power", "grid_setpoint", "ess_mode")
    }


def test_the_verification_fails_per_phase_on_a_plant_whose_multis_do_not_follow_their_setpoints() -> None:
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    plant.written_at = _Never()
    verification = _verify(executor, plant, ("per_phase",))
    assert gate.mode_verdict(verification.report()["modes"]["per_phase"]["outcome"]) == "fail"


class _Never(dict):  # type: ignore[type-arg]
    """A plant whose setpoints are never fresh: it stays in passthru whatever is written."""

    def get(self, _key: Any, _default: Any = None) -> None:  # type: ignore[override]
        return None

    def __setitem__(self, _key: Any, _value: Any) -> None:
        return None


# --- quirks seen in other installations ------------------------------------------------------------------------------


class GappyPlant(Plant):
    """A GX that has not republished some values for a while: victron-mqtt marks a metric not seen for
    `STALE_METRIC_TIMEOUT_SECONDS` (360 s) unavailable, and a value the GX publishes as null reads `unknown`."""

    def __init__(self, gaps: dict[str, str] | None = None) -> None:
        super().__init__()
        self.gaps: dict[str, str] = gaps or {}

    def reads(self, executor: Executor, now: datetime) -> dict[str, RoleRead]:
        reads = super().reads(executor, now)
        for name, state in self.gaps.items():
            if name in reads:
                reads[name] = RoleRead(
                    entity_id=reads[name].entity_id, state=state, found=True, enabled=True, step=reads[name].step,
                    device_id="GX",
                )
        return reads


def test_a_setting_spotnav_owns_that_goes_unknown_or_unavailable_is_neither_a_failure_nor_written_again() -> None:
    """Mode 2's settings are stored on the GX and do not revert (the module): a read that stops is the GX not
    republishing, no evidence of anything, and the battery's power keeps judging the effect."""
    plant, executor = GappyPlant(), Executor(VICTRON_CONTROL)
    now, results = _run(executor, plant, T0, 4, "hold")
    assert _written(results) == [("discharge_power", 0.0)]
    for gap in ("unavailable", "unknown"):
        plant.gaps = {"discharge_power": gap}
        now, results = _run(executor, plant, now, 60, "hold")  # 10 minutes
        assert _written(results) == [] and not _events(results, "hand_back") and executor.state.phase == "active"
    plant.gaps = {}
    now, results = _run(executor, plant, now, 4, "hold")
    assert _written(results) == [] and executor.state.phase == "active"
    _now, results = _run(executor, plant, now, 4, "self_consume")
    assert _written(results) == [("discharge_power", -1.0)]


def test_a_setpoint_that_reads_unknown_is_still_renewed_on_time() -> None:
    """The setpoints per phase are written again every `VICTRON_RENEW_S` whatever they read: an `unknown` before a
    renewal is no evidence and never holds a write back."""
    plant, executor = GappyPlant(), _phased()
    caps = _lifted()
    now, _ = _run(executor, plant, T0, 8, "grid_charge", 3.0, phase_caps_kw=caps)
    start = len(plant.writes)
    plant.gaps = {"phase_l1_target": "unknown"}
    now, results = _run(executor, plant, now, 2, "grid_charge", 3.0, phase_caps_kw=caps)
    plant.gaps = {}
    now, more = _run(executor, plant, now, 10, "grid_charge", 3.0, phase_caps_kw=caps)
    renewed = [at for at, op in plant.writes[start:] if op.role == "phase_l1_target"]
    assert renewed and (renewed[0] - (now - TICK * 12)).total_seconds() <= VICTRON_RENEW_S + 10
    assert not _events(results + more, "hand_back") and executor.state.phase == "active"


@pytest.mark.parametrize("found", ["0", "0.0"])
def test_a_max_inverter_power_of_0_keeps_every_discharge_from_starting_and_is_never_written(found: str) -> None:
    plant = Plant()
    plant.states["discharge_power"] = found
    for action, rate in (("discharge_house", 0.0), ("export_discharge", 3.0)):
        executor = Executor(VICTRON_CONTROL)
        _now, results = _run(executor, plant, T0, 3, action, rate)
        assert _written(results) == [] and executor.state.phase == "idle"
        assert _events(results, "not_started")[0]["reason"] == "inverter_power_zero"
        assert executor.told_problems(plant.reads(executor, T0)) == ("inverter_power_zero",)
    # A hold and a charge still run.
    executor = Executor(VICTRON_CONTROL)
    now, results = _run(executor, plant, T0, 4, "hold")
    assert executor.state.phase == "active"


def test_spotnav_s_own_hold_at_0_is_not_the_person_s_and_a_discharge_follows_it_without_a_hand_back() -> None:
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    now, results = _run(executor, plant, T0, 4, "hold")
    assert plant.states["discharge_power"] == "0"
    assert executor.told_problems(plant.reads(executor, now)) == ()
    now, results = _run(executor, plant, now, 6, "discharge_house")
    assert not _events(results, "hand_back") and executor.state.action == "discharge_house"
    assert ("charge_power", 0.0) in _written(results)
    assert Executor(VICTRON_CONTROL).told_problems(Plant().reads(executor, now)) == ()


def test_a_grid_setpoint_found_selling_is_handed_back_as_the_gx_default_never_as_found() -> None:
    """A negative grid setpoint at the snapshot is a sale running then (SpotNav's own left by a lost state, or
    another tool's): written back, the GX would sell the battery for good."""
    from custom_components.spotnav.battery.commands import VICTRON_GRID_DEFAULT_W

    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    plant.states["grid_setpoint"] = "-2000"
    now, results = _run(executor, plant, T0, 5, "grid_charge", 2.0)
    assert executor.state.restore_values["grid_setpoint"] == VICTRON_GRID_DEFAULT_W
    assert [role for role, _value in _written(results)] == ["charge_power", "grid_setpoint"]
    # The replacement is said in the change log.
    [replaced] = _events(results, "restore_replaced")
    assert replaced["reason"] == "grid_setpoint" and replaced["found"] == "-2000"
    assert replaced["restored_as"] == VICTRON_GRID_DEFAULT_W
    _now, results = _run(executor, plant, now, 5, "self_consume")
    assert _written(results) == [("grid_setpoint", VICTRON_GRID_DEFAULT_W), ("charge_power", -1.0)]
    # A hold does not touch the setpoint: it is left as found, not written to the default.
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    plant.states["grid_setpoint"] = "-100"
    now, results = _run(executor, plant, T0, 4, "hold")
    assert executor.state.phase == "active"
    _now, more = _run(executor, plant, now, 4, "self_consume")
    assert [role for role, _value in _written(results + more)] == ["discharge_power", "discharge_power"]
    assert plant.states["grid_setpoint"] == "-100"
    # A positive one, or a small negative bias against importing (down to `VICTRON_SALE_BELOW_W`), is the person's
    # and is written back as found, with nothing to log.
    for found in ("120", "-20", "-200"):
        plant, executor = Plant(), Executor(VICTRON_CONTROL)
        plant.states["grid_setpoint"] = found
        now, results = _run(executor, plant, T0, 5, "grid_charge", 2.0)
        assert not _events(results, "restore_replaced")
        _now, results = _run(executor, plant, now, 5, "self_consume")
        assert ("grid_setpoint", float(found)) in _written(results), found
    plant, executor = Plant(), Executor(VICTRON_CONTROL)
    plant.states["grid_setpoint"] = "-210"
    _run(executor, plant, T0, 5, "grid_charge", 2.0)
    assert executor.state.restore_values["grid_setpoint"] == VICTRON_GRID_DEFAULT_W


def test_the_ess_controls_are_found_on_another_device_of_the_battery_s_entry_but_never_guessed_between_two() -> None:
    """Victron puts the ESS settings on the GX's system device and the setpoints per phase on the VE.Bus device, not
    always the device of the battery's own entity (`facts.find_entity`): one in the entry is taken, two that cannot be
    told apart are none (control then says it is not found, never writes the wrong one)."""
    from custom_components.spotnav.battery.executor import role_reads
    from custom_components.spotnav.battery.facts import EntityFact

    battery = EntityFact("sensor.battery_power", "victron_gx", unique_id="abc_battery_512_battery_power",
                         device_id="battery", state="100")
    ess = EntityFact("number.ess_max_charge", "victron_gx", unique_id="abc_system_0_system_ess_max_charge_power",
                     device_id="system", state="-1")
    reads = role_reads(VICTRON_CONTROL, [battery, ess], battery)
    assert reads["charge_power"].entity_id == "number.ess_max_charge"
    other = replace(ess, entity_id="number.other_ess_max_charge", unique_id="xyz_system_0_system_ess_max_charge_power",
                    device_id="system2")
    assert "charge_power" not in role_reads(VICTRON_CONTROL, [battery, ess, other], battery)
    # One on the battery's own device wins over another elsewhere.
    own = replace(other, device_id="battery")
    assert role_reads(VICTRON_CONTROL, [battery, ess, own], battery)["charge_power"].entity_id == own.entity_id
