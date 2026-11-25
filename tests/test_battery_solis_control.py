"""Solis's control recipe (`commands.SOLIS_CONTROL`, `solis_modbus` v4.5.0) through the executor against a fake hybrid
that does what the integration's source says and what a tester's 25 h log showed.

The fake keeps the inverter's registers as the integration's register cache does: a register changes only when a
write was executed (a number shows its own value at once, its register only once the write went through), a
service's write batch may be lost whole or after its first write, and the RC force reverts by itself when its
timeout runs out after the last write that restarts it. Tested: the recipe as data, a force sent with its power and
timeout in one call after both read back by their registers, renewals bounded, the hand-back's order (never the
found power), a lost write never running a force at a power SpotNav did not see taken, an outside EMS's force never
written over at any point of the renewal cycle, a force that lapses by itself, the BMS bound, the restart restore,
and the hardware test's dead-man step. Nothing here has run on a plant."""

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
    NORMAL,
    SOLIS_CONTROL,
    SOLIS_RC_STOP,
    control_for,
    field_tested,
    hardware_test_first,
    reading,
    service_call,
    table,
)
from custom_components.spotnav.battery.control import BatteryControl
from custom_components.spotnav.battery.executor import (
    LAPSES_A_DAY,
    REARM_S,
    ExecState,
    Executor,
    Ledger,
    Monitor,
    RoleRead,
    StepResult,
    preview,
)
from custom_components.spotnav.battery.facts import Management
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, inputs, written

SERIAL = "solis_modbus_2403123456_"
#: Where each role is found (`helpers.py` `unique_id_generator`, `_binary`).
KEYS = {
    "rc_force": ("switch", "2403123456_43135_1"),
    "charge_power": ("number", f"{SERIAL}solis_modbus_inverter_rc_force_charge_power"),
    "discharge_power": ("number", f"{SERIAL}solis_modbus_inverter_rc_force_discharge_power"),
    "rc_timeout": ("number", f"{SERIAL}solis_modbus_inverter_rc_timeout"),
    "grid_charge_allowed": ("switch", "2403123456_43110_5"),
}
#: An outside EMS's last setpoints left in the registers (the tester's log shows up to 8 850 W).
FOUND = {43135: 0, 43136: 885, 43129: 150, 43282: 5}
_POWER_REG = {"charge": 43136, "discharge": 43129}


