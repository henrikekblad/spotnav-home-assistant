"""Solis's control recipe (`commands.SOLIS_CONTROL`, `solis_modbus` v4.5.0) through the executor against a fake hybrid
that does what the integration's source says and what a tester's 25 h log showed: the RC force select whose own write
re-sends the powers and the timeout after 43135 (issue #352), numbers that write only a new value, a force that
reverts by itself after the RC Timeout, a hold that is a force charge at 0 W, a charge the BMS bounds, the hand-back's
order, the check before control and the hardware test's dead-man step, and an outside EMS that drives the force.
Nothing here has run on a plant."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.spotnav.battery import gate, optin
from custom_components.spotnav.battery import control as control_module
from custom_components.spotnav.battery.adapters import SOLIS, adapter_for
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    SOLIS_CONTROL,
    SOLIS_RC_CHARGE,
    SOLIS_RC_DISCHARGE,
    SOLIS_RC_OFF,
    control_for,
    field_tested,
    hardware_test_first,
    table,
)
from custom_components.spotnav.battery.control import BatteryControl
from custom_components.spotnav.battery.executor import Executor, StepResult, preview
from custom_components.spotnav.battery.facts import Management
from custom_components.spotnav.battery.commands import reading, service_call
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, inputs, written

SERIAL = "solis_modbus_2403123456_"
#: The integration's unique ids per role (`helpers.py` `unique_id_generator`, `_binary`).
KEYS = {
    "charge_power": ("number", f"{SERIAL}solis_modbus_inverter_rc_force_charge_power"),
    "discharge_power": ("number", f"{SERIAL}solis_modbus_inverter_rc_force_discharge_power"),
    "rc_timeout": ("number", f"{SERIAL}solis_modbus_inverter_rc_timeout"),
    "rc_force": ("select", f"{SERIAL}43135_select"),
    "grid_charge_allowed": ("switch", "2403123456_43110_5"),
}
#: The select's option with no force running, which SpotNav never writes.
NONE = "None"
STATES = {
    "charge_power": "500", "discharge_power": "1500", "rc_timeout": "5", "rc_force": NONE, "grid_charge_allowed": "on",
}
_MODES = {NONE: 0, SOLIS_RC_CHARGE: 1, SOLIS_RC_DISCHARGE: 2}
_OPTIONS = {mode: option for option, mode in _MODES.items()}


@dataclass
class Hybrid(FakeBattery):
    """A Solis hybrid behind `solis_modbus`. The select's write sets 43135 and re-sends both powers and the timeout
    from the cache (the numbers' last values), which latches them; a number written while a force runs latches too,
    one written while none runs only waits in the cache. A force runs at the latched power (a charge bounded by
    the BMS) and reverts to 0 `timeout` minutes after the select's last write. With no force the inverter's Self-Use
    covers the house."""

    now: datetime = T0
    house_kw: float = 1.8
    #: What the BMS takes now (kW): its charge-current limit times the voltage.
    bms_kw: float = 3.77
    #: The firmware latches the timeout written after 43135 (issue #352); `False`: it keeps its own 5 minutes.
    timeout_latches: bool = True
    #: The force reverts after the timeout (the integration's word); `False`: it stands until written.
    deadman: bool = True
    force: int = 0
    latched: dict[str, float] = field(default_factory=lambda: {"charge_power": 0.0, "discharge_power": 0.0})
    timeout_min: float = 5.0
    rc_at: datetime | None = None
    #: The select's writes: (option, the cache's powers and timeout it re-sent).
    combos: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def at(self, now: datetime) -> Hybrid:
        self.now = now
        if (
            self.deadman and self.force and self.rc_at is not None
            and (now - self.rc_at).total_seconds() > self.timeout_min * 60
        ):
            self._set_force(0)
        return self

    def _set_force(self, mode: int) -> None:
        self.force = mode
        self.states["rc_force"] = _OPTIONS[mode]

    def apply(self, op: Any) -> bool:
        self.writes.append(op)
        if op.role in self.raises:
            return False
        if op.role == "rc_force":
            # As Home Assistant's service would take it (`commands.service_call` with the role's `write_as`).
            option = service_call(
                "select", op.entity_id, op.value, options=SOLIS_CONTROL.role("rc_force").write_as
            )[2]["option"]
            self._set_force(_MODES[option])
            cache = {name: self.states[name] for name in ("charge_power", "discharge_power", "rc_timeout")}
            self.combos.append((option, cache))
            self.latched = {name: float(cache[name]) for name in ("charge_power", "discharge_power")}
            if self.timeout_latches:
                self.timeout_min = float(cache["rc_timeout"])
            self.rc_at = self.now
        elif op.method == "number":
            value = float(op.value)
            if f"{value:g}" == self.states[op.role]:
                return True  # `set_native_value` writes nothing for the value it shows
            self.states[op.role] = f"{value:g}"
            if self.force and op.role in self.latched:
                self.latched[op.role] = value
        return True

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        """The battery's power (kW, + charging) from what the inverter follows, whatever the executor thinks."""
        if not self.obeys:
            return self.idle_kw
        if self.force == 1:
            return min(self.latched["charge_power"] / 1000, self.bms_kw)
        if self.force == 2:
            return -self.latched["discharge_power"] / 1000
        return -self.house_kw

    def management(self) -> Management:
        settings = {
            "storage mode": "Self-Use", "remote force charge": "on" if self.force == 1 else "off",
            "remote force discharge": "on" if self.force == 2 else "off",
        }
        if self.force == 0:
            return Management("free", "self_consumption", settings)
        label = "remote force charge" if self.force == 1 else "remote force discharge"
        return Management("self_managed", f"{label}:on", settings)


def _plant(**kwargs: Any) -> tuple[Executor, Hybrid]:
    return Executor(SOLIS_CONTROL), Hybrid(SOLIS_CONTROL, dict(STATES), idle_kw=-1.8, **kwargs)


def _run(executor: Executor, plant: Hybrid, start: datetime, looks: int, action: str | None, rate_kw: float = 0.0,
         **overrides: Any) -> tuple[datetime, list[StepResult]]:
    now = start
    results: list[StepResult] = []
    for _ in range(looks):
        result = executor.step(inputs(plant.at(now), now, action, rate_kw, power_kw=plant.power_kw(), **overrides))
        results.append(result)
        for op in result.ops:
            plant.apply(op)
        now += TICK
    return now, results


def _as_found(plant: Hybrid) -> bool:
    return plant.states == STATES and plant.force == 0


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_the_rc_force_with_its_powers_and_a_one_minute_timeout() -> None:
    control = SOLIS_CONTROL
    assert control_for("solis") is control and control in CONTROLS and control.adapter is SOLIS
    assert adapter_for("solis_modbus", None) is SOLIS
    assert control.wanted("hold", 3.0) == {
        "charge_power": 0.0, "discharge_power": "<snapshot>", "rc_timeout": 1.0, "rc_force": SOLIS_RC_CHARGE,
    }, "a hold is a force charge at 0 W"
    assert control.wanted("grid_charge", 1.234) == {
        "charge_power": 1230.0, "discharge_power": "<snapshot>", "rc_timeout": 1.0, "rc_force": SOLIS_RC_CHARGE,
    }, "W, rounded down to the register's 10 W"
    assert control.wanted("export_discharge", 2.0) == {
        "charge_power": "<snapshot>", "discharge_power": 2000.0, "rc_timeout": 1.0, "rc_force": SOLIS_RC_DISCHARGE,
    }
    # The timeout is the recipe's own minute whatever window a caller passes.
    assert control.states["grid_charge"](1.0, 1800)[1].value == 1.0
    # The inverter's own Self-Use covers the house: everything as found (no force).
    assert all(value == "<snapshot>" for value in control.wanted("discharge_house", 2.0).values())
    assert all(value == "<snapshot>" for value in control.wanted("self_consume", 0.0).values())
    assert control.enter_order == ("charge_power", "discharge_power", "rc_timeout", "rc_force")
    assert control.restore_order == ("rc_force", "charge_power", "discharge_power", "rc_timeout")
    assert control.mode_limits == ("charge_power", "discharge_power", "rc_timeout")
    assert control.peak_first == {"hold": ("rc_force",), "grid_charge": ("rc_force",)}
    assert not control.hold_idle() and not control.export_signed_by_charge()
    rows = [row for row in table() if row["brand"] == "solis"]
    assert {row["action"] for row in rows} == {"hold", "grid_charge", "discharge_house", "export_discharge", "self_consume"}
    # Taken only in Self-Use with no force running and the select at "None"; its own force is not someone else's.
    assert [(rule.label, rule.free) for rule in SOLIS.modes] == [
        ("storage mode", frozenset({"Self-Use"})), ("remote force charge", frozenset({"off"})),
        ("remote force discharge", frozenset({"off"})),
    ]
    assert control.own_states == {
        "remote force charge": frozenset({"on"}), "remote force discharge": frozenset({"on"}),
    }
    assert control.requires == (("rc_force", frozenset({SOLIS_RC_OFF}), "rc_force_running"),)
    assert control.action_requires == (
        ("grid_charge", "grid_charge_allowed", frozenset({"off"}), "grid_charge_not_allowed"),
    )


def test_the_selects_none_is_compared_by_name_and_written_back_as_none() -> None:
    # "None" is no reading to the ledger and to `reading`: the role compares it as "No force" and writes "None".
    role = SOLIS_CONTROL.role("rc_force")
    assert reading(role, "None") == SOLIS_RC_OFF and reading(role, SOLIS_RC_CHARGE) == SOLIS_RC_CHARGE
    assert reading(role, "unavailable") is None and reading(role, "unknown") is None and reading(role, None) is None
    assert service_call("select", "select.x", SOLIS_RC_OFF, options=role.write_as)[2] == {
        "entity_id": "select.x", "option": "None",
    }
    assert service_call("select", "select.x", SOLIS_RC_CHARGE, options=role.write_as)[2]["option"] == SOLIS_RC_CHARGE
    # Every other select is written as it is.
    assert service_call("select", "select.x", "None")[2]["option"] == "None"
    assert all(not r.write_as for c in CONTROLS if c.brand != "solis" for r in c.roles)


def test_every_role_is_the_integrations_key_and_the_card_offers_to_enable_each() -> None:
    roles = {role.name: role for role in SOLIS_CONTROL.roles}
    assert set(roles) == set(KEYS)
    for name, (domain, unique_id) in KEYS.items():
        assert [key for key, role in roles.items() if role.match.matches(domain, unique_id)] == [name]
    # Never a role: the sensors of the same registers, the force's switches (the adapter's mode rules), the storage
    # mode, the backup SoC, the reserve mode's bit, the time-of-use's period switches, the grid adjustment.
    for domain, unique_id in (
        ("sensor", f"{SERIAL}solis_modbus_inverter_rc_force_charge_power"), ("switch", "2403123456_43135_1"),
        ("switch", "2403123456_43135_2"), ("select", f"{SERIAL}43110_select"),
        ("number", f"{SERIAL}solis_modbus_inverter_backup_soc"), ("switch", "2403123456_43707_0"),
        ("switch", "2403123456_43110_4"), ("select", f"{SERIAL}43132_select"),
    ):
        assert not any(role.match.matches(domain, unique_id) for role in roles.values()), unique_id
    for role in roles.values():
        assert any(role.match.matches(domain, f"2403123456_{key}") for domain, key in optin.ENABLE_KEYS["solis"])


def test_the_dead_man_is_spotnavs_renewal_of_the_select_stated_not_measured() -> None:
    expiry = SOLIS_CONTROL.expiry
    assert (expiry.kind, expiry.renewer, expiry.window_s, expiry.renewed_roles, expiry.renew_s) == (
        "device_deadman", "spotnav", 60.0, ("rc_force",), 20,
    )
    assert expiry.evidence == "stated" and not expiry.measured_on
    assert expiry.fallback_values == (("rc_force", SOLIS_RC_OFF),)
    # Stated: never lifts the HA-down cap; measured on the plant's firmware it would, as any dead-man of SpotNav's.
    testable = {"slow": False, "fixed_charge": False}
    assert not gate.expiry_lifts(expiry, measured=False, **testable)
    assert gate.expiry_lifts(expiry, measured=True, **testable)
    assert gate.expiry_testable(expiry, **testable) and not gate.deadman_testable(expiry, **testable)
    assert gate.expiry_note(expiry, measured=False) == "dead-man stated, not measured"
    assert gate.expiry_energy_kwh(expiry, 3.0) == pytest.approx(0.05)
    assert SOLIS_CONTROL.watchdog_roles == ("rc_force",) and SOLIS_CONTROL.watchdog_renew_s == 20
    assert gate.expiry_measured(expiry, "59|solis_modbus|4.5.0", None) is False


def test_untested_on_a_plant_it_waits_for_a_hardware_test_and_is_bounded_by_its_bms() -> None:
    control = SOLIS_CONTROL
    assert control.hardware_test_first and not control.field_tested
    assert hardware_test_first("solis") and not field_tested("solis")
    assert control.bms_charge_limit and control.charge_power_bound is None
    assert [c.brand for c in CONTROLS if c.bms_charge_limit] == ["solis"]
    assert control.grid_charge_phases == 1 and control.phase_control is None and control.import_limit is None
    assert control.write_cap("setpoint") == 500
    assert {role.name: role.write_class for role in control.roles} == {
        "charge_power": "setpoint", "discharge_power": "setpoint", "rc_timeout": "setpoint", "rc_force": "setpoint",
        "grid_charge_allowed": "setting",
    }
    assert control.local_readback, "the numbers echo: the battery's power judges the effect"


# --- the writes and their read-back ----------------------------------------------------------------------------------


def _first(results: list[StepResult], count: int) -> list[tuple[str, Any]]:
    """The first writes, before the renewals of the select begin."""
    return written(results)[:count]


def test_a_hold_writes_the_power_and_the_timeout_before_the_force_which_latches_them() -> None:
    executor, plant = _plant()
    now, results = _run(executor, plant, T0, 6, "hold")
    assert _first(results, 3) == [("charge_power", 0.0), ("rc_timeout", 1.0), ("rc_force", SOLIS_RC_CHARGE)]
    # The select's own write re-sent the new power and timeout after 43135: the firmware took both.
    assert plant.combos[0] == (SOLIS_RC_CHARGE, {"charge_power": "0", "discharge_power": "1500", "rc_timeout": "1"})
    assert plant.timeout_min == 1.0 and plant.power_kw() == 0.0 and executor.state.phase == "active"
    # Handed back: the force off first ("None" written, read back as no force), then the power and the timeout.
    _now, results = _run(executor, plant, now, 8, "self_consume")
    assert written(results) == [("rc_force", SOLIS_RC_OFF), ("charge_power", 500.0), ("rc_timeout", 5.0)]
    assert plant.combos[-1][0] == NONE
    assert all(op.restore for result in results for op in result.ops)
    assert _as_found(plant) and executor.state.phase == "idle"


def test_the_select_is_renewed_inside_the_timeout_so_the_force_never_lapses() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 6, "hold")
    _now, results = _run(executor, plant, now, 30, "hold")
    renewals = [op for result in results for op in result.ops]
    assert len(renewals) >= 12, "every 20 s over five minutes"
    assert {(op.role, op.value) for op in renewals} == {("rc_force", SOLIS_RC_CHARGE)}, "the select alone"
    assert len(events(results, "renew")) == len(renewals)
    assert plant.force == 1 and plant.power_kw() == 0.0, "the one-minute timeout never ran out"
    assert all(combo == plant.combos[0] for combo in plant.combos), "each renewal re-sends the same power and timeout"


