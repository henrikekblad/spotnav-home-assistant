"""The export limit of Victron, Sungrow, Solis, GoodWe and SolaX: each recipe's entities (by the unique ids its
integration gives them), orders and gate fields, and the executor on a fake plant of each (the switch that enables a
limit, a switch found off, Victron's -1, GoodWe without its switch, a change by someone else, the import that rises
under a limit on the inverter's output, a site without a signed grid reading)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from custom_components.spotnav.battery.commands import (
    ACTION_CURTAIL_EXPORT,
    EXPORT_CONTROLS,
    EXPORT_LIMIT_TABLE,
    GOODWE_CONTROL,
    GOODWE_EXPORT_CONTROL,
    SIGENERGY_EXPORT_CONTROL,
    SOLAX_EXPORT_CONTROL,
    SOLIS_EXPORT_CONTROL,
    SUNGROW_EXPORT_CONTROL,
    VICTRON_EXPORT_CONTROL,
    BrandControl,
    export_control_for,
    export_field_tested,
    export_table,
    field_tested,
    hardware_test_first,
)
from custom_components.spotnav.battery.executor import Executor, RoleRead, preview
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.monitor import (
    CURTAIL_GRACE_S,
    EXPORT_TOLERANCE_KW,
    IMPORT_ROSE_WINDOW_S,
    READING_GAP_S,
    Capability,
    Command,
    Monitor,
    Reading,
    judge,
)
from custom_components.spotnav.battery.shadow import MIN_DIVERGENCE_S
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, inputs, written

NEW = (VICTRON_EXPORT_CONTROL, SUNGROW_EXPORT_CONTROL, SOLIS_EXPORT_CONTROL, GOODWE_EXPORT_CONTROL, SOLAX_EXPORT_CONTROL)


# --- the recipes -----------------------------------------------------------------------------------------------------


def test_the_export_controls_are_sigenergy_and_the_five_beta_brands() -> None:
    assert [control.brand for control in EXPORT_CONTROLS] == [
        "sigenergy", "victron", "sungrow", "solis", "goodwe", "solax",
    ]
    for control in EXPORT_CONTROLS:
        assert export_control_for(control.brand) is control
        assert control.kind == "export" and control.limit_point == "grid"
        assert set(control.states) == {ACTION_CURTAIL_EXPORT}
        assert control.wanted(ACTION_CURTAIL_EXPORT, 0.0)["export_limit"] == 0.0
        # Untested: a beta tester's site only, and never behind the battery recipe's hardware test or code.
        assert control.field_tested is False and export_field_tested(control.brand) is False
        assert control.hardware_test_first is False
    for control in NEW:
        assert control.restore_own_only and control.count_restores
    mapped = {row["brand"] for row in EXPORT_LIMIT_TABLE if row["mapped"]}
    assert mapped == {control.brand for control in EXPORT_CONTROLS}
    assert all(row["verdict"] == "beta" for row in EXPORT_LIMIT_TABLE if row["mapped"])
    assert {row["brand"]: row["verdict"] for row in EXPORT_LIMIT_TABLE if row["brand"] in ("fronius", "kostal")} == {
        "fronius": "not_possible", "kostal": "not_possible",
    }


def test_the_battery_recipes_gate_is_never_the_export_controls() -> None:
    # GoodWe's battery recipe waits for a hardware test; its export limit does not inherit that.
    assert hardware_test_first("goodwe") is True and GOODWE_CONTROL.hardware_test_first
    assert GOODWE_EXPORT_CONTROL.hardware_test_first is False
    # `field_tested` answers for the battery recipe where the brand has one; the export control has its own.
    tested = replace(SOLAX_EXPORT_CONTROL, field_tested=True)
    assert tested.field_tested and field_tested("solax") is False
    assert export_field_tested("fronius") is False and export_field_tested(None) is False


def test_an_export_control_must_act_at_the_grid_point() -> None:
    with pytest.raises(ValueError, match="grid point"):
        replace(SOLAX_EXPORT_CONTROL, limit_point="inverter_ac")
    with pytest.raises(ValueError, match="grid point"):
        replace(SOLAX_EXPORT_CONTROL, limit_point=None)
    # A battery control states none.
    assert GOODWE_CONTROL.limit_point is None


def test_which_brands_have_a_source_that_states_zero_is_no_export() -> None:
    assert {control.brand: control.zero_stated for control in EXPORT_CONTROLS} == {
        "sigenergy": True, "victron": True, "sungrow": True, "solis": False, "goodwe": False, "solax": False,
    }


def test_victrons_feed_in_limit_reads_to_the_watt_and_names_no_limit() -> None:
    role = VICTRON_EXPORT_CONTROL.role("export_limit")
    assert role.match.matches("number", "c0619ab_system_0_system_ess_max_feed_in_power")
    assert not role.match.matches("number", "c0619ab_system_0_system_ess_max_charge_power")
    assert (role.scale, role.step, role.tolerance, role.no_limit) == (1000.0, 1.0, 1.0, -1.0)
    assert VICTRON_EXPORT_CONTROL.enter_order == VICTRON_EXPORT_CONTROL.restore_order == ("export_limit",)
    row = next(row for row in export_table() if row["brand"] == "victron" and row["action"] == ACTION_CURTAIL_EXPORT)
    assert row["no_limit"] == {"export_limit": -1.0} and row["zero_stated"] is True and row["limit_point"] == "grid"
    # The community Modbus integration is another brand and has no export control.
    assert export_control_for("victron_modbus") is None


def test_sungrows_limit_is_the_packages_number_with_its_switch_and_its_lower_bound() -> None:
    control = SUNGROW_EXPORT_CONTROL
    assert control.role("export_limit").match.matches("number", "uid_export_power_limit")
    assert control.role("export_limit").match.matches("number", "uid_export_power_limit_inv_2")
    assert not control.role("export_limit").match.matches("sensor", "sg_export_power_limit")
    assert control.role("export_enable").match.matches("switch", "sg_export_power_limit_switch")
    assert control.role("limit_min").match.matches("sensor", "sg_export_power_limit_min")
    assert control.enter_order == ("export_limit", "export_enable")
    assert control.restore_order == ("export_enable", "export_limit")
    assert control.wanted(ACTION_CURTAIL_EXPORT, 0.0) == {"export_limit": 0.0, "export_enable": True}


def test_solis_limit_is_the_backflow_power_read_by_its_register_with_the_feed_in_switch() -> None:
    control = SOLIS_EXPORT_CONTROL
    limit = control.role("export_limit")
    assert limit.match.matches("number", "solis_modbus_1234_solis_modbus_inverter_backflow_power")
    assert not limit.match.matches("number", "solis_modbus_1234_solis_modbus_inverter_flexible_export_backflow_power")
    assert limit.parts[0].matches("sensor", "solis_modbus_1234_solis_modbus_inverter_backflow_power")
    assert limit.compose is not None and limit.compose("0", ("1500",)) == "1500"
    assert control.role("export_enable").match.matches("switch", "solis_modbus_1234_43073_4")
    assert not control.role("export_enable").match.matches("switch", "solis_modbus_1234_43110_4")
    watched, value, why = control.watched[0]
    assert watched.match.matches("sensor", "solis_modbus_1234_solis_modbus_inverter_flexible_export_status")
    assert (value, why) == ("Off", "flexible_export_on")


def test_goodwes_limit_is_cores_number_and_the_switch_is_the_forks_and_optional() -> None:
    control = GOODWE_EXPORT_CONTROL
    assert control.role("export_limit").match.matches("number", "goodwe-grid_export_limit-9010KETU000W0000")
    # The fork's switches carry no `goodwe-` before the key.
    enable = control.role("export_enable")
    assert enable.match.matches("switch", "grid_export_limit_switch-9010KETU000W0000")
    assert not enable.match.matches("switch", "load_control-9010KETU000W0000")
    assert enable.optional and control.optional_names() == frozenset({"export_enable"})
    assert control.hacs_roles == ("export_enable",) and control.hacs_repo == "mletenay/home-assistant-goodwe-inverter"
    assert control.wanted(ACTION_CURTAIL_EXPORT, 0.0, absent=frozenset({"export_enable"})) == {"export_limit": 0.0}


def test_solax_limit_is_the_user_limit_and_never_the_remote_controls_target() -> None:
    role = SOLAX_EXPORT_CONTROL.role("export_limit")
    assert role.match.matches("number", "solax_export_control_user_limit")
    assert not role.match.matches("number", "solax_grid_export_limit")
    assert not role.match.matches("number", "solax_export_duration")
    assert SOLAX_EXPORT_CONTROL.wanted(ACTION_CURTAIL_EXPORT, 0.0) == {"export_limit": 0.0}


# --- the executor on a fake plant of each brand --------------------------------------------------------------------


@dataclass
class Plant(FakeBattery):
    """A plant free to run, whose every role is found unless `missing`; `watch` are the control's watched entities."""

    watch: dict[str, str] | None = None

    def management(self) -> Management:
        return Management("free", "self_consumption", {})

    def reads(self) -> dict[str, RoleRead]:
        out = super().reads()
        for role, _value, _why in self.control.watched:
            if self.watch is not None and role.name in self.watch:
                out[role.name] = RoleRead(f"sensor.{role.name}", self.watch[role.name], found=True, enabled=True)
        return out