@dataclass
class Hybrid(FakeBattery):
    """A Solis hybrid behind `solis_modbus` (see the module)."""

    now: datetime = T0
    house_kw: float = 1.8
    #: What the BMS takes now (kW).
    bms_kw: float = 3.77
    #: The force reverts after its timeout; `False`: it stands until written.
    deadman: bool = True
    #: Only a change of 43135 restarts the timer (a firmware whose renewal would not keep the force alive).
    restart_on_change_only: bool = False
    reg: dict[int, int] = field(default_factory=lambda: dict(FOUND))
    #: What each number entity shows (its own optimistic value after a write, else its register).
    shown: dict[str, str] = field(default_factory=dict)
    rc_at: datetime | None = None
    #: The next service batch is lost: `all`, or `after_first` (the link dropped after 43135 went out).
    lose_next: str | None = None
    #: Every service batch lost while set (a link that stalls).
    lose_all: bool = False
    #: Number roles whose writes the inverter never takes (the entity still shows the value).
    number_fails: set[str] = field(default_factory=set)
    #: The service calls made: (service, data).
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: The most the battery charged at under each look (kW), for the safety assertions.
    charged: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._shown_from_registers()

    def _shown_from_registers(self) -> None:
        self.shown = {
            "charge_power": str(self.reg[43136] * 10), "discharge_power": str(self.reg[43129] * 10),
            "rc_timeout": str(self.reg[43282]),
        }

    def _write(self, register: int, value: int) -> None:
        before = self.reg[register]
        self.reg[register] = value
        if register == 43135 and value:
            if not self.restart_on_change_only or before != value:
                self.rc_at = self.now
        elif register == 43282 and self.reg[43135] and not self.restart_on_change_only:
            self.rc_at = self.now
        if register in (43136, 43129, 43282):
            name = {43136: "charge_power", 43129: "discharge_power", 43282: "rc_timeout"}[register]
            self.shown[name] = str(value * 10 if register != 43282 else value)

    def at(self, now: datetime) -> Hybrid:
        self.now = now
        if (
            self.deadman and self.reg[43135] and self.rc_at is not None
            and (now - self.rc_at).total_seconds() > self.reg[43282] * 60
        ):
            self.reg[43135] = 0
        self.charged.append(max(self.power_kw(), 0.0))
        return self

    def reads(self) -> dict[str, RoleRead]:
        switch = {1: ("on", "off"), 2: ("off", "on")}.get(self.reg[43135], ("off", "off"))
        sensors = {43136: str(self.reg[43136] * 10), 43129: str(self.reg[43129] * 10), 43282: str(self.reg[43282])}
        own = {
            "rc_force": switch[0], "charge_power": self.shown["charge_power"],
            "discharge_power": self.shown["discharge_power"], "rc_timeout": self.shown["rc_timeout"],
            "grid_charge_allowed": self.states.get("grid_charge_allowed", "on"),
        }
        parts = {
            "rc_force": (switch[1], sensors[43136], sensors[43129]), "charge_power": (sensors[43136],),
            "discharge_power": (sensors[43129],), "rc_timeout": (sensors[43282],),
        }
        out: dict[str, RoleRead] = {}
        for role in self.control.roles:
            if role.name in self.missing:
                continue
            state = own[role.name]
            if role.compose is not None:
                state = role.compose(state, parts[role.name])
            out[role.name] = RoleRead(
                entity_id=f"{role.method}.solis_{role.name}", state=state, found=True, enabled=True, step=role.step,
                device_id=None,
            )
        return out

    def apply(self, op: Any) -> bool:
        self.writes.append(op)
        role = self.control.role(op.role)
        if op.role == "rc_force":
            domain, service, data = service_call(
                "service", op.entity_id, op.value, services=role.services, device_field=role.device_field,
                duration=role.lease, watchdog_s=int(self.control.expiry.window_s or 0),
                power_field=role.power_field, duration_field=role.duration_field,
            )
            service = f"{domain}.{service}"
            self.calls.append((service, data))
            lose, self.lose_next = ("all" if self.lose_all else self.lose_next), None
            if lose == "all":
                return True  # queued, then dropped whole: nothing reached the inverter
            if service.endswith("stop_force_charge_discharge"):
                self._write(43135, 0)
                return True
            kind = "charge" if service.endswith("force_battery_charge") else "discharge"
            self._write(43135, 1 if kind == "charge" else 2)
            if lose == "after_first":
                return True
            self._write(_POWER_REG[kind], round(data["power_watts"] / 10))
            self._write(43282, int(data["duration_minutes"]))
            return True
        if op.method == "number":
            value = float(op.value)
            self.shown[op.role] = f"{value:g}"
            if op.role not in self.number_fails:
                register = {"charge_power": 43136, "discharge_power": 43129, "rc_timeout": 43282}[op.role]
                self._write(register, round(value / 10) if register != 43282 else round(value))
            return True
        raise AssertionError(f"never written: {op.role}")

    def outside_force(self, kind: str, watts: int) -> None:
        """An outside EMS writes its own force (43135 with its power and timeout)."""
        self.reg[43135] = 1 if kind == "charge" else 2
        self.reg[_POWER_REG[kind]] = watts // 10
        self.rc_at = self.now

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        """The battery's power (kW, + charging) from the inverter's registers, whatever the executor thinks."""
        if not self.obeys:
            return self.idle_kw
        if self.reg[43135] == 1:
            return min(self.reg[43136] * 10 / 1000, self.bms_kw)
        if self.reg[43135] == 2:
            return -self.reg[43129] * 10 / 1000
        return -self.house_kw

    def management(self) -> Management:
        force = self.reg[43135]
        settings = {
            "storage mode": "Self-Use", "remote force charge": "on" if force == 1 else "off",
            "remote force discharge": "on" if force == 2 else "off",
        }
        if force == 0:
            return Management("free", "self_consumption", settings)
        label = "remote force charge" if force == 1 else "remote force discharge"
        return Management("self_managed", f"{label}:on", settings)


def _plant(**kwargs: Any) -> tuple[Executor, Hybrid]:
    return Executor(SOLIS_CONTROL), Hybrid(SOLIS_CONTROL, {"grid_charge_allowed": "on"}, idle_kw=-1.8, **kwargs)


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


def _services(plant: Hybrid) -> list[str]:
    return [service.removeprefix("solis_force_battery_").removeprefix("solis_") for service, _data in plant.calls]


# --- the recipe ------------------------------------------------------------------------------------------------------


