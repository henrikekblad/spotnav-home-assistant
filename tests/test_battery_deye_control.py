"""Deye / Sunsynk's control recipe (`commands.DEYE_CONTROL`, `solarman` or kellerza's MQTT add-on) through the executor
against a fake hybrid that does what the integrations' source says: limits only (the battery's max discharge current
at 0 A to hold, its max charge current at 0 A to cover the house without storing), each written on a change only and
read back, the hand-back to the values found, nothing written over a limit someone else changed, no grid charge and no
sale, a plant whose Time of Use is on left alone, a day's write cap below a setting's, the hardware test without a
charge step, and the shadow's "would" lines. Nothing here has run on a device."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import pytest

from custom_components.spotnav.battery import optin
from custom_components.spotnav.battery.adapters import DEYE
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    DEYE_CONTROL,
    DEYE_SETTING_WRITES,
    NO_EXPIRY,
    SETTING_WRITES,
    control_for,
    hardware_test_first,
    table,
)
from custom_components.spotnav.battery.executor import RESTORE_READ_WAIT_S, Executor, ExecState, preview
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.ledger import Ledger
from custom_components.spotnav.battery.gate import merge_report, modes_to_run, needs_run
from custom_components.spotnav.battery.monitor import Capability, Monitor
from custom_components.spotnav.battery.plan_model import (
    ACTION_DISCHARGE_HOUSE,
    ACTION_EXPORT_DISCHARGE,
    ACTION_GRID_CHARGE,
    ACTION_HOLD,
)
from custom_components.spotnav.battery.verify import Sample, Timing, Verification, supported_modes
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, inputs, run, written

#: The owner's limits as found (A).
STATES = {"discharge_current": "120", "charge_current": "100"}
#: The unique ids each route builds: `solarman`'s `<entry id>_<slug of the item's name>_<platform>`, kellerza's
#: `<inverter serial>_<slug of the sensor's name>`.
SOLARMAN = {
    "discharge_current": "01jsolarman000000000000000_battery_max_discharging_current_number",
    "charge_current": "01jsolarman000000000000000_battery_max_charging_current_number",
}
KELLERZA = {
    "discharge_current": "2201010101_battery_max_discharge_current",
    "charge_current": "2201010101_battery_max_charge_current",
}


@dataclass
class Hybrid(FakeBattery):
    """A Deye hybrid as `solarman` shows it: the Time of Use select and the two current limits (whole amps). It covers
    the house up to its discharge limit and stores nothing at a charge limit of 0."""

    time_of_use: str = "Disabled"
    house_kw: float = 2.0
    volts: float = 51.2

    def management(self) -> Management:
        free = self.time_of_use in ("Disabled", "off")
        return Management(
            "free" if free else "self_managed", "self_consumption" if free else f"time of use:{self.time_of_use}",
            {"time of use": self.time_of_use},
        )

    def apply(self, op: Any) -> bool:
        """Home Assistant skips an entity service call to an unavailable entity: nothing lands, no error."""
        if self.states.get(op.role) == "unavailable":
            self.writes.append(op)
            return True
        return super().apply(op)

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        """The battery's power (kW, + charging) from the limits it holds, whatever the executor thinks."""
        if not self.obeys:
            return self.idle_kw
        try:
            limit_a = float(self.states["discharge_current"])
        except ValueError:
            return self.idle_kw
        return -min(self.house_kw, limit_a * self.volts / 1000)


def _plant(**kwargs: Any) -> tuple[Executor, Hybrid]:
    return Executor(DEYE_CONTROL), Hybrid(DEYE_CONTROL, dict(STATES), idle_kw=-2.0, **kwargs)


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_limits_only_waits_for_a_hardware_test_and_writes_less_than_a_setting() -> None:
    assert control_for("deye") is DEYE_CONTROL and DEYE_CONTROL in CONTROLS and DEYE_CONTROL.adapter is DEYE
    assert set(DEYE_CONTROL.states) == {ACTION_HOLD, ACTION_DISCHARGE_HOUSE}
    assert not DEYE_CONTROL.supports(ACTION_GRID_CHARGE) and not DEYE_CONTROL.supports(ACTION_EXPORT_DISCHARGE)
    # A lower limit never adds import: nothing to expire, no import limit counted on, every role handed back as found.
    assert DEYE_CONTROL.expiry is NO_EXPIRY and DEYE_CONTROL.import_limit is None
    assert DEYE_CONTROL.autonomous_import is None and DEYE_CONTROL.command_role is None
    assert all(role.restored and role.write_class == "setting" for role in DEYE_CONTROL.roles)
    # Flash: fewer counted writes a day than a plain setting.
    assert DEYE_CONTROL.write_cap("setting") == DEYE_SETTING_WRITES < SETTING_WRITES
    assert hardware_test_first("deye") and not DEYE_CONTROL.field_tested
    rows = {row["action"]: row["writes"] for row in table() if row["brand"] == "deye"}
    assert rows[ACTION_HOLD] == {"discharge_current": 0.0}
    assert rows[ACTION_DISCHARGE_HOUSE] == {"charge_current": 0.0}
    assert ACTION_GRID_CHARGE not in rows and ACTION_EXPORT_DISCHARGE not in rows