def test_a_charge_writes_the_power_first_and_follows_a_lower_rate_with_the_power_alone() -> None:
    executor, plant = _plant(bms_kw=10.0)
    now, results = _run(executor, plant, T0, 8, "grid_charge", 2.0)
    assert _first(results, 3) == [("charge_power", 2000.0), ("rc_timeout", 1.0), ("rc_force", SOLIS_RC_CHARGE)]
    assert plant.power_kw() == 2.0
    seen = preview(SOLIS_CONTROL, inputs(Hybrid(SOLIS_CONTROL, dict(STATES)), T0, "grid_charge", 2.0))
    assert [(op.role, op.value) for op in seen.writes] == _first(results, 3)
    assert [(op.role, op.value) for op in seen.restores] == [
        ("rc_force", SOLIS_RC_OFF), ("charge_power", 500.0), ("rc_timeout", 5.0),
    ]
    # A lower rate: the number alone, taken at once while the force runs.
    _now, results = _run(executor, plant, now, 3, "grid_charge", 1.0)
    assert [w for w in written(results) if w[0] != "rc_force"] == [("charge_power", 1000.0)]
    assert plant.power_kw() == 1.0


def test_a_change_from_a_hold_ends_the_force_before_the_power_found_goes_back() -> None:
    # From a hold (force charge at 0 W) to the inverter's own Self-Use: the force ended before the power found goes
    # back, so the battery never charges at 500 W in between.
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 6, "hold")
    _now, results = _run(executor, plant, now, 8, "discharge_house", 1.8)
    assert written(results) == [("rc_force", SOLIS_RC_OFF), ("charge_power", 500.0), ("rc_timeout", 5.0)]
    assert plant.force == 0 and executor.state.action == "discharge_house" and plant.power_kw() == -1.8
    # From a charge to a hold: the power falls first, while the force still runs.
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 3.0)
    _now, results = _run(executor, plant, now, 6, "hold")
    assert written(results)[0] == ("charge_power", 0.0) and plant.power_kw() == 0.0