def test_the_recipe_is_the_rc_force_sent_by_the_integrations_services() -> None:
    control = SOLIS_CONTROL
    assert control_for("solis") is control and control in CONTROLS and control.adapter is SOLIS
    assert adapter_for("solis_modbus", None) is SOLIS
    assert control.wanted("hold", 3.0) == {
        "charge_power": 0.0, "rc_timeout": 5.0, "rc_force": "charge:0",
    }, "a hold is a force charge at 0 W, its power set first"
    assert control.wanted("grid_charge", 1.234) == {
        "charge_power": 1230.0, "rc_timeout": 5.0, "rc_force": "charge:1230",
    }, "W, rounded down to the register's 10 W"
    assert control.wanted("export_discharge", 2.0) == {
        "discharge_power": 2000.0, "rc_timeout": 5.0, "rc_force": "discharge:2000",
    }
    # The inverter's own Self-Use covers the house: the force and the timeout as found; the powers are not handed
    # back as found at all, only to 0.
    assert control.wanted("discharge_house", 2.0) == {"rc_force": "<snapshot>", "rc_timeout": "<snapshot>"}
    assert NORMAL["solis"] == {"charge_power": 0.0, "discharge_power": 0.0}
    assert control.enter_order == ("charge_power", "discharge_power", "rc_timeout", "rc_force")
    assert control.restore_order == ("rc_force", "charge_power", "discharge_power", "rc_timeout")
    assert control.mode_limits == ("charge_power", "discharge_power", "rc_timeout")
    assert control.command_role == "rc_force" and control.peak_first == {"hold": ("rc_force",), "grid_charge": ("rc_force",)}
    assert not control.hold_idle() and not control.export_signed_by_charge()
    rows = [row for row in table() if row["brand"] == "solis"]
    assert {row["action"] for row in rows} == {"hold", "grid_charge", "discharge_house", "export_discharge", "self_consume"}
    # Taken only in Self-Use with no force running and no time-of-use period on; its own force is not someone else's.
    labels = [(rule.label, rule.free) for rule in SOLIS.modes]
    assert labels[:3] == [
        ("storage mode", frozenset({"Self-Use"})), ("remote force charge", frozenset({"off"})),
        ("remote force discharge", frozenset({"off"})),
    ]
    assert [label for label, free in labels[3:] if free == frozenset({"off"})] == [
        *(f"time-of-use charge period {n}" for n in range(1, 7)),
        *(f"time-of-use discharge period {n}" for n in range(1, 7)),
    ]
    assert all(rule.match.matches("switch", f"2403123456_43707_{bit}") for bit, rule in enumerate(SOLIS.modes[3:]))
    assert control.own_states == {
        "remote force charge": frozenset({"on"}), "remote force discharge": frozenset({"on"}),
    }
    assert control.requires == (("rc_force", frozenset({SOLIS_RC_STOP}), "rc_force_running"),)
    assert control.action_requires == (
        ("grid_charge", "grid_charge_allowed", frozenset({"off"}), "grid_charge_not_allowed"),
    )


def test_each_force_is_one_service_call_with_its_power_and_timeout_and_the_stop_is_43135_alone() -> None:
    role = SOLIS_CONTROL.role("rc_force")
    call = lambda value: service_call(  # noqa: E731
        "service", None, value, services=role.services, device_field=role.device_field, duration=role.lease,
        watchdog_s=300, power_field=role.power_field, duration_field=role.duration_field,
    )
    assert call("charge:0") == ("solis_modbus", "solis_force_battery_charge", {"power_watts": 0, "duration_minutes": 5})
    assert call("discharge:2000") == (
        "solis_modbus", "solis_force_battery_discharge", {"power_watts": 2000, "duration_minutes": 5},
    )
    assert call(SOLIS_RC_STOP) == ("solis_modbus", "solis_stop_force_charge_discharge", {})
    # Every other service role still names its device.
    huawei = control_for("huawei")
    assert huawei is not None
    forcible = huawei.role("forcible")
    assert service_call(
        "service", None, "charge:2000", device_id="D", services=forcible.services, duration=forcible.lease,
    )[2] == {"device_id": "D", "power": 2000, "duration": 30}


def test_the_force_is_read_from_its_two_switches_and_the_power_registers() -> None:
    compose = SOLIS_CONTROL.role("rc_force").compose
    assert compose is not None
    assert compose("off", ("off", "8850", "1500")) == SOLIS_RC_STOP
    assert compose("on", ("off", "0", "1500")) == "charge:0"
    assert compose("on", ("off", "2000.0", "1500")) == "charge:2000"
    assert compose("off", ("on", "8850", "3000")) == "discharge:3000"
    # No reading where a part reads nothing, or both switches read on.
    assert compose("unavailable", ("off", "0", "0")) is None
    assert compose("on", ("off", "unknown", "0")) is None
    assert compose("on", ("on", "0", "0")) is None
    assert compose("on", ()) is None
    # The numbers are read back by their registers' sensors, never by their own echo.
    number = SOLIS_CONTROL.role("charge_power").compose
    assert number is not None and number("0", ("8850",)) == "8850"
    assert reading(SOLIS_CONTROL.role("rc_force"), "stop") == "stop"