def _plant(control: BrandControl, states: dict[str, str], **kwargs) -> tuple[Executor, Plant]:
    return Executor(control), Plant(control, dict(states), **kwargs)


def _run(executor: Executor, plant: Plant, start: datetime, looks: int, action: str | None, **overrides):
    now = start
    results = []
    failed: tuple[str, ...] = ()
    for _ in range(looks):
        result = executor.step(inputs(plant, now, action, failed=failed, **overrides))
        results.append(result)
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        now += TICK
    return now, results


def test_a_limit_with_a_switch_writes_the_value_first_and_the_switch_last_and_back_the_switch_first() -> None:
    executor, plant = _plant(SUNGROW_EXPORT_CONTROL, {"export_limit": "5000", "export_enable": "off", "limit_min": "0"})
    now, results = _run(executor, plant, T0, 6, ACTION_CURTAIL_EXPORT)
    assert written(results) == [("export_limit", 0.0), ("export_enable", True)]
    assert plant.states["export_limit"] == "0" and plant.states["export_enable"] == "on"
    assert executor.state.phase == "active"
    # Both roles are in the stored snapshot, the switch too.
    assert executor.state.snapshot == {"export_limit": "5000", "export_enable": "off"}
    _now, results = _run(executor, plant, now, 6, "self_consume")
    # A switch found off is switched off again, and the value is handed back as found.
    assert written(results) == [("export_enable", "off"), ("export_limit", 5000.0)]
    assert plant.states["export_limit"] == "5000" and plant.states["export_enable"] == "off"
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_a_switch_found_on_is_never_written() -> None:
    executor, plant = _plant(SOLIS_EXPORT_CONTROL, {"export_limit": "8000", "export_enable": "on"})
    now, results = _run(executor, plant, T0, 5, ACTION_CURTAIL_EXPORT)
    assert written(results) == [("export_limit", 0.0)]
    _now, results = _run(executor, plant, now, 5, "self_consume")
    assert written(results) == [("export_limit", 8000.0)]
    assert plant.states == {"export_limit": "8000", "export_enable": "on"}