def test_a_discharge_to_the_house_is_self_use_and_renews_nothing() -> None:
    executor, plant = _plant()
    _now, results = _run(executor, plant, T0, 30, "discharge_house", 1.8)
    assert written(results) == [] and not events(results, "renew")
    assert executor.state.phase == "active" and plant.power_kw() == -1.8


def test_a_sale_is_a_force_discharge_at_the_rate_and_back_to_self_use() -> None:
    executor, plant = _plant()
    now, results = _run(executor, plant, T0, 8, "export_discharge", 2.0)
    assert _first(results, 3) == [
        ("discharge_power", 2000.0), ("rc_timeout", 1.0), ("rc_force", SOLIS_RC_DISCHARGE),
    ]
    assert plant.power_kw() == -2.0
    _now, results = _run(executor, plant, now, 8, "self_consume")
    assert written(results) == [("rc_force", SOLIS_RC_OFF), ("discharge_power", 1500.0), ("rc_timeout", 5.0)]
    assert _as_found(plant)


def test_it_starts_only_in_self_use_with_no_force_and_never_over_an_outside_ems() -> None:
    # An outside EMS (Enequi on a tester's plant) drives the force: the switches read on.
    executor, plant = _plant()
    plant._set_force(1)
    _now, results = _run(executor, plant, T0, 2, "hold")
    assert written(results) == [] and executor.state.phase == "idle"
    assert events(results, "not_started")[0]["reason"] == "self_managed:remote force charge:on"
    # Even where the management reads free (its switches' rules not read yet), the select's requirement holds.
    executor, plant = _plant()
    plant._set_force(2)
    _now, results = _run(executor, plant, T0, 2, "hold", management=Management(
        "free", "self_consumption", {"storage mode": "Self-Use"}))
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "rc_force_running"
    # Marked as run by someone else (the EMS's writes counted by `outside_control`): never written.
    executor, plant = _plant()
    _now, results = _run(executor, plant, T0, 2, "hold", management=Management(
        "externally_controlled", "settings_changed", {"storage mode": "Self-Use"}))
    assert written(results) == [] and events(results, "not_started")[0]["reason"].startswith("externally_controlled")