def test_every_role_is_the_integrations_key_and_the_card_offers_to_enable_each() -> None:
    roles = {role.name: role for role in SOLIS_CONTROL.roles}
    assert set(roles) == set(KEYS)
    for name, (domain, unique_id) in KEYS.items():
        assert [key for key, role in roles.items() if role.match.matches(domain, unique_id)] == [name]
    parts = [match for role in roles.values() for match in role.parts]
    for domain, unique_id in (
        ("switch", "2403123456_43135_2"), ("sensor", f"{SERIAL}solis_modbus_inverter_rc_force_charge_power"),
        ("sensor", f"{SERIAL}solis_modbus_inverter_rc_force_discharge_power"),
        ("sensor", f"{SERIAL}solis_modbus_inverter_rc_timeout"),
    ):
        assert any(match.matches(domain, unique_id) for match in parts), unique_id
    # Never a role or a part: the select (its write re-sends the cache), the storage mode, the backup SoC.
    for domain, unique_id in (
        ("select", f"{SERIAL}43135_select"), ("select", f"{SERIAL}43110_select"),
        ("number", f"{SERIAL}solis_modbus_inverter_backup_soc"),
    ):
        assert not any(role.match.matches(domain, unique_id) for role in roles.values()), unique_id
        assert not any(match.matches(domain, unique_id) for match in parts), unique_id
    # The card offers each to enable; a power's or the timeout's sensor shares its number's unique id.
    for match in (*(role.match for role in roles.values()), *parts):
        assert any(
            match.matches(match.domains[0], f"{SERIAL}{key}") or match.matches(match.domains[0], f"2403123456_{key}")
            for _domain, key in optin.ENABLE_KEYS["solis"]
        ), match


def test_the_dead_man_is_five_minutes_renewed_every_two_bounded_a_day_and_never_lifts_a_cap() -> None:
    control = SOLIS_CONTROL
    expiry = control.expiry
    assert (expiry.kind, expiry.renewer, expiry.window_s, expiry.renewed_roles, expiry.renew_s) == (
        "device_deadman", "spotnav", 300.0, ("rc_force",), 120,
    )
    assert expiry.evidence == "stated" and expiry.fallback_values == (("rc_force", SOLIS_RC_STOP),)
    testable = {"slow": False, "fixed_charge": False}
    # Longer than any window that lifts the HA-down cap, measured or not.
    assert not gate.expiry_lifts(expiry, measured=True, **testable)
    assert not gate.expiry_testable(expiry, **testable) and not gate.deadman_testable(expiry, **testable)
    assert gate.expiry_energy_kwh(expiry, 3.0) == pytest.approx(0.25)
    assert control.renewal_cap == 800 and 86400 // 120 < control.renewal_cap
    assert control.deadman_must_pass and control.hardware_test_first and not control.field_tested
    assert hardware_test_first("solis") and not field_tested("solis")
    assert control.recipe_rev == 2


def test_the_inverters_own_force_charge_below_its_force_charge_soc_is_counted_as_its_own_import() -> None:
    spec = SOLIS_CONTROL.autonomous_import
    assert spec is not None and spec.trigger == "below_reserve" and spec.kw is None and spec.scale == 0.001
    assert spec.entity is not None and spec.entity.matches("number", f"{SERIAL}solis_modbus_inverter_battery_force_charge_limit")
    assert spec.reserve is not None and spec.reserve.matches("sensor", f"{SERIAL}solis_modbus_inverter_force_charge_soc")
    assert "unverified" in spec.why


def test_untested_on_a_plant_it_is_bounded_by_its_bms_and_its_setpoints() -> None:
    control = SOLIS_CONTROL
    assert control.bms_charge_limit and control.charge_power_bound is None
    assert [c.brand for c in CONTROLS if c.bms_charge_limit] == ["solis"]
    assert control.grid_charge_phases == 1 and control.phase_control is None and control.import_limit is None
    assert control.write_cap("setpoint") == 500
    assert {role.name: role.write_class for role in control.roles} == {
        "rc_force": "setpoint", "charge_power": "setpoint", "discharge_power": "setpoint", "rc_timeout": "setpoint",
        "grid_charge_allowed": "setting",
    }
    assert all(role.readback == "register" for role in control.roles) and not control.local_readback


# --- the writes and their read-back ----------------------------------------------------------------------------------


def test_a_hold_sets_the_power_to_0_first_then_sends_the_force_in_one_call() -> None:
    executor, plant = _plant()
    now, results = _run(executor, plant, T0, 8, "hold")
    # The outside EMS's 8 850 W left in 43136 goes to 0, read back by its register, before any force.
    assert written(results)[:2] == [("charge_power", 0.0), ("rc_force", "charge:0")]
    assert plant.calls[0] == ("solis_modbus.solis_force_battery_charge", {"power_watts": 0, "duration_minutes": 5})
    assert plant.reg[43135] == 1 and plant.reg[43136] == 0 and plant.reg[43282] == 5 and plant.power_kw() == 0.0
    assert max(plant.charged) == 0.0 and executor.state.phase == "active"
    # Handed back: the force stopped first (43135 alone), then the other force power to 0 too (the charge power
    # already is), the timeout as found (5).
    _now, results = _run(executor, plant, now, 8, "self_consume")
    assert written(results) == [("rc_force", SOLIS_RC_STOP), ("discharge_power", 0.0)]
    assert plant.calls[-1] == ("solis_modbus.solis_stop_force_charge_discharge", {})
    assert plant.reg[43135] == 0 and executor.state.phase == "idle"


