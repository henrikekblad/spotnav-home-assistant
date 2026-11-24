"""Marstek's control recipe (`commands.MARSTEK_CONTROL`, `marstek_modbus`) through the executor against a fake Venus E
that does what the integration's register files and evcc's templates say: the recipe as data, a hold that is only a
limit, a force charge whose 42xxx registers act only with RS485 control on, the hand-back's order, a charge that keeps
the fuse when Home Assistant stops (no dead-man), the check before control in full and in the hardware test's quick
timing, and the integrations it is not for. Nothing here has run on a device."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

from custom_components.spotnav.battery import optin
from custom_components.spotnav.battery.adapters import MARSTEK, OMNIBATTERY, adapter_for
from custom_components.spotnav.battery.coexist import outside_integrations
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    MARSTEK_CONTROL,
    NO_EXPIRY,
    control_for,
    field_tested,
    table,
)
from custom_components.spotnav.battery.executor import HEADROOM_MARGIN_A, REARM_S, Executor, preview
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.gate import grid_charge_cap_kw, shadow_default
from tests.battery_control_fakes import T0, FakeBattery, events, inputs, run, written

VOLTS = 400.0 / 3 ** 0.5
#: The integration's unique ids, `<config entry id>_<key>`, per role.
ENTRY = "01jmarstekmodbus0000000000"
KEYS = {
    "rs485_control": ("switch", f"{ENTRY}_rs485_control_mode"),
    "force_mode": ("select", f"{ENTRY}_force_mode"),
    "charge_power": ("number", f"{ENTRY}_set_charge_power"),
    "max_discharge_power": ("number", f"{ENTRY}_max_discharge_power"),
}
STATES = {"max_discharge_power": "2500", "rs485_control": "off", "charge_power": "0", "force_mode": "standby"}
#: The registers that act only with RS485 control on (the integration's README).
RS485_ONLY = frozenset({"force_mode", "charge_power"})


@dataclass
class Venus(FakeBattery):
    """A Venus E as `marstek_modbus` shows it. In its own self-consumption (`anti_feed`) it covers the house up to its
    maximum discharge power; with RS485 control on it does what the force mode says (standby: idle, charge: the charge
    power). A write to a 42xxx register with RS485 control off is not taken. It keeps whatever it was last told."""

    work_mode: str = "anti_feed"
    house_kw: float = 2.0
    #: Writes to a 42xxx register made while RS485 control was off.
    ignored: list[str] = field(default_factory=list)

    def apply(self, op: Any) -> bool:
        if op.role in RS485_ONLY and self.states["rs485_control"] != "on":
            self.writes.append(op)
            self.ignored.append(op.role)
            return True
        taken = super().apply(op)
        if op.method == "number" and op.role in self.states and taken and op.role not in self.drops:
            self.states[op.role] = str(int(float(op.value)))
        return taken

    def management(self) -> Management:
        settings = {"operating mode": self.work_mode, "RS485 control": self.states["rs485_control"]}
        if self.work_mode == "anti_feed" and self.states["rs485_control"] == "off":
            return Management("free", "self_consumption", settings)
        reason = "operating mode:" + self.work_mode if self.work_mode != "anti_feed" else "RS485 control:on"
        return Management("self_managed", reason, settings)

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        """The battery's power (kW, + charging) from what the battery holds, whatever the executor thinks."""
        if not self.obeys:
            return self.idle_kw
        if self.states["rs485_control"] == "on":
            mode = self.states["force_mode"]
            return float(self.states["charge_power"]) / 1000 if mode == "charge" else 0.0
        return -min(self.house_kw, float(self.states["max_discharge_power"]) / 1000)


def _as_found(plant: Venus) -> bool:
    """Every role as found but the charge power, which only a force charge uses and which is never written back."""
    return {**plant.states, "charge_power": "0"} == STATES