@pytest.mark.parametrize("ids", [SOLARMAN, KELLERZA], ids=["solarman", "kellerza"])
def test_each_role_is_the_integrations_number_and_the_card_offers_to_enable_it(ids: dict[str, str]) -> None:
    for role in DEYE_CONTROL.roles:
        assert role.match.matches("number", ids[role.name]), role.name
        assert not role.match.matches("sensor", ids[role.name])
        assert any(role.match.matches(domain, f"PLANT_{key}") for domain, key in optin.ENABLE_KEYS["deye"])
    # The BMS's own limits (kellerza's sensors) and the grid charge current are not the roles.
    for other in ("2201010101_battery_charge_limit_current", "2201010101_grid_charge_battery_current",
                  "01jsolarman000000000000000_battery_grid_charging_current_number"):
        assert not any(role.match.matches("number", other) or role.match.matches("sensor", other)
                       for role in DEYE_CONTROL.roles), other


def test_over_mqtt_a_limit_is_only_the_one_on_the_batterys_own_device() -> None:
    """The MQTT broker's entry holds every MQTT device: a number of the same name on another device (a BMS, a second
    system) is never taken for a limit SpotNav writes, even as the only one; on the battery's device it is."""
    from custom_components.spotnav.battery.executor import role_reads  # noqa: PLC0415
    from custom_components.spotnav.battery.facts import EntityFact  # noqa: PLC0415

    def fact(key: str, device: str, domain: str = "number", serial: str = "2201010101") -> EntityFact:
        return EntityFact(
            entity_id=f"{domain}.{device}_{key}", platform="mqtt", unique_id=f"{serial}_{key}", device_id=device,
            config_entry_id="broker", state="100", unit="A",
        )

    battery = fact("battery_power", "inverter", domain="sensor")
    others = [fact("battery_max_discharge_current", "bms", serial="9999"),
              fact("battery_max_charge_current", "bms", serial="9999")]
    assert role_reads(DEYE_CONTROL, [battery, *others], battery) == {}
    own = [fact("battery_max_discharge_current", "inverter"), fact("battery_max_charge_current", "inverter")]
    reads = role_reads(DEYE_CONTROL, [battery, *others, *own], battery)
    assert {name: read.entity_id for name, read in reads.items()} == {
        "discharge_current": "number.inverter_battery_max_discharge_current",
        "charge_current": "number.inverter_battery_max_charge_current",
    }


# --- the actions -----------------------------------------------------------------------------------------------------


def test_a_hold_writes_the_discharge_current_to_0_and_the_hand_back_writes_it_back() -> None:
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 4, ACTION_HOLD)
    assert written(results) == [("discharge_current", 0.0)]
    assert plant.states == {"discharge_current": "0", "charge_current": "100"}
    assert executor.state.phase == "active" and plant.power_kw() == 0.0
    # The same action again writes nothing (a write only on a change).
    now, results = run(executor, plant, now, 30, ACTION_HOLD)
    assert written(results) == []
    # A discharge for the house: the discharge current back, the charge current to 0.
    now, results = run(executor, plant, now, 6, ACTION_DISCHARGE_HOUSE)
    assert sorted(written(results)) == [("charge_current", 0.0), ("discharge_current", 120.0)]
    # Normal: both as found.
    _now, results = run(executor, plant, now, 8, None)
    assert plant.states == STATES
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot


def test_a_grid_charge_and_a_sale_are_never_written() -> None:
    for action in (ACTION_GRID_CHARGE, ACTION_EXPORT_DISCHARGE):
        executor, plant = _plant()
        _now, results = run(executor, plant, T0, 4, action, 2.0)
        assert written(results) == [] and plant.states == STATES
        assert any(event.get("reason") == f"action_not_supported:{action}" for event in events(results, "not_started"))


@pytest.mark.parametrize("time_of_use", ["Week", "Weekdays", "Monday", "on"])
def test_a_plant_whose_time_of_use_is_on_is_left_alone(time_of_use: str) -> None:
    executor, plant = _plant(time_of_use=time_of_use)
    _now, results = run(executor, plant, T0, 4, ACTION_HOLD)
    assert written(results) == [] and plant.states == STATES


def test_time_of_use_turned_on_under_a_hold_hands_back() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 4, ACTION_HOLD)
    plant.time_of_use = "Week"
    _now, results = run(executor, plant, now, 8, ACTION_HOLD)
    assert events(results, "hand_back") and plant.states == STATES
    assert executor.state.phase != "active"