def test_a_power_0_the_inverter_never_took_never_lets_a_force_run_at_the_found_power() -> None:
    # The number shows 0 at once (its own echo), but the write never reaches the inverter: read back by the register
    # it is not taken, the force is never sent, and the battery never charges at the 8 850 W found.
    executor, plant = _plant(number_fails={"charge_power"})
    _now, results = _run(executor, plant, T0, 12, "hold")
    assert plant.shown["charge_power"] == "0" and plant.reg[43136] == 885
    assert ("rc_force", "charge:0") not in written(results) and plant.calls == []
    assert plant.reg[43135] == 0 and max(plant.charged) == 0.0
    assert events(results, "hand_back")
    # A batch that reaches the inverter only in part (43135 and then the link dropped): the force runs at the 0 W
    # already taken, a hold, never at the power found.
    executor, plant = _plant(lose_next=None)
    now, _results = _run(executor, plant, T0, 1, "hold")  # the power write
    plant.lose_next = "after_first"
    _now, results = _run(executor, plant, now, 10, "hold")
    assert plant.reg[43135] == 1 and plant.reg[43136] == 0 and max(plant.charged) == 0.0


def test_a_lost_batch_is_a_write_not_taken_and_starts_no_force() -> None:
    executor, plant = _plant()
    plant.lose_all = True
    _now, results = _run(executor, plant, T0, 12, "hold")
    assert plant.reg[43135] == 0 and max(plant.charged) == 0.0
    assert any(str(e["reason"]).startswith("write_not_taken:rc_force") for e in events(results, "hand_back"))


def test_the_force_is_renewed_every_two_minutes_and_counted() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "hold")
    _now, results = _run(executor, plant, now, 60, "hold")  # ten minutes
    renewals = [op for result in results for op in result.ops]
    assert len(renewals) in (4, 5) and {(op.role, op.value) for op in renewals} == {("rc_force", "charge:0")}
    assert executor.state.renewals_today == len(renewals) == len(events(results, "renew"))
    assert plant.reg[43135] == 1 and plant.power_kw() == 0.0, "the five-minute timeout never ran out"
    assert all(data == {"power_watts": 0, "duration_minutes": 5} for _service, data in plant.calls)


def test_the_days_renewals_spent_hand_back_and_start_nothing_more_that_day() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "hold")
    executor.state = replace(executor.state, renewals_today=SOLIS_CONTROL.renewal_cap or 0)
    now, results = _run(executor, plant, now, 20, "hold")
    assert events(results, "hand_back")[0]["reason"] == "renewal_cap" and plant.reg[43135] == 0
    _now, results = _run(executor, plant, now + timedelta(seconds=REARM_S), 3, "hold")
    # Refused quietly, as the day's other budgets are.
    assert written(results) == [] and executor.state.phase == "idle"
    assert executor._blocker(inputs(plant, now, "hold")) == "renewal_cap"
    # A new day.
    tomorrow = T0 + timedelta(days=1)
    _now, results = _run(executor, plant, tomorrow, 8, "hold")
    assert ("rc_force", "charge:0") in written(results) and executor.state.renewals_today == 0


def test_a_charge_sets_its_power_first_and_hands_back_with_both_powers_at_0_never_the_found_one() -> None:
    executor, plant = _plant(bms_kw=10.0)
    now, results = _run(executor, plant, T0, 8, "grid_charge", 2.0)
    assert written(results)[:2] == [("charge_power", 2000.0), ("rc_force", "charge:2000")]
    assert plant.power_kw() == 2.0
    seen = preview(SOLIS_CONTROL, inputs(Hybrid(SOLIS_CONTROL, {"grid_charge_allowed": "on"}), T0, "grid_charge", 2.0))
    assert [(op.role, op.value) for op in seen.writes] == written(results)[:2]
    _now, results = _run(executor, plant, now, 8, "self_consume")
    assert written(results) == [("rc_force", SOLIS_RC_STOP), ("charge_power", 0.0), ("discharge_power", 0.0)]
    assert plant.reg[43136] == 0 and plant.reg[43129] == 0 and plant.reg[43282] == 5 and plant.reg[43135] == 0
    assert all(value != 8850.0 for _role, value in written(results))