def _plant(**kwargs: Any) -> tuple[Executor, Venus]:
    return Executor(MARSTEK_CONTROL), Venus(MARSTEK_CONTROL, dict(STATES), idle_kw=-2.0, **kwargs)


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_a_discharge_limit_for_a_hold_and_a_force_charge_under_rs485_control() -> None:
    control = MARSTEK_CONTROL
    assert control_for("marstek") is control and control in CONTROLS and control.adapter is MARSTEK
    assert adapter_for("marstek_modbus", None) is MARSTEK
    assert control.wanted("hold", 0.0) == {
        "max_discharge_power": 0.0, "rs485_control": "<snapshot>", "force_mode": "<snapshot>",
    }, "the charge power is no part of a hold and never written back"
    charge = control.wanted("grid_charge", 1.234)
    assert charge == {
        "max_discharge_power": "<snapshot>", "rs485_control": True, "charge_power": 1200.0, "force_mode": "charge",
    }, "W, rounded down to the entity's 50 W step"
    # Never above the Venus E's own 2500 W, so the entity never refuses it.
    assert control.wanted("grid_charge", 9.0)["charge_power"] == 2500.0
    # The battery's own self-consumption covers the house, and normal is everything as found.
    assert all(value == "<snapshot>" for value in control.wanted("discharge_house", 2.0).values())
    assert all(value == "<snapshot>" for value in control.wanted("self_consume", 0.0).values())
    # No discharge to the grid (not mapped), and the hold stores the sun (not idle).
    assert not control.supports("export_discharge") and not control.hold_idle()
    rows = [row for row in table() if row["brand"] == "marstek"]
    assert {row["action"] for row in rows} == {"hold", "grid_charge", "discharge_house", "self_consume"}
    # Taken only in its own self-consumption with RS485 control off; its own RS485 control is not someone else's.
    assert [(rule.label, rule.free) for rule in MARSTEK.modes] == [
        ("operating mode", frozenset({"Auto", "anti_feed"})), ("RS485 control", frozenset({"off"})),
    ]
    assert control.own_states == {"RS485 control": frozenset({"on"})}
    assert control.requires == (("force_mode", frozenset({"standby"}), "force_mode_left_standing"),)


def test_every_role_is_the_integrations_key_and_the_card_offers_to_enable_each() -> None:
    roles = {role.name: role for role in MARSTEK_CONTROL.roles}
    assert set(roles) == set(KEYS)
    for name, (domain, unique_id) in KEYS.items():
        assert [key for key, role in roles.items() if role.match.matches(domain, unique_id)] == [name]
    # Never a role: the battery's own charge ceiling, the max charge power, the discharge setpoint, the work mode, the
    # local API's entities.
    for domain, unique_id in (
        ("number", f"{ENTRY}_charge_to_soc"), ("number", f"{ENTRY}_max_charge_power"),
        ("number", f"{ENTRY}_set_discharge_power"), ("select", f"{ENTRY}_user_work_mode"),
        ("sensor", "aabbccddee01_operating_mode"), ("number", f"{ENTRY}_discharging_cutoff_capacity"),
    ):
        assert not any(role.match.matches(domain, unique_id) for role in roles.values()), unique_id
    assert optin.ENABLE_KEYS["marstek"] == (
        ("number", "max_discharge_power"), ("switch", "rs485_control_mode"), ("number", "set_charge_power"),
        ("select", "force_mode"),
    )
    for role in roles.values():
        assert any(role.match.matches(domain, f"{ENTRY}_{key}") for domain, key in optin.ENABLE_KEYS["marstek"])
    # Only `marstek_modbus` has them: on the local API the status names the integration needed.
    assert set(MARSTEK_CONTROL.hacs_roles) == set(roles)
    assert MARSTEK_CONTROL.hacs_repo == "ViperRNMC/marstek_venus_modbus"


def test_no_dead_man_is_counted_on_and_the_whole_charge_is_on_one_phase() -> None:
    control = MARSTEK_CONTROL
    assert control.expiry == NO_EXPIRY and control.watchdog_roles == () and not control.expiry_by_action
    assert control.import_limit is None and control.autonomous_import is None and control.phase_control is None
    assert control.grid_charge_phases == 1 and control.output_type is None
    assert control.write_cap("setpoint") == 500 and control.write_cap("setting") == 96
    assert {role.name: role.write_class for role in control.roles} == {
        "max_discharge_power": "setting", "rs485_control": "setting", "charge_power": "setpoint",
        "force_mode": "setpoint",
    }
    assert all(role.readback == "register" for role in control.roles) and not control.local_readback
    # Untested on a device: shadow only by default, unless the site is a beta tester's.
    assert not control.field_tested and not field_tested("marstek")
    assert shadow_default(field_tested=False, beta=False) and not shadow_default(field_tested=False, beta=True)


# --- the writes and their read-back ----------------------------------------------------------------------------------


def test_a_hold_writes_the_discharge_limit_alone_and_leaves_rs485_control_off() -> None:
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 6, "hold")
    assert written(results) == [("max_discharge_power", 0.0)]
    assert executor.state.phase == "active" and plant.power_kw() == 0.0
    assert plant.states["rs485_control"] == "off" and plant.ignored == []
    # Handed back: only the limit, to its value found.
    now, results = run(executor, plant, now, 6, "self_consume")
    assert written(results) == [("max_discharge_power", 2500.0)]
    assert plant.states == STATES and executor.state.phase == "idle" and plant.ignored == []