def test_victrons_no_limit_is_handed_back_exactly_and_a_read_below_it_is_no_value() -> None:
    executor, plant = _plant(VICTRON_EXPORT_CONTROL, {"export_limit": "-1"})
    now, results = _run(executor, plant, T0, 4, ACTION_CURTAIL_EXPORT)
    assert written(results) == [("export_limit", 0.0)], "0 is one watt from -1: it is written"
    assert executor.state.restore_values == {"export_limit": -1.0}
    _now, results = _run(executor, plant, now, 4, "self_consume")
    assert written(results) == [("export_limit", -1.0)]
    assert plant.states["export_limit"] == "-1"
    executor, plant = _plant(VICTRON_EXPORT_CONTROL, {"export_limit": "-5"})
    _now, results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "no_restore_value:export_limit"


def test_goodwe_without_the_forks_switch_writes_the_limit_alone() -> None:
    executor, plant = _plant(GOODWE_EXPORT_CONTROL, {"export_limit": "10000"}, missing={"export_enable"})
    now, results = _run(executor, plant, T0, 5, ACTION_CURTAIL_EXPORT)
    assert written(results) == [("export_limit", 0.0)]
    assert executor.state.phase == "active" and executor.state.snapshot == {"export_limit": "10000"}
    _now, results = _run(executor, plant, now, 5, "self_consume")
    assert written(results) == [("export_limit", 10000.0)]
    # With the fork the switch is taken too.
    executor, plant = _plant(GOODWE_EXPORT_CONTROL, {"export_limit": "10000", "export_enable": "off"})
    _now, results = _run(executor, plant, T0, 6, ACTION_CURTAIL_EXPORT)
    assert written(results) == [("export_limit", 0.0), ("export_enable", True)]