def test_an_outside_ems_writing_the_force_while_spotnav_runs_is_given_up_not_fought() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 6, "hold")
    plant._set_force(2)
    plant.latched["discharge_power"] = 3000.0
    _now, results = _run(executor, plant, now, 12, "hold")
    # Seen as someone else's change, or (landing on a renewal still awaited) as a write not taken: handed back
    # either way, and SpotNav's own force is never written again over the other system's.
    assert ("rc_force", SOLIS_RC_CHARGE) not in written(results), "never written over the other system"
    assert events(results, "hand_back") and executor.state.phase != "active"


def test_a_grid_charge_waits_while_grid_charging_is_not_allowed_and_a_hold_still_runs() -> None:
    executor, plant = _plant()
    plant.states["grid_charge_allowed"] = "off"
    _now, results = _run(executor, plant, T0, 3, "grid_charge", 1.0)
    assert written(results) == []
    assert events(results, "not_started")[0]["reason"] == "grid_charge_not_allowed"
    executor, plant = _plant()
    plant.states["grid_charge_allowed"] = "off"
    _now, results = _run(executor, plant, T0, 6, "hold")
    assert executor.state.phase == "active" and plant.force == 1
    assert "grid_charge_allowed" not in {role for role, _value in written(results)}, "read, never written"