def test_a_found_timeout_goes_back_once_the_force_has_stopped() -> None:
    executor, plant = _plant(bms_kw=10.0)
    plant.reg[43282] = 30
    plant._shown_from_registers()
    now, results = _run(executor, plant, T0, 8, "grid_charge", 2.0)
    # The timeout to 5 before the force (never a force with the found 30 minutes).
    assert written(results)[:3] == [("charge_power", 2000.0), ("rc_timeout", 5.0), ("rc_force", "charge:2000")]
    _now, results = _run(executor, plant, now, 8, "self_consume")
    assert written(results) == [
        ("rc_force", SOLIS_RC_STOP), ("charge_power", 0.0), ("discharge_power", 0.0), ("rc_timeout", 30.0),
    ]
    assert plant.reg[43282] == 30


def test_a_change_of_action_lowers_the_power_before_the_force_and_raises_it_after() -> None:
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 3.0)
    _now, results = _run(executor, plant, now, 6, "hold")
    assert written(results)[0] == ("charge_power", 0.0) and plant.power_kw() == 0.0
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "hold")
    _now, results = _run(executor, plant, now, 8, "discharge_house", 1.8)
    assert written(results)[0] == ("rc_force", SOLIS_RC_STOP)
    assert plant.reg[43135] == 0 and max(plant.charged) == 0.0 and plant.power_kw() == -1.8


def test_a_discharge_to_the_house_is_self_use_and_renews_nothing() -> None:
    executor, plant = _plant()
    _now, results = _run(executor, plant, T0, 30, "discharge_house", 1.8)
    assert written(results) == [] and not events(results, "renew") and plant.calls == []
    assert executor.state.phase == "active" and plant.power_kw() == -1.8


def test_a_sale_is_a_force_discharge_at_the_rate_and_back_to_self_use() -> None:
    executor, plant = _plant()
    now, results = _run(executor, plant, T0, 8, "export_discharge", 2.0)
    assert written(results)[:2] == [("discharge_power", 2000.0), ("rc_force", "discharge:2000")]
    assert plant.power_kw() == -2.0
    _now, results = _run(executor, plant, now, 8, "self_consume")
    assert written(results) == [("rc_force", SOLIS_RC_STOP), ("charge_power", 0.0), ("discharge_power", 0.0)]


# --- another system's force ------------------------------------------------------------------------------------------


def test_it_starts_only_with_no_force_running_and_never_over_an_outside_ems() -> None:
    executor, plant = _plant()
    plant.outside_force("charge", 3000)
    _now, results = _run(executor, plant, T0, 2, "hold")
    assert written(results) == [] and executor.state.phase == "idle"
    assert events(results, "not_started")[0]["reason"] == "self_managed:remote force charge:on"
    executor, plant = _plant()
    plant.outside_force("discharge", 3000)
    _now, results = _run(executor, plant, T0, 2, "hold", management=Management(
        "free", "self_consumption", {"storage mode": "Self-Use"}))
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "rc_force_running"
    executor, plant = _plant()
    _now, results = _run(executor, plant, T0, 2, "hold", management=Management(
        "externally_controlled", "foreign_command", {"storage mode": "Self-Use"}))
    assert written(results) == [] and events(results, "not_started")[0]["reason"].startswith("externally_controlled")


@pytest.mark.parametrize("offset", range(14))
@pytest.mark.parametrize(("kind", "watts"), [("discharge", 3000), ("charge", 8850)])
def test_an_outside_force_landing_at_any_point_of_the_renewal_cycle_is_never_written_over(
    offset: int, kind: str, watts: int,
) -> None:
    # SpotNav holds; the outside EMS writes its own force at some look of the two-minute renewal cycle, also while a
    # renewal is awaited. From then on nothing at all is written: no stop, no power, no timeout.
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8 + offset, "hold")
    plant.outside_force(kind, watts)
    _now, results = _run(executor, plant, now, 20, "hold")
    assert written(results) == [], (offset, written(results))
    assert events(results, "left_alone") and events(results, "external")
    assert {"rc_force", "charge_power", "discharge_power", "rc_timeout"} <= set(executor.state.given_up)
    assert plant.reg[43135] == (1 if kind == "charge" else 2) and plant.reg[_POWER_REG[kind]] == watts // 10
    assert executor.state.phase != "active"


def test_a_grid_charge_waits_while_grid_charging_is_not_allowed_and_a_hold_still_runs() -> None:
    executor, plant = _plant()
    plant.states["grid_charge_allowed"] = "off"
    _now, results = _run(executor, plant, T0, 3, "grid_charge", 1.0)
    assert written(results) == [] and events(results, "not_started")[0]["reason"] == "grid_charge_not_allowed"
    executor, plant = _plant()
    plant.states["grid_charge_allowed"] = "off"
    _now, results = _run(executor, plant, T0, 8, "hold")
    assert executor.state.phase == "active" and plant.reg[43135] == 1