def test_a_switch_changed_by_someone_else_is_given_up_and_left_alone() -> None:
    executor, plant = _plant(SUNGROW_EXPORT_CONTROL, {"export_limit": "5000", "export_enable": "off", "limit_min": "0"})
    now, _results = _run(executor, plant, T0, 6, ACTION_CURTAIL_EXPORT)
    plant.states["export_enable"] = "off"  # the installer's app switches the limitation off
    looks = int(MIN_DIVERGENCE_S / TICK.total_seconds()) + 8
    _now, results = _run(executor, plant, now, looks, ACTION_CURTAIL_EXPORT)
    assert events(results, "external")[0]["reason"] == "export_enable"
    assert ("export_enable", True) not in written(results)
    assert plant.states["export_enable"] == "off"
    assert executor.state.blocked is not None and executor.state.blocked.startswith("changed_outside_spotnav")


def test_sungrow_is_refused_where_the_inverters_least_limit_is_above_zero_or_unread() -> None:
    states = {"export_limit": "5000", "export_enable": "off"}
    _executor, plant = _plant(SUNGROW_EXPORT_CONTROL, states | {"limit_min": "300"})
    assert preview(SUNGROW_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT)).blocker == "limit_min_above_zero"
    _executor, plant = _plant(SUNGROW_EXPORT_CONTROL, states | {"limit_min": "unavailable"})
    assert preview(SUNGROW_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT)).blocker == "limit_min_unread"
    _executor, plant = _plant(SUNGROW_EXPORT_CONTROL, states | {"limit_min": "0.0"})
    assert preview(SUNGROW_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT)).blocker is None


def test_solis_is_not_started_while_flexible_export_is_on() -> None:
    states = {"export_limit": "8000", "export_enable": "on"}
    _executor, plant = _plant(SOLIS_EXPORT_CONTROL, states, watch={"flexible_export": "Active"})
    shown = preview(SOLIS_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT))
    assert shown.blocker == "flexible_export_on:flexible_export"
    for state in ("Off", "unavailable"):
        _executor, plant = _plant(SOLIS_EXPORT_CONTROL, states, watch={"flexible_export": state})
        assert preview(SOLIS_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT)).blocker is None
    # Where the inverter has no such sensor nothing is asked.
    _executor, plant = _plant(SOLIS_EXPORT_CONTROL, states)
    assert preview(SOLIS_EXPORT_CONTROL, inputs(plant, T0, ACTION_CURTAIL_EXPORT)).blocker is None


def test_a_site_without_a_signed_grid_reading_does_not_start_a_curtail() -> None:
    for control, states in (
        (SOLAX_EXPORT_CONTROL, {"export_limit": "6000"}),
        (SIGENERGY_EXPORT_CONTROL, {"export_limit": "11.5"}),
    ):
        executor, plant = _plant(control, states)
        if control is SIGENERGY_EXPORT_CONTROL:
            plant = FakeBattery(control, dict(states))
        _now, results = _run(executor, plant, T0, 3, ACTION_CURTAIL_EXPORT, grid_import_kw=None)
        assert written(results) == []
        assert events(results, "not_started")[0]["reason"] == "no_grid_reading"