def test_a_near_fuse_hands_back_the_force_first_and_a_holds_peak_is_covered_by_self_use() -> None:
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    _now, results = _run(executor, plant, now, 4, "grid_charge", 1.0, headroom_a=0.5)
    assert [w for w in written(results) if w != ("rc_force", SOLIS_RC_CHARGE)][0] == ("rc_force", SOLIS_RC_OFF)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 6, "hold")
    _now, results = _run(executor, plant, now, 6, "hold", headroom_a=1.2)
    assert events(results, "peak_cover")
    covered = written(results)[written(results).index(("rc_force", SOLIS_RC_OFF)):]
    assert ("rc_force", SOLIS_RC_CHARGE) not in covered, "a covered hold renews no force"
    assert plant.force == 0 and plant.power_kw() == -1.8, "Self-Use covers the peak"


def test_a_force_that_lapsed_by_itself_is_read_as_no_force_and_handed_back() -> None:
    # Renewals that never landed (the link stalled past the minute): the inverter's own fallback reads as no force,
    # a change SpotNav does not fight (never someone else's force) and the hold no longer holds.
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 6, "hold")
    plant.apply = _dropping(plant, "rc_force")  # type: ignore[method-assign]
    _now, results = _run(executor, plant, now, 40, "hold")
    assert plant.force == 0
    assert executor.state.phase != "active"