def test_a_near_fuse_stops_the_force_first_and_a_holds_peak_is_covered_by_self_use() -> None:
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 1.0)
    _now, results = _run(executor, plant, now, 4, "grid_charge", 1.0, headroom_a=0.5)
    assert events(results, "hand_back")[0]["reason"] == "fuse_headroom"
    assert [w for w in written(results) if w != ("rc_force", "charge:1000")][0] == ("rc_force", SOLIS_RC_STOP)
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "hold")
    _now, results = _run(executor, plant, now, 30, "hold", headroom_a=1.2)
    assert events(results, "peak_cover")
    covered = written(results)[written(results).index(("rc_force", SOLIS_RC_STOP)):]
    assert ("rc_force", "charge:0") not in covered, "a covered hold renews no force"
    assert plant.reg[43135] == 0 and plant.power_kw() == -1.8


# --- a force that lapses by itself -----------------------------------------------------------------------------------


def test_a_force_that_lapses_by_itself_is_handed_back_and_started_again_not_blocked() -> None:
    executor, plant = _plant()
    now, _results = _run(executor, plant, T0, 8, "hold")
    plant.lose_all = True  # the renewals stall: the five-minute timeout acts
    results: list[StepResult] = []
    while not events(results, "lapsed") and now - T0 < timedelta(minutes=10):
        now, more = _run(executor, plant, now, 1, "hold")
        results += more
    plant.lose_all = False
    now, more = _run(executor, plant, now, 4, "hold")
    results += more
    assert events(results, "lapsed") and not events(results, "external")
    assert any(str(e["reason"]) == "lapsed:rc_force" for e in events(results, "hand_back"))
    assert executor.state.lapses_today == 1 and executor.state.phase != "blocked"
    _now, results = _run(executor, plant, now + timedelta(seconds=REARM_S), 10, "hold")
    assert plant.reg[43135] == 1 and executor.state.phase == "active", "started again"


def test_lapses_repeated_in_a_day_block_the_control_until_it_clears_by_itself() -> None:
    executor, plant = _plant()
    executor.state = replace(executor.state, lapses_today=LAPSES_A_DAY - 1, day=T0.date().isoformat())
    now, _results = _run(executor, plant, T0, 8, "hold")
    plant.lose_all = True
    _now, results = _run(executor, plant, now, 60, "hold")
    assert any(str(e["reason"]) == "lapsed_repeatedly:rc_force" for e in events(results, "hand_back"))
    assert executor.state.phase == "blocked"


# --- a restart mid-force ---------------------------------------------------------------------------------------------


def test_a_restart_mid_force_stops_it_and_puts_the_found_timeout_back_first() -> None:
    executor, plant = _plant(bms_kw=10.0)
    plant.reg[43282] = 10
    plant._shown_from_registers()
    now, _results = _run(executor, plant, T0, 8, "grid_charge", 2.0)
    assert plant.reg[43135] == 1 and plant.reg[43282] == 5
    stored = executor.as_dict()
    again = Executor(
        SOLIS_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
        state=ExecState.from_dict(stored["state"]),
    )
    again.restart(now)
    _now, results = _run(again, plant, now, 10, None, stopping="restart")
    assert written(results) == [
        ("rc_force", SOLIS_RC_STOP), ("charge_power", 0.0), ("discharge_power", 0.0), ("rc_timeout", 10.0),
    ]
    assert plant.reg[43135] == 0 and plant.reg[43282] == 10 and again.state.phase == "idle"


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
    control = _bare(3.77, monkeypatch)
    assert control._bms_charge_kw(SOLIS_CONTROL) == 3.77
    assert control._within_charge_power(None) == 3.77 and control._within_charge_power(8.8) == 3.77
    assert control._within_charge_power(2.0) == 2.0, "it never raises a cap"
    assert _bare(0.0, monkeypatch)._within_charge_power(5.0) == 0.0
    assert _bare(None, monkeypatch)._within_charge_power(5.0) == 5.0
    for brand in ("marstek", "goodwe", "sigenergy"):
        other = control_for(brand)
        assert other is not None and control._bms_charge_kw(other) is None


def test_the_bms_charge_limit_is_read_live_and_never_learnt() -> None:
    from tests.test_battery_facts import _facts  # noqa: PLC0415

    assert _facts("solis").bms_charge_kw == pytest.approx(10.0)
    facts = _facts("solis", **{"sensor.solis_s6_eh3p_battery_charge_current_limitation_bms": {"state": "0"}})
    assert facts.bms_charge_kw == 0.0
    assert _facts("solis", **{"sensor.solis_s6_eh3p_battery_voltage_bms": {"state": "unavailable"}}).bms_charge_kw is None
    from custom_components.spotnav.battery.facts import BatteryFacts  # noqa: PLC0415

    assert replace(BatteryFacts(bms_charge_kw=3.7), rated_charge_kw=7.2).bms_charge_kw == 3.7