def test_a_charge_turns_rs485_control_on_then_the_power_then_the_mode_each_read_back() -> None:
    executor, plant = _plant()
    now, results = run(executor, plant, T0, 8, "grid_charge", 1.0)
    assert written(results) == [("rs485_control", True), ("charge_power", 1000.0), ("force_mode", "charge")]
    assert [len(result.ops) for result in results[:4]] == [1, 1, 1, 0], "one write a look, each read back first"
    assert len(events(results, "confirmed")) == 3
    assert plant.ignored == [], "no 42xxx register is written while RS485 control is off"
    assert plant.power_kw() == 1.0 and executor.state.phase == "active"
    # What it would write is what the dry run shows, and what handing back would.
    seen = preview(MARSTEK_CONTROL, inputs(Venus(MARSTEK_CONTROL, dict(STATES)), T0, "grid_charge", 1.0))
    assert [(op.role, op.value) for op in seen.writes] == written(results)
    assert [(op.role, op.value) for op in seen.restores] == [
        ("force_mode", "standby"), ("rs485_control", "off"),
    ]
    # A lower rate is the power alone.
    now, results = run(executor, plant, now, 3, "grid_charge", 0.5)
    assert written(results) == [("charge_power", 500.0)] and plant.power_kw() == 0.5


def test_the_hand_back_stops_the_force_first_and_turns_rs485_control_off_last() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 8, "grid_charge", 1.0)
    now, results = run(executor, plant, now, 8, "self_consume")
    assert written(results) == [("force_mode", "standby"), ("rs485_control", "off")]
    assert all(op.restore for result in results for op in result.ops)
    assert _as_found(plant) and plant.states["charge_power"] == "1000" and plant.ignored == [] and executor.state.phase == "idle"
    assert plant.management().state == "free", "back in its own self-consumption"
    # From a charge to a hold: the force stopped, RS485 control off, then the limit.
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 8, "grid_charge", 1.0)
    _now, results = run(executor, plant, now, 10, "hold")
    assert written(results) == [
        ("force_mode", "standby"), ("max_discharge_power", 0.0), ("rs485_control", "off"),
    ]
    assert plant.ignored == [] and executor.state.action == "hold" and plant.power_kw() == 0.0


def test_a_discharge_to_the_house_is_the_batterys_own_mode_and_writes_nothing() -> None:
    executor, plant = _plant()
    _now, results = run(executor, plant, T0, 6, "discharge_house", 2.0)
    assert written(results) == [] and executor.state.phase == "active" and plant.power_kw() == -2.0


def test_it_starts_only_in_self_consumption_with_rs485_off_and_never_over_a_force_left_standing() -> None:
    for mode in ("trade_mode", "manual"):
        executor, plant = _plant(work_mode=mode)
        _now, results = run(executor, plant, T0, 2, "hold")
        assert written(results) == []
        assert events(results, "not_started")[0]["reason"] == f"self_managed:operating mode:{mode}"
    # Someone else's force commands (evcc keeps the work mode at anti_feed meanwhile).
    executor, plant = _plant()
    plant.states["rs485_control"] = "on"
    executor = Executor(MARSTEK_CONTROL)
    _now, results = run(executor, plant, T0, 2, "grid_charge", 1.0, management=Management(
        "self_managed", "RS485 control:on", {"operating mode": "anti_feed", "RS485 control": "on"}))
    assert written(results) == [] and executor.state.phase == "idle"
    # A force mode left at charge would run the moment RS485 control goes on, at whatever power is there.
    executor, plant = _plant()
    plant.states["force_mode"] = "charge"
    _now, results = run(executor, plant, T0, 2, "grid_charge", 1.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "force_mode_left_standing"


def test_a_near_fuse_hands_back_in_one_look_and_a_holds_peak_is_covered_by_the_limit() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 8, "grid_charge", 1.0)
    _now, results = run(executor, plant, now, 1, "grid_charge", 1.0, headroom_a=0.5)
    assert written(results) == [("force_mode", "standby"), ("rs485_control", "off")]
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "hold")
    _now, results = run(executor, plant, now, 2, "hold", headroom_a=1.2)
    assert written(results) == [("max_discharge_power", 2500.0)] and events(results, "peak_cover")


def test_a_charge_that_does_nothing_is_handed_back_and_left_as_found() -> None:
    executor, plant = _plant(obeys=False)
    _now, results = run(executor, plant, T0, 80, "grid_charge", 1.0)
    assert events(results, "hand_back")[0]["reason"] == "no_effect:grid_charge" and executor.state.phase == "blocked"
    assert _as_found(plant)