# --- a limit that caps the inverter's output -----------------------------------------------------------------------


def _curtail(before_kw: float) -> Command:
    return Command("curtail", T0, 0.0, before_kw=before_kw, written=True)


def test_an_import_after_an_export_is_import_rose_unless_the_battery_and_the_chargers_take_it() -> None:
    command = _curtail(-4.0)
    assert judge(command, 0.0, 0.3) == "ok"
    assert judge(command, 0.0, EXPORT_TOLERANCE_KW + 0.1) == "import_rose"
    assert judge(command, 0.0, 3.0, excused_kw=2.6) == "ok"
    assert judge(command, 0.0, 3.0, excused_kw=2.0) == "import_rose"
    # A site that did not export before says nothing: its import is the house's.
    assert judge(_curtail(1.0), 0.0, 3.0) == "ok"
    assert judge(Command("curtail", T0, 0.0, before_kw=None, written=True), 0.0, 3.0) == "ok"
    # Still exporting is judged as before.
    assert judge(command, 0.0, -4.0) == "no_effect" and judge(command, 0.0, -1.5) == "partial"


def test_three_readings_of_a_risen_import_are_one_failure_and_only_soon_after_the_command() -> None:
    monitor = Monitor()
    monitor.start(_curtail(-4.0))
    judged = None
    step = READING_GAP_S
    at = T0 + timedelta(seconds=CURTAIL_GRACE_S)
    for _ in range(3):
        judged = monitor.observe(Reading(2.5, 0.0), Capability(), at)
        at += timedelta(seconds=step)
    assert judged is not None and judged.kind == "failure" and judged.outcome == "import_rose"
    # A load that starts long after the limit was set is the house's own.
    monitor = Monitor()
    monitor.start(_curtail(-4.0))
    at = T0 + timedelta(seconds=IMPORT_ROSE_WINDOW_S + 1)
    for _ in range(4):
        assert monitor.observe(Reading(2.5, 0.0), Capability(), at) is None
        at += timedelta(seconds=step)


def test_the_executor_hands_back_and_blocks_a_limit_that_makes_the_site_import() -> None:
    executor, plant = _plant(SOLAX_EXPORT_CONTROL, {"export_limit": "6000"})
    now = T0
    results = []
    failed: tuple[str, ...] = ()
    for _ in range(int((CURTAIL_GRACE_S + 5 * READING_GAP_S) / TICK.total_seconds())):
        # The "limit" stops the inverter: the house's 2.5 kW comes from the grid.
        grid_kw = 2.5 if plant.states["export_limit"] == "0" else -4.0
        result = executor.step(inputs(plant, now, ACTION_CURTAIL_EXPORT, failed=failed, grid_import_kw=grid_kw))
        results.append(result)
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        now += TICK
    assert events(results, "monitor")[0]["reason"] == "import_rose"
    assert events(results, "hand_back")[0]["reason"] == "not_obeyed:curtail_export:import_rose"
    assert plant.states["export_limit"] == "6000"
    assert executor.state.phase == "blocked" and executor.state.blocked.startswith("not_obeyed")


def test_an_import_the_battery_and_the_car_take_is_not_the_limits_doing() -> None:
    executor, plant = _plant(SOLAX_EXPORT_CONTROL, {"export_limit": "6000"})
    now = T0
    results = []
    failed: tuple[str, ...] = ()
    for _ in range(int((CURTAIL_GRACE_S + 5 * READING_GAP_S) / TICK.total_seconds())):
        grid_kw = 2.5 if plant.states["export_limit"] == "0" else -4.0
        result = executor.step(inputs(
            plant, now, ACTION_CURTAIL_EXPORT, failed=failed, grid_import_kw=grid_kw, excused_import_kw=7.0,
        ))
        results.append(result)
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        now += TICK
    assert events(results, "monitor") == [] and executor.state.phase == "active"