# --- the hardware test -----------------------------------------------------------------------------------------------


def _test_run(plant: Hybrid, executor: Executor, *, looks: int = 400, house_kw: float = 1.8,
              headroom_a: float = 20.0, deadman: bool = True) -> tuple[Any, list[StepResult], datetime]:
    """The hardware test's battery part (`Timing.quick`, `stop_on_fail`) as `BatteryControl.async_hardware_test_start`
    builds it, with the dead-man step `_hardware_test_deadman` names, on the fake's own clock."""
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


def _outcomes(verification: Any) -> dict[str, Any]:
    return {mode: row["outcome"] for mode, row in verification.report()["modes"].items()}


def test_the_hardware_test_times_the_five_minute_timeout_from_a_renewal() -> None:
    assert BatteryControl._hardware_test_deadman(None, SOLIS_CONTROL) is SOLIS_CONTROL.expiry  # type: ignore[arg-type]
    tested = replace(SOLIS_CONTROL, hardware_test_first=False)
    assert BatteryControl._hardware_test_deadman(None, tested) is None  # type: ignore[arg-type]
    executor, plant = _plant(bms_kw=10.0)
    verification, results, end = _test_run(plant, executor)
    assert _outcomes(verification) == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "expiry": "obeyed", "normal": "restored",
    }
    row = verification.report()["modes"]["expiry"]
    # Timed from the renewal it waited for: the fake reverts five minutes after it, seen at the next look.
    assert row["window_s"] == 300.0 and 290.0 < row["fallback_s"] <= 370.0
    assert not events(results, "diverged") and not events(results, "external")
    assert gate.hardware_test_passed(verification.report(), deadman_required=True)
    assert plant.reg[43135] == 0 and executor.state.phase == "idle"
    assert end - T0 <= timedelta(minutes=25)
    from custom_components.spotnav.battery.verify import expiry_max_s  # noqa: PLC0415

    assert expiry_max_s(300.0) == 390.0 and expiry_max_s(60.0) == 120.0


def test_a_firmware_whose_renewal_does_not_restart_the_timer_fails_the_dead_man_step() -> None:
    executor, plant = _plant(bms_kw=10.0, restart_on_change_only=True)
    verification, _results, _end = _test_run(plant, executor)
    outcomes = _outcomes(verification)
    assert outcomes["expiry"] == "partial:not_renewed" and outcomes["normal"] == "restored"
    assert not gate.hardware_test_passed(verification.report(), deadman_required=True)
    assert plant.reg[43135] == 0


def test_a_force_that_never_ends_by_itself_fails_the_dead_man_step_and_is_still_stopped() -> None:
    executor, plant = _plant(bms_kw=10.0, deadman=False)
    verification, _results, _end = _test_run(plant, executor)
    assert _outcomes(verification)["expiry"] == "ignored" and plant.reg[43135] == 0


def test_the_dead_man_step_must_pass_for_solis_not_merely_be_tried() -> None:
    report = {"modes": {
        "hold": {"outcome": "obeyed"}, "charge": {"outcome": "obeyed"}, "discharge_house": {"outcome": "obeyed"},
        "expiry": {"outcome": "not_judgeable:house_changed"}, "normal": {"outcome": "restored"},
    }}
    assert gate.hardware_test_passed(report) and not gate.hardware_test_passed(report, deadman_required=True)
    without = {"modes": {name: row for name, row in report["modes"].items() if name != "expiry"}}
    assert not gate.hardware_test_passed(without, deadman_required=True)
    passed = {"modes": {**report["modes"], "expiry": {"outcome": "obeyed"}}}
    assert gate.hardware_test_passed(passed, deadman_required=True)


def test_a_force_standing_while_spotnav_acts_on_nothing_is_a_foreign_command() -> None:
    from custom_components.spotnav.battery.facts import BatteryFacts  # noqa: PLC0415
    from custom_components.spotnav.battery.runtime import BatteryShadow  # noqa: PLC0415

    def seen(settings: dict[str, str], *, brand: str = "solis", acting: bool = False) -> bool:
        shadow = SimpleNamespace(spotnav_acting=lambda: acting)
        facts = BatteryFacts(brand=brand, management=Management("self_managed", "x", settings))
        return BatteryShadow._foreign_command(shadow, facts)  # type: ignore[arg-type]

    assert seen({"remote force charge": "on", "remote force discharge": "off"})
    assert seen({"remote force charge": "off", "remote force discharge": "on"})
    assert not seen({"remote force charge": "off", "remote force discharge": "off"})
    assert not seen({"remote force charge": "on"}, acting=True), "SpotNav's own force"
    # A recipe without a command role (Marstek's RS485 control) keeps the old rules.
    assert not seen({"RS485 control": "on"}, brand="marstek")