def _dropping(plant: Hybrid, role: str) -> Any:
    apply = Hybrid.apply

    def drop(op: Any) -> bool:
        if op.role == role and op.value != SOLIS_RC_OFF:
            plant.writes.append(op)
            return True
        return apply(plant, op)

    return drop


# --- the BMS bounds a grid charge -------------------------------------------------------------------------------------


def _bare(bms_kw: float | None, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(control_module, "plant_entity_id", lambda _hass, _config, _match: None)
    control = BatteryControl.__new__(BatteryControl)
    control.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _id: None))  # type: ignore[assignment]
    control.site = SimpleNamespace(config={})  # type: ignore[assignment]
    control.facts = SimpleNamespace(brand="solis", bms_charge_kw=bms_kw)  # type: ignore[assignment]
    control.executor = None
    control._look_id, control._charge_bound = 0, None
    return control


def test_a_grid_charge_never_asks_more_than_the_bms_takes_now(monkeypatch: pytest.MonkeyPatch) -> None:
    # A cold Dyness: 10 A x 377 V holds a force charge at 3.77 kW whatever is asked.
    control = _bare(3.77, monkeypatch)
    assert control._bms_charge_kw(SOLIS_CONTROL) == 3.77
    assert control._within_charge_power(None) == 3.77 and control._within_charge_power(8.8) == 3.77
    assert control._within_charge_power(2.0) == 2.0, "it never raises a cap"
    # A full or a cold pack that takes nothing: no grid charge at all.
    assert _bare(0.0, monkeypatch)._within_charge_power(5.0) == 0.0
    # Not read: no bound of the BMS's (the fuse's own cap stands).
    assert _bare(None, monkeypatch)._within_charge_power(5.0) == 5.0
    # No other recipe is bounded by it, whatever the facts read.
    for brand in ("marstek", "goodwe", "sigenergy"):
        other = control_for(brand)
        assert other is not None and control._bms_charge_kw(other) is None


def test_the_bms_charge_limit_is_read_live_and_never_learnt() -> None:
    from tests.test_battery_facts import _facts  # noqa: PLC0415

    facts = _facts("solis")
    # 25 A at 400 V in the fixture.
    assert facts.bms_charge_kw == pytest.approx(10.0)
    facts = _facts("solis", **{"sensor.solis_s6_eh3p_battery_charge_current_limitation_bms": {"state": "0"}})
    assert facts.bms_charge_kw == 0.0, "the BMS takes nothing now"
    assert _facts("solis", **{"sensor.solis_s6_eh3p_battery_voltage_bms": {"state": "unavailable"}}).bms_charge_kw is None
    # Learning lifts the rating, never this.
    from custom_components.spotnav.battery.facts import BatteryFacts  # noqa: PLC0415

    assert replace(BatteryFacts(bms_charge_kw=3.7), rated_charge_kw=7.2).bms_charge_kw == 3.7


def test_a_charge_the_bms_holds_lower_is_still_a_charge_taken_at_the_bounded_rate() -> None:
    executor, plant = _plant(bms_kw=3.77)
    _now, results = _run(executor, plant, T0, 40, "grid_charge", 3.7)
    assert not events(results, "hand_back"), "commanded within what the BMS takes"
    assert plant.power_kw() == pytest.approx(3.7)


# --- the check before control and the hardware test ------------------------------------------------------------------