def test_a_limit_found_unknown_is_never_started_on() -> None:
    """`solarman` shows a value outside its profile's range as unknown (ha-solarman #1233): no snapshot to hand back."""
    executor, plant = _plant()
    plant.states["discharge_current"] = "unknown"
    _now, results = run(executor, plant, T0, 6, ACTION_HOLD)
    assert ("discharge_current", 0.0) not in written(results)
    assert plant.states["discharge_current"] == "unknown"


# --- someone else's writes -------------------------------------------------------------------------------------------


def test_a_limit_the_app_changed_under_a_hold_is_given_up_and_not_written_back_over() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 4, ACTION_HOLD)
    # The vendor app (or Predbat, evcc) sets its own discharge current while SpotNav holds.
    plant.states["discharge_current"] = "50"
    now, results = run(executor, plant, now, 40, ACTION_HOLD)
    assert ("discharge_current", 0.0) not in written(results), "never fought"
    now, results = run(executor, plant, now, 20, None)
    assert all(role != "discharge_current" for role, _value in written(results)), "the hand-back leaves it alone"
    assert plant.states["discharge_current"] == "50"


def test_a_write_the_inverter_does_not_keep_hands_back_and_blocks() -> None:
    """A write `solarman` acknowledges that the next poll shows as before (ha-solarman #1222: a setting the inverter
    takes only in its app's group write) is no write taken."""
    executor, plant = _plant()
    plant.drops.add("discharge_current")
    _now, results = run(executor, plant, T0, 30, ACTION_HOLD)
    assert written(results) == [("discharge_current", 0.0)], "never repeated"
    assert executor.state.phase == "blocked" and plant.states == STATES


def test_a_hold_that_does_not_hold_is_handed_back() -> None:
    executor, plant = _plant(obeys=False)
    _now, results = run(executor, plant, T0, 80, ACTION_HOLD)
    assert events(results, "hand_back") and executor.state.phase == "blocked"
    assert plant.states == STATES


# --- a restart mid-hold -------------------------------------------------------------------------------------------


def _restarted(control: Any = DEYE_CONTROL, found_after: str | None = None) -> tuple[Executor, Hybrid, Any]:
    """A hold, then Home Assistant restarts (the executor, its ledger and monitor from what was stored); meanwhile
    the discharge current reads `found_after`."""
    executor, plant = Executor(control), Hybrid(control, dict(STATES), idle_kw=-2.0)
    now, _results = run(executor, plant, T0, 4, ACTION_HOLD)
    stored = executor.as_dict()
    again = Executor(
        control, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        state=ExecState.from_dict(stored["state"]),
    )
    again.restart(now)
    if found_after is not None:
        plant.states["discharge_current"] = found_after
    return again, plant, now


def test_a_restart_mid_hold_hands_the_limit_back_as_found() -> None:
    again, plant, now = _restarted()
    _now, results = run(again, plant, now, 6, None, stopping="restart")
    assert written(results) == [("discharge_current", 120.0)]
    assert plant.states == STATES and again.state.phase == "idle"


def test_a_limit_set_by_someone_else_while_home_assistant_was_down_is_left_alone() -> None:
    again, plant, now = _restarted(found_after="50")
    _now, results = run(again, plant, now, 6, None, stopping="restart")
    assert written(results) == [] and plant.states["discharge_current"] == "50"
    assert events(results, "external")


def test_after_a_restart_the_hand_back_waits_for_a_first_reading_and_never_writes_blind() -> None:
    again, plant, now = _restarted(found_after="unavailable")
    now, results = run(again, plant, now, 6, None, stopping="restart")
    assert written(results) == [] and again.state.phase == "restoring"
    # The integration reads the inverter: someone else's value is left alone.
    plant.states["discharge_current"] = "50"
    _now, results = run(again, plant, now, 6, None)
    assert written(results) == [] and plant.states["discharge_current"] == "50"
    # SpotNav's own value is handed back.
    again, plant, now = _restarted(found_after="unavailable")
    now, _results = run(again, plant, now, 6, None, stopping="restart")
    plant.states["discharge_current"] = "0"
    _now, results = run(again, plant, now, 6, None)
    assert written(results) == [("discharge_current", 120.0)] and plant.states == STATES


