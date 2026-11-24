"""FoxESS's control recipe (`commands.FOXESS_CONTROL`, `foxess_modbus`) through the executor against a fake inverter
that does what the integration's source says: limits only (the battery's max discharge current at 0 to hold, its max
charge current at 0 to cover the house without storing), each written to the inverter and read back as polled, the
hand-back to the values found, no grid charge and no sale, a plant in any work mode but Self Use left alone, the
verification and the hardware test without a charge step, and the shadow's "would" lines. Nothing here has run on a
device."""

from __future__ import annotations

from dataclasses import dataclass

from custom_components.spotnav.battery import optin
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    FOXESS_CONTROL,
    NO_EXPIRY,
    control_for,
    hardware_test_first,
    table,
)
from custom_components.spotnav.battery.executor import Executor, preview
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.gate import modes_to_run, needs_run, merge_report
from custom_components.spotnav.battery.monitor import Capability
from custom_components.spotnav.battery.plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
)
from custom_components.spotnav.battery.verify import Sample, Timing, Verification, supported_modes
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, inputs, run, written

#: The owner's limits as found (A).
STATES = {"discharge_current": "25", "charge_current": "25"}


@dataclass
class Inverter(FakeBattery):
    """A FoxESS hybrid as `foxess_modbus` shows it: the work mode select and the two current limits."""

    work_mode: str = "Self Use"

    def management(self) -> Management:
        free = self.work_mode == "Self Use"
        return Management(
            "free" if free else "self_managed", "self_consumption" if free else f"work mode:{self.work_mode}",
            {"work mode": self.work_mode},
        )


def _inverter(**kwargs: object) -> Inverter:
    return Inverter(FOXESS_CONTROL, dict(STATES), **kwargs)  # type: ignore[arg-type]


def test_the_recipe_is_limits_only_and_waits_for_a_hardware_test() -> None:
    assert control_for("foxess") is FOXESS_CONTROL and FOXESS_CONTROL in CONTROLS
    assert set(FOXESS_CONTROL.states) == {ACTION_HOLD, ACTION_DISCHARGE_HOUSE}
    assert not FOXESS_CONTROL.supports(ACTION_GRID_CHARGE) and not FOXESS_CONTROL.supports(ACTION_EXPORT_DISCHARGE)
    # A lower limit never adds import: nothing to expire, no fuse cap needed, every role handed back as found.
    assert FOXESS_CONTROL.expiry is NO_EXPIRY and FOXESS_CONTROL.import_limit is None
    assert all(role.restored and role.write_class == "setting" for role in FOXESS_CONTROL.roles)
    assert hardware_test_first("foxess") and not FOXESS_CONTROL.field_tested
    assert not hardware_test_first("sigenergy")
    rows = {row["action"]: row["writes"] for row in table() if row["brand"] == "foxess"}
    assert rows[ACTION_HOLD] == {"discharge_current": 0.0}
    assert rows[ACTION_DISCHARGE_HOUSE] == {"charge_current": 0.0}
    assert ACTION_GRID_CHARGE not in rows
    # The opt-in enables exactly the roles' numbers.
    assert optin.ENABLE_KEYS["foxess"] == (("number", "max_discharge_current"), ("number", "max_charge_current"))


def test_a_hold_writes_the_discharge_current_to_0_and_the_hand_back_writes_it_back() -> None:
    battery = _inverter()
    executor = Executor(FOXESS_CONTROL)
    now, results = run(executor, battery, T0, 4, ACTION_HOLD)
    assert written(results) == [("discharge_current", 0.0)]
    assert battery.states == {"discharge_current": "0", "charge_current": "25"}
    assert executor.state.phase == "active"
    # A discharge for the house: the discharge current back, the charge current to 0.
    now, results = run(executor, battery, now, 6, ACTION_DISCHARGE_HOUSE)
    assert sorted(written(results)) == [("charge_current", 0.0), ("discharge_current", 25.0)]
    # Normal: both as found.
    _now, results = run(executor, battery, now, 8, None)
    assert battery.states == STATES
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_a_grid_charge_is_never_written() -> None:
    battery = _inverter()
    executor = Executor(FOXESS_CONTROL)
    _now, results = run(executor, battery, T0, 4, ACTION_GRID_CHARGE, 2.0)
    assert written(results) == [] and battery.states == STATES
    assert any(event.get("reason") == f"action_not_supported:{ACTION_GRID_CHARGE}" for event in events(results, "not_started"))


def test_a_plant_in_another_work_mode_is_left_alone() -> None:
    for mode in ("Feed-in First", "Back-up", "Peak Shaving", "Force Charge", "Force Discharge"):
        battery = _inverter(work_mode=mode)
        _now, results = run(Executor(FOXESS_CONTROL), battery, T0, 4, ACTION_HOLD)
        assert written(results) == [], mode


def test_the_shadow_shows_what_a_hold_would_write_and_writes_nothing() -> None:
    battery = _inverter()
    seen = preview(FOXESS_CONTROL, inputs(battery, T0, ACTION_HOLD))
    assert seen.blocker is None
    assert [(op.role, op.value) for op in seen.writes] == [("discharge_current", 0.0)]
    assert battery.writes == []


def test_the_hardware_test_holds_covers_the_house_skips_the_charge_and_hands_back() -> None:
    battery = _inverter()
    executor = Executor(FOXESS_CONTROL)
    verification = Verification(executor, T0, firmware="V1.40", timing=Timing.quick(), stop_on_fail=True)
    now = T0
    failed: tuple[str, ...] = ()
    capability = Capability(rated_charge_kw=5.0, rated_discharge_kw=5.0)
    for _ in range(400):
        power = battery.power_kw(battery.action, battery.rate_kw)
        sample = Sample(power, 5.0, battery.soc_pct, 2.0)
        action, rate = verification.command(now, sample, 5.0)
        result = executor.step(inputs(battery, now, action, rate, failed=failed, house_deficit_kw=2.0))
        failed = tuple(op.role for op in result.ops if not battery.apply(op))
        battery.action = executor.state.action if executor.state.phase == "active" else None
        battery.rate_kw = executor.state.rate_kw
        verification.after(now, result, sample, capability)
        now += TICK
        if verification.done and executor.state.phase in ("idle", "blocked") and not result.ops:
            break
    report = verification.report()
    assert {mode: row["outcome"] for mode, row in report["modes"].items()} == {
        "hold": "obeyed", "charge": "skipped", "discharge_house": "obeyed", "normal": "restored",
    }
    assert battery.states == STATES
    # The routine never waits for a charge step it cannot take.
    merged = merge_report(None, report, key="V1.40 | foxess_modbus 1", attempts=1, now=now)
    assert modes_to_run(merged, supported_modes(FOXESS_CONTROL)) == ()
    assert not needs_run(merged, supported_modes(FOXESS_CONTROL))