def _test_run(plant: Hybrid, executor: Executor, *, looks: int = 200, house_kw: float = 1.8,
              headroom_a: float = 20.0, deadman: bool = True) -> tuple[Any, list[StepResult], datetime]:
    """The hardware test's battery part (`Timing.quick`, `stop_on_fail`) as `BatteryControl.async_hardware_test_start`
    builds it, with the dead-man step where `_hardware_test_deadman` names one, on the fake's own clock."""
    from custom_components.spotnav.battery.monitor import Capability  # noqa: PLC0415
    from custom_components.spotnav.battery.verify import (  # noqa: PLC0415
        TEST_CHARGE_SOC_MAX_PCT, Sample, Timing, Verification,
    )

    expiry = BatteryControl._hardware_test_deadman(None, SOLIS_CONTROL) if deadman else None  # type: ignore[arg-type]
    verification = Verification(
        executor, T0, firmware="59", modes=(*gate.BATTERY_MODES, *((gate.MODE_EXPIRY,) if expiry else ())),
        timing=Timing.quick(), stop_on_fail=True, charge_soc_max_pct=TEST_CHARGE_SOC_MAX_PCT, expiry=expiry,
    )
    capability = Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0)
    now = T0
    results: list[StepResult] = []
    for _ in range(looks):
        power = plant.at(now).power_kw()
        grid = house_kw + power
        sample = Sample(power, 5.0, plant.soc_pct, house_kw, grid_kw=grid, headroom_a=headroom_a)
        action, rate = verification.command(now, sample, 10.0)
        result = executor.step(inputs(
            plant, now, action, rate, power_kw=power, house_deficit_kw=house_kw, headroom_a=headroom_a,
            grid_import_kw=max(grid, 0.0), expiry_probe=verification.expiry_probe(),
        ))
        results.append(result)
        for op in result.ops:
            plant.apply(op)
        verification.after(now, result, sample, capability)
        now += TICK
        if verification.done and executor.state.phase in ("idle", "blocked") and not result.ops:
            break
    return verification, results, now


def test_the_hardware_test_times_the_rc_timeout_after_the_hold() -> None:
    assert BatteryControl._hardware_test_deadman(None, SOLIS_CONTROL) is SOLIS_CONTROL.expiry  # type: ignore[arg-type]
    # A recipe already tested on site leaves its SpotNav dead-man to the routine's own mode `expiry`.
    tested = replace(SOLIS_CONTROL, hardware_test_first=False)
    assert BatteryControl._hardware_test_deadman(None, tested) is None  # type: ignore[arg-type]
    executor, plant = _plant(bms_kw=10.0)
    verification, results, end = _test_run(plant, executor)
    report = verification.report()
    outcomes = {mode: row["outcome"] for mode, row in report["modes"].items()}
    assert outcomes == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "expiry": "obeyed", "normal": "restored",
    }
    row = report["modes"]["expiry"]
    # The fake reverts 60 s after the select's last write, seen at the next look.
    assert row["scope"] == "hold" and row["window_s"] == 60.0 and 50.0 < row["fallback_s"] <= 70.0
    assert events(results, "expired") and not events(results, "diverged"), "the select read back at no force"
    assert gate.hardware_test_passed(report)
    assert _as_found(plant) and executor.state.phase == "idle"
    assert end - T0 <= timedelta(minutes=15)


def test_a_firmware_that_keeps_its_own_five_minutes_fails_the_dead_man_step() -> None:
    # Issue #352: without the combo the timeout "falls back to about 5 minutes regardless of what is set".
    executor, plant = _plant(bms_kw=10.0, timeout_latches=False)
    verification, _results, _end = _test_run(plant, executor)
    outcomes = {mode: row["outcome"] for mode, row in verification.report()["modes"].items()}
    assert outcomes["expiry"] == "ignored" and outcomes["normal"] == "restored"
    assert not gate.hardware_test_passed(verification.report())
    assert _as_found(plant)
    # No dead-man at all: the same, and the force SpotNav left is handed back as ever.
    executor, plant = _plant(bms_kw=10.0, deadman=False)
    verification, _results, _end = _test_run(plant, executor)
    assert verification.report()["modes"]["expiry"]["outcome"] == "ignored" and _as_found(plant)


def test_the_routine_runs_every_mode_and_leaves_the_battery_as_found() -> None:
    executor, plant = _plant(bms_kw=10.0)
    verification, _results, _end = _test_run(plant, executor, deadman=False, looks=400)
    outcomes = {mode: row["outcome"] for mode, row in verification.report()["modes"].items()}
    assert outcomes == {"hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"}
    assert verification.report()["brand"] == "solis" and _as_found(plant)