def test_a_restore_the_battery_reset_is_written_again() -> None:
    """evcc #36110: after an outside stop the maximum powers read 800 W until toggled in the app. A limit that does
    not read back as found is not a hand-back taken."""
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 6, "hold")
    plant.drops.add("max_discharge_power")
    now, results = run(executor, plant, now, 3, "self_consume")
    assert written(results)[0] == ("max_discharge_power", 2500.0)
    assert executor.state.phase != "idle", "not handed back until it reads as found"
    plant.drops.clear()
    _now, results = run(executor, plant, now + timedelta(minutes=10), 30, "self_consume")
    assert plant.states == STATES and executor.state.phase in ("idle", "blocked")


# --- the fuse with Home Assistant stopped ----------------------------------------------------------------------------


def test_a_charge_keeps_the_fuse_when_home_assistant_stops_all_on_the_batterys_phase() -> None:
    """The battery keeps a force charge (no dead-man): the cap takes the whole charge on the phase the house peaks."""
    peaks = {"L1": 6.0, "L2": 9.0, "L3": 4.0}
    for fuse_a in (16.0, 20.0, 25.0):
        cap = grid_charge_cap_kw(fuse_a, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                                 plant_phases=MARSTEK_CONTROL.grid_charge_phases, house_phase_peaks_a=peaks)
        assert cap is not None and max(peaks.values()) + cap * 1000 / VOLTS <= fuse_a - HEADROOM_MARGIN_A + 1e-9
    cap = grid_charge_cap_kw(16.0, 400.0, margin_a=HEADROOM_MARGIN_A, cars_a=0.0, house_peak_kw=None,
                             plant_phases=1, house_phase_peaks_a=peaks)
    executor, plant = _plant()
    run(executor, plant, T0, 8, "grid_charge", min(2.5, cap))
    charge_a = float(plant.states["charge_power"]) * 1000 / 1000 / VOLTS
    assert max(peaks.values()) + charge_a <= 16.0 - HEADROOM_MARGIN_A
    assert plant.states["force_mode"] == "charge" and plant.states["rs485_control"] == "on", "nothing ends it"


# --- the check before control and the hardware test ------------------------------------------------------------------


def test_the_verification_runs_every_mode_and_leaves_the_battery_as_found() -> None:
    from tests.test_battery_verify import _outcomes, _verify

    for quick in (False, True):
        executor, plant = _plant()
        verification = _verify(executor, plant, quick=quick)
        assert verification.done, quick
        assert _outcomes(verification) == {
            "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"
        }, quick
        assert verification.report()["brand"] == "marstek"
        assert _as_found(plant) and plant.ignored == [] and executor.state.phase == "idle"
    # A battery that takes the writes and does nothing fails the check; nothing is left written.
    executor, plant = _plant(obeys=False)
    outcomes = _outcomes(_verify(executor, plant, looks=600))
    assert outcomes["hold"] in ("ignored", "aborted:no_effect:hold") and _as_found(plant)


# --- the integrations it is not for ----------------------------------------------------------------------------------


def test_the_local_api_has_none_of_the_controls_and_is_told_which_integration_to_add() -> None:
    executor, plant = _plant(missing=set(KEYS))
    _now, results = run(executor, plant, T0, 2, "hold")
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "needs_hacs_integration"


def test_a_battery_run_by_omnibattery_is_never_written() -> None:
    # Omnibattery is the battery's one Modbus client and its own controller: no recipe of its own, and its presence
    # marks any battery as run by someone else.
    assert control_for(OMNIBATTERY.brand) is None
    assert [row.strength for row in outside_integrations({"omnibattery", "marstek_modbus"})] == ["runs"]
    executor, plant = _plant()
    _now, results = run(executor, plant, T0, 2, "hold", management=Management(
        "externally_controlled", "integration:omnibattery", {"operating mode": "anti_feed", "RS485 control": "off"}))
    assert written(results) == [] and executor.state.phase == "idle"
    assert events(results, "not_started")[0]["reason"] == "externally_controlled:integration:omnibattery"


def test_the_rearm_after_a_hand_back_holds_for_marstek_too() -> None:
    executor, plant = _plant()
    now, _results = run(executor, plant, T0, 8, "grid_charge", 1.0)
    now, _results = run(executor, plant, now, 8, "self_consume")
    _now, results = run(executor, plant, now, 2, "hold")
    assert written(results) == []
    _now, results = run(executor, plant, now + timedelta(seconds=REARM_S), 4, "hold")
    assert written(results) == [("max_discharge_power", 0.0)]
    assert replace(executor.state).action == "hold"


def test_nothing_is_written_until_a_hardware_test_passed_on_the_plants_firmware() -> None:
    from custom_components.spotnav.battery.commands import hardware_test_first  # noqa: PLC0415

    assert MARSTEK_CONTROL.hardware_test_first and not MARSTEK_CONTROL.field_tested
    assert hardware_test_first("marstek")