def test_a_reading_that_never_comes_is_written_after_the_wait_as_before() -> None:
    again, plant, now = _restarted(found_after="unavailable")
    looks = int(RESTORE_READ_WAIT_S // TICK.total_seconds())
    now, results = run(again, plant, now, looks - 1, None, stopping="restart")
    assert written(results) == []
    _now, results = run(again, plant, now, 3, None, stopping="restart")
    assert ("discharge_current", 120.0) in written(results)


def test_without_the_flag_a_restart_writes_back_at_once_as_every_other_recipe() -> None:
    plain = replace(DEYE_CONTROL, restore_own_only=False)
    again, plant, now = _restarted(plain, found_after="unavailable")
    _now, results = run(again, plant, now, 2, None, stopping="restart")
    assert written(results) == [("discharge_current", 120.0)]


# --- the fuse and the day's writes -----------------------------------------------------------------------------------


def test_a_holds_peak_is_covered_by_the_discharge_limit_and_a_near_fuse_hands_back() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, ACTION_HOLD)
    _now, results = run(executor, plant, now, 2, ACTION_HOLD, headroom_a=1.2)
    assert written(results) == [("discharge_current", 120.0)] and events(results, "peak_cover")
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, ACTION_HOLD)
    _now, results = run(executor, plant, now, 1, ACTION_HOLD, headroom_a=0.5)
    assert written(results) == [("discharge_current", 120.0)]
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"


def test_the_days_write_cap_stops_the_changes_and_the_hand_back_still_runs() -> None:
    executor, plant = _plant()
    now = T0
    counted = 0
    for _ in range(DEYE_SETTING_WRITES + 10):
        now, results = run(executor, plant, now, 6, ACTION_HOLD)
        counted += len(written(results))
        now, results = run(executor, plant, now, 6, ACTION_DISCHARGE_HOUSE)
        counted += len(written(results))
        if executor.state.phase != "active":
            break
    # A change between the two actions writes both limits: the cap ends them after 48 counted writes, half a
    # setting's, and blocks control for the rest of the day.
    assert executor.state.phase == "blocked" and executor.state.blocked == "daily_write_cap"
    assert DEYE_SETTING_WRITES <= counted <= DEYE_SETTING_WRITES + 3
    _now, results = run(executor, plant, now + timedelta(minutes=5), 20, None)
    assert plant.states == STATES


def test_every_register_write_counts_the_hand_backs_too_and_a_spent_cap_refuses_the_next_start() -> None:
    executor, plant = _plant()
    now = T0
    for _ in range(DEYE_SETTING_WRITES):
        now, _results = run(executor, plant, now, 6, ACTION_HOLD)
        now, _results = run(executor, plant, now, 6, None)
        assert plant.states == STATES, "each hand-back runs"
    # A hold and its hand-back are two register writes, both counted: the 48 are spent after 24 holds, and no
    # hold starts again today.
    assert len(plant.writes) <= DEYE_SETTING_WRITES + 1
    assert executor.state.writes_today >= DEYE_SETTING_WRITES
    assert DEYE_CONTROL.count_restores
    _now, results = run(executor, plant, now, 6, ACTION_HOLD)
    assert written(results) == [] and plant.states == STATES


# --- shadow and the hardware test ------------------------------------------------------------------------------------


def test_the_shadow_shows_what_a_hold_would_write_and_writes_nothing() -> None:
    _executor, plant = _plant()
    seen = preview(DEYE_CONTROL, inputs(plant, T0, ACTION_HOLD))
    assert seen.blocker is None
    assert [(op.role, op.value) for op in seen.writes] == [("discharge_current", 0.0)]
    assert plant.writes == []


def test_the_hardware_test_holds_covers_the_house_skips_the_charge_and_hands_back() -> None:
    executor, plant = _plant()
    verification = Verification(executor, T0, firmware="2107-1175-1809", timing=Timing.quick(), stop_on_fail=True)
    now = T0
    failed: tuple[str, ...] = ()
    capability = Capability(rated_charge_kw=5.0, rated_discharge_kw=5.0)
    for _ in range(400):
        power = plant.power_kw()
        sample = Sample(power, 5.0, plant.soc_pct, 2.0)
        action, rate = verification.command(now, sample, 5.0)
        result = executor.step(inputs(plant, now, action, rate, failed=failed, house_deficit_kw=2.0))
        failed = tuple(op.role for op in result.ops if not plant.apply(op))
        plant.action = executor.state.action if executor.state.phase == "active" else None
        verification.after(now, result, sample, capability)
        now += TICK
        if verification.done and executor.state.phase in ("idle", "blocked") and not result.ops:
            break
    report = verification.report()
    assert {mode: row["outcome"] for mode, row in report["modes"].items()} == {
        "hold": "obeyed", "charge": "skipped", "discharge_house": "obeyed", "normal": "restored",
    }
    assert plant.states == STATES
    # The routine never waits for a charge step it cannot take.
    merged = merge_report(None, report, key="2107-1175-1809 | solarman 25.08.16", attempts=1, now=now)
    assert modes_to_run(merged, supported_modes(DEYE_CONTROL)) == ()
    assert not needs_run(merged, supported_modes(DEYE_CONTROL))
