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
from custom_components.spotnav.battery.adapters import SOLIS, adapter_for
from custom_components.spotnav.battery.commands import (
    CONTROLS,
    ACTION_GRID_CHARGE,
    NORMAL,
    SIGENERGY_CONTROL,
    SOLIS_CONTROL,
    SOLIS_RC_STOP,
    control_for,
    field_tested,
    hardware_test_first,
    plant_phases,
    reading,
    service_call,
    table,
)
from custom_components.spotnav.battery.control import BatteryControl
from custom_components.spotnav.battery.executor import (
    LAPSES_A_DAY,
    REARM_S,
    ExecInputs,
    ExecState,
    Executor,
    Ledger,
    Monitor,
    RoleRead,
    StepResult,
    cover_a,
    grid_charge_cap_kw,
    preview,
)
from custom_components.spotnav.battery.facts import Management
from tests import battery_patch
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
    #: The integration has not read the plant yet (after a restart): the force's switches read nothing.
    unread: bool = False

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
        if self.unread:
            switch = ("unavailable", "unavailable")
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
    # Three phases only from the inverter's own type (`test_a_three_phase_solis_is_read_from_the_inverters_own_type`).
    assert control.output_type is not None and control.balanced_output
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
    if kind == "charge":
        # The same kind as SpotNav's own hold: it cannot tell it from its own force with the power moved, so it keeps
        # the action, writes nothing and would stop only for the fuse (`altered`).
        assert events(results, "altered") and executor.state.altered == "rc_force"
    else:
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
    battery_patch.patch(monkeypatch, "plant_entity_id", lambda _hass, _config, _match: None)
    control = BatteryControl.__new__(BatteryControl)
    control.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _id: None))  # type: ignore[assignment]
    control.site = SimpleNamespace(config={})  # type: ignore[assignment]
    control.facts = SimpleNamespace(brand="solis", control_brand="solis", bms_charge_kw=bms_kw)  # type: ignore[assignment]
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
              headroom_a: float = 20.0, deadman: bool = True,
              house_at: Any = None, headroom_of: Any = None, charge_phases: int = 3, output_balanced: bool = False,
              before_look: Any = None) -> tuple[Any, list[StepResult], datetime]:
    """The hardware test's battery part (`Timing.quick`, `stop_on_fail`) as `BatteryControl.async_hardware_test_start`
    builds it, with the dead-man step `_hardware_test_deadman` names right after the hold, on the fake's own clock.
    `headroom_of`: the least headroom on a phase from the site's import now (kW); `before_look`: called with the
    verification and the time before each look."""
    from custom_components.spotnav.battery.monitor import Capability  # noqa: PLC0415
    from custom_components.spotnav.battery.verify import (  # noqa: PLC0415
        TEST_CHARGE_SOC_MAX_PCT, Sample, Timing, Verification,
    )

    expiry = BatteryControl._hardware_test_deadman(None, SOLIS_CONTROL) if deadman else None  # type: ignore[arg-type]
    verification = Verification(
        executor, T0, firmware="59", modes=(*gate.BATTERY_MODES, *((gate.MODE_EXPIRY,) if expiry else ())),
        timing=Timing.quick(), stop_on_fail=True, charge_soc_max_pct=TEST_CHARGE_SOC_MAX_PCT, expiry=expiry,
        expiry_first=expiry is not None,
    )
    capability = Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0)
    now = T0
    results: list[StepResult] = []
    for _ in range(looks):
        if before_look is not None:
            before_look(verification, now)
        if house_at is not None:
            house_kw = plant.house_kw = house_at(now)
        power = plant.at(now).power_kw()
        grid = house_kw + power
        if headroom_of is not None:
            headroom_a = headroom_of(max(grid, 0.0))
        sample = Sample(power, 5.0, plant.soc_pct, house_kw, grid_kw=grid, headroom_a=headroom_a)
        action, rate = verification.command(now, sample, 10.0)
        result = executor.step(inputs(
            plant, now, action, rate, power_kw=power, house_deficit_kw=house_kw, headroom_a=headroom_a,
            grid_import_kw=max(grid, 0.0), expiry_probe=verification.expiry_probe(), charge_phases=charge_phases,
            output_balanced=output_balanced,
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
        "hold": "obeyed", "expiry": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored",
    }
    # The dead-man is timed on the hold right after it passed, before anything is charged.
    assert list(_outcomes(verification)) == ["hold", "expiry", "charge", "discharge_house", "normal"]
    row = verification.report()["modes"]["expiry"]
    timed_at = datetime.fromisoformat(row["commanded_at"])
    assert timed_at < datetime.fromisoformat(verification.report()["modes"]["charge"]["commanded_at"])
    assert not any(amount > 0 for amount in plant.charged[: int((timed_at - T0) / TICK)]), "nothing charged before it"
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


# --- SpotNav's own changes are never someone else's (review round 2) ------------------------------------------------


@pytest.mark.parametrize(
    ("first", "first_kw", "then", "then_kw"),
    [
        ("grid_charge", 3.0, "grid_charge", 1.5), ("grid_charge", 1.5, "grid_charge", 3.0),
        ("export_discharge", 2.0, "export_discharge", 1.0), ("grid_charge", 3.0, "hold", 0.0),
        ("hold", 0.0, "grid_charge", 2.0), ("export_discharge", 2.0, "hold", 0.0),
        ("hold", 0.0, "export_discharge", 2.0),
    ],
)
def test_spotnavs_own_change_of_power_or_action_runs_on_and_is_never_read_as_someone_elses(
    first: str, first_kw: float, then: str, then_kw: float,
) -> None:
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 10, first, first_kw)
    assert executor.state.phase == "active" and executor.state.action == first
    now, results = _run(executor, plant, now, 20, then, then_kw)
    assert not events(results, "external") and not events(results, "left_alone") and not events(results, "hand_back")
    assert executor.state.phase == "active" and executor.state.action == then and not executor.state.given_up
    want = executor.state.target["rc_force"]
    kind, _sep, watts = str(want).partition(":")
    assert plant.reg[43135] == (1 if kind == "charge" else 2)
    assert plant.reg[_POWER_REG[kind]] * 10 == int(watts) == round(then_kw * 1000)
    # It keeps renewing its own force afterwards.
    _now, results = _run(executor, plant, now, 14, then, then_kw)
    assert events(results, "renew") and executor.state.phase == "active"


def test_a_same_kind_force_landing_on_spotnavs_first_call_awaited_is_given_up_not_stopped() -> None:
    executor, plant = _plant()
    now, results = T0, []
    while ("rc_force", "charge:0") not in written(results):
        now, more = _run(executor, plant, now, 1, "hold")
        results += more
    plant.outside_force("charge", 4000)  # lands before the call's read-back
    _now, results = _run(executor, plant, now, 12, "hold")
    assert written(results) == [] and events(results, "external")
    assert plant.reg[43135] == 1 and plant.reg[43136] == 400


def test_a_hand_back_after_a_restart_waits_for_a_reading_and_leaves_a_force_it_did_not_write() -> None:
    def restarted(plant: Hybrid) -> tuple[Executor, datetime]:
        executor = Executor(SOLIS_CONTROL)
        now, _results = _run(executor, plant, T0, 8, "hold")
        stored = executor.as_dict()
        again = Executor(
            SOLIS_CONTROL, Ledger.from_dict(stored["ledger"]), Monitor.from_dict(stored["monitor"]),
            state=ExecState.from_dict(stored["state"]),
        )
        again.restart(now)
        return again, now

    # Nothing read yet: nothing written; once it reads SpotNav's own hold, the hand-back stops it.
    plant = Hybrid(SOLIS_CONTROL, {"grid_charge_allowed": "on"}, idle_kw=-1.8)
    again, now = restarted(plant)
    plant.unread = True
    now, results = _run(again, plant, now, 6, None, stopping="restart")
    assert written(results) == [] and again.state.phase == "restoring"
    plant.unread = False
    _now, results = _run(again, plant, now, 6, None, stopping="restart")
    assert written(results)[0] == ("rc_force", SOLIS_RC_STOP) and plant.reg[43135] == 0
    # Another system's force found once it reads: left as it is, nothing written.
    plant = Hybrid(SOLIS_CONTROL, {"grid_charge_allowed": "on"}, idle_kw=-1.8)
    again, now = restarted(plant)
    plant.unread = True
    now, _results = _run(again, plant, now, 3, None, stopping="restart")
    plant.unread = False
    plant.outside_force("discharge", 3000)
    _now, results = _run(again, plant, now, 6, None, stopping="restart")
    assert written(results) == [] and events(results, "external") and plant.reg[43135] == 2
    # Never read within the command's own window: left as it is (SpotNav's own force has ended by then).
    plant = Hybrid(SOLIS_CONTROL, {"grid_charge_allowed": "on"}, idle_kw=-1.8)
    again, now = restarted(plant)
    plant.unread = True
    _now, results = _run(again, plant, now, 40, None, stopping="restart")
    assert ("rc_force", SOLIS_RC_STOP) not in written(results) and again.state.phase in ("idle", "blocked")
    assert not any(role in ("charge_power", "discharge_power", "rc_timeout") for role, _value in written(results))


def test_a_power_set_from_outside_under_spotnavs_own_force_is_left_to_the_timeout_or_stopped_for_the_fuse() -> None:
    # Someone moves the charge power under SpotNav's hold: never fought, nothing written, the action runs on.
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 8, "hold")
    plant.reg[43136] = 885
    now, results = _run(executor, plant, now, 6, "hold")
    assert written(results) == [] and events(results, "altered") and executor.state.phase == "active"
    assert not events(results, "renew") and executor.state.altered == "rc_force"
    # Near the fuse SpotNav ends its own force (only that: the power stays the other writer's).
    _now, results = _run(executor, plant, now, 4, "hold", headroom_a=0.5)
    assert written(results) == [("rc_force", SOLIS_RC_STOP)] and plant.reg[43135] == 0 and plant.reg[43136] == 885
    # A plan change instead: nothing written, the inverter's own timeout ends it.
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 8, "hold")
    plant.reg[43136] = 885
    now, _results = _run(executor, plant, now, 3, "hold")
    _now, results = _run(executor, plant, now, 6, "self_consume")
    assert written(results) == [] and plant.reg[43135] == 1
    # Its own expiry ending it while the action still runs: handed back, not blocked.
    executor, plant = _plant(bms_kw=10.0)
    now, _results = _run(executor, plant, T0, 8, "hold")
    plant.reg[43136] = 885
    _now, results = _run(executor, plant, now, 40, "hold")
    assert any(str(e["reason"]) == "altered_ended:rc_force" for e in events(results, "hand_back"))
    # Not blocked: once re-armed the plan's hold starts afresh (its own power first), nothing stopped by SpotNav.
    assert executor.state.phase != "blocked" and ("rc_force", SOLIS_RC_STOP) not in written(results)
    assert written(results)[:2] in ([], [("charge_power", 0.0), ("rc_force", "charge:0")])


def test_the_dead_man_is_judged_by_its_switches_whatever_the_house_does() -> None:
    # The house moves by kilowatts during the five-minute silence (a heat pump, a kettle): still timed.
    executor, plant = _plant(bms_kw=10.0)
    verification, _results, _end = _test_run(
        plant, executor, house_at=lambda now: 1.8 + (1.5 if (now - T0).total_seconds() // 60 % 2 else 0.0),
    )
    row = verification.report()["modes"]["expiry"]
    assert row["outcome"] == "obeyed" and 290.0 < row["fallback_s"] <= 370.0
    assert any(reading.get("cross_check") == "covering" for reading in row["readings"])


def test_the_inverters_own_top_up_counts_against_a_grid_charge_only_near_its_force_charge_soc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {"solis_modbus_inverter_battery_force_charge_limit": "3000", "solis_modbus_inverter_force_charge_soc": "10"}

    def read(_hass: Any, _config: Any, match: Any) -> str | None:
        return next((value for key, value in values.items() if match is not None and match.matches("number", key)), None)

    battery_patch.patch(monkeypatch, "read_plant_entity", read)
    control = BatteryControl.__new__(BatteryControl)
    control.hass = None  # type: ignore[assignment]
    control.site = SimpleNamespace(config={})  # type: ignore[assignment]
    for soc, counted in ((50.0, False), (16.0, False), (14.0, True), (8.0, True), (None, True)):
        control.facts = SimpleNamespace(brand="solis", soc_pct=soc, reserve_soc_pct=10.0)  # type: ignore[assignment]
        amps = control._own_import_a(SOLIS_CONTROL)
        assert (amps > 0) is counted, soc
    assert SOLIS_CONTROL.autonomous_import is not None and SOLIS_CONTROL.autonomous_import.near_reserve_only


# --- the first tester's run (3.0.0b6, a 25 A fuse, the house at about 4.7 kW) ----------------------------------------


#: The inverter's own type as `solis_modbus` words it (`helpers.decode_inverter_model`, v4.5.0).
S6_EH3P_TYPE = "S6 3-Phase HV Hybrid (12-20kW)(Protocol 3)"
S6_EH1P_TYPE = "1-Phase HV Hybrid Inverter (S6)(Protocol 3)"


def _field_house(executor: Executor, plant: Hybrid, *, model: str | None = None,
                 **kwargs: Any) -> tuple[Any, list[StepResult], datetime]:
    """The tester's site as the test met it: the battery covers a 4.7 kW house, so the grid reads about 0 and the
    worst phase has 24 A left under the 25 A fuse. `model`: what the inverter's own type reads (`None`: unread, the
    charge and the cover all counted on one phase, `grid_charge_phases` 1)."""
    plant.house_kw = 4.7
    return _test_run(
        plant, executor, house_kw=4.7, charge_phases=plant_phases(SOLIS_CONTROL, model),
        output_balanced=SOLIS_CONTROL.balanced_output,
        headroom_of=lambda grid_kw: 24.0 - grid_kw * 1000.0 / (3 * 230.94), **kwargs,
    )


def test_a_three_phase_solis_is_read_from_the_inverters_own_type() -> None:
    for state, phases in (
        (S6_EH3P_TYPE, 3), ("S6 3-Phase HV Hybrid (5-10kW)(Protocol 2)", 3), ("3-Phase LV Hybrid Inverter(Protocol 1)", 3),
        (S6_EH1P_TYPE, 1), ("1-Phase LV Hybrid Inverter(Protocol 2)", 1), ("1-Phase LV Hybrid Inverter (S6)(Protocol 2)", 1),
        # Not determined: the one-phase rule.
        ("Unknown Model(Protocol 3)", 1), ("No definition(Protocol 0)", 1), ("5-15kWh All-in-One Hybrid(Protocol 1)", 1),
        ("13-Phase", 1), ("1-Phase or 3-Phase", 1), ("", 1), (None, 1),
    ):
        assert plant_phases(SOLIS_CONTROL, state) == phases, state
    # Sigenergy's enum is read as before, and is not a Solis type.
    assert plant_phases(SIGENERGY_CONTROL, "L_N") == 1 and plant_phases(SIGENERGY_CONTROL, "l1_l2_l3_n") == 3
    assert plant_phases(SIGENERGY_CONTROL, None) == 3 and plant_phases(SOLIS_CONTROL, "l1_l2_l3_n") == 1


def test_the_shell_reads_the_phases_from_the_model_definition_sensor(monkeypatch: pytest.MonkeyPatch) -> None:
    states: dict[str, str | None] = {}

    def read(_hass: Any, _config: Any, match: Any) -> str | None:
        if match is not None and match.matches("sensor", "solis_modbus_1234_solis_modbus_inverter_model_definition"):
            return states.get("model")
        return None

    battery_patch.patch(monkeypatch, "read_plant_entity", read)
    control = BatteryControl.__new__(BatteryControl)
    control.hass = None  # type: ignore[assignment]
    control.site = SimpleNamespace(config={})  # type: ignore[assignment]
    assert control._plant_phases(SOLIS_CONTROL) == 1  # the sensor missing, disabled or unread
    states["model"] = S6_EH1P_TYPE
    assert control._plant_phases(SOLIS_CONTROL) == 1
    states["model"] = "Unknown Model(Protocol 3)"
    assert control._plant_phases(SOLIS_CONTROL) == 1
    states["model"] = S6_EH3P_TYPE
    assert control._plant_phases(SOLIS_CONTROL) == 3


def test_the_field_site_counted_balanced_has_room_for_the_test_charge() -> None:
    """A 25 A fuse, the worst phase 24 A under it while the battery covers a 4.7 kW house (grid about 0)."""
    _executor, plant = _plant(bms_kw=10.0)

    def look(model: str | None) -> ExecInputs:
        return inputs(
            plant, T0, ACTION_GRID_CHARGE, 2.0, power_kw=-4.7, headroom_a=24.0, grid_import_kw=0.0,
            charge_phases=plant_phases(SOLIS_CONTROL, model), output_balanced=SOLIS_CONTROL.balanced_output,
        )

    def cap(model: str | None) -> float:
        seen = look(model)
        room = grid_charge_cap_kw(seen, 0.0, covered_a=cover_a(seen))
        assert room is not None
        return room

    # Three-phase: the cover comes back a third on each phase (6.8 A), 24 - 6.8 - 2 = 15.2 A on three phases.
    assert cover_a(look(S6_EH3P_TYPE)) == pytest.approx(4700 / 3 / 230.94, abs=0.01)
    assert cap(S6_EH3P_TYPE) == pytest.approx(10.5, abs=0.1) and cap(S6_EH3P_TYPE) >= 2.0
    # Single-phase or not determined: all 20.4 A back on one phase, 1.6 A left on that one phase.
    for model in (S6_EH1P_TYPE, "Unknown Model(Protocol 3)", None):
        assert cover_a(look(model)) == pytest.approx(4700 / 230.94, abs=0.01)
        assert cap(model) == pytest.approx(0.38, abs=0.02), model
    # The fuse rule still holds per phase for the balanced plant: with 8 A left on the worst phase nothing fits.
    tight = replace(look(S6_EH3P_TYPE), headroom_a=8.0)
    assert grid_charge_cap_kw(tight, 0.0, covered_a=cover_a(tight)) == pytest.approx(-0.55, abs=0.05)


def test_the_hardware_test_charge_starts_on_the_field_site_once_it_reads_three_phase() -> None:
    executor, plant = _plant(bms_kw=10.0)
    verification, results, _end = _field_house(executor, plant, model=S6_EH3P_TYPE)
    assert _outcomes(verification)["charge"] == "obeyed", _outcomes(verification)
    assert not events(results, "not_started")
    assert max(plant.charged) > 0.0
    report = verification.report()
    assert gate.hardware_test_missing(report, deadman_required=True) == ()
    assert gate.hardware_test_passed(report, deadman_required=True)
    assert plant.reg[43135] == 0 and executor.state.phase == "idle"


def test_a_single_phase_solis_on_the_field_site_still_has_no_room_for_the_test_charge() -> None:
    executor, plant = _plant(bms_kw=10.0)
    verification, results, _end = _field_house(executor, plant, model=S6_EH1P_TYPE)
    assert _outcomes(verification)["charge"] == "aborted:fuse_headroom"
    assert max(plant.charged) == 0.0


def test_a_charge_with_no_room_under_the_fuse_leaves_the_test_incomplete_and_the_other_steps_still_run() -> None:
    executor, plant = _plant(bms_kw=10.0)
    verification, results, _end = _field_house(executor, plant)
    # The inverter's type unread, so one phase. The charge never started: once the battery stops covering 4.7 kW
    # (20.4 A, all counted on one phase) the 24 A
    # left keep 2 A of margin and leave 1.6 A, 0.38 kW on one phase, under the least charge of 0.5 kW.
    assert _outcomes(verification) == {
        "hold": "obeyed", "expiry": "obeyed", "charge": "aborted:fuse_headroom", "discharge_house": "obeyed",
        "normal": "restored",
    }
    assert [e["reason"] for e in events(results, "not_started")] == ["fuse_headroom"]
    assert max(plant.charged) == 0.0, "no charge was ever commanded"
    report = verification.report()
    assert gate.hardware_test_missing(report, deadman_required=True) == (("charge", "aborted:fuse_headroom"),)
    assert not gate.hardware_test_passed(report, deadman_required=True)
    assert plant.reg[43135] == 0 and executor.state.phase == "idle"


def test_a_step_aborted_for_a_fault_still_skips_the_rest_of_the_test() -> None:
    # The site's measurement goes stale as the dead-man step starts: nothing more is commanded, normal hands back.
    executor, plant = _plant(bms_kw=10.0)
    from custom_components.spotnav.battery.verify import Sample, Timing, Verification  # noqa: PLC0415
    from custom_components.spotnav.battery.monitor import Capability  # noqa: PLC0415

    verification = Verification(
        executor, T0, firmware="59", modes=(*gate.BATTERY_MODES, gate.MODE_EXPIRY), timing=Timing.quick(),
        stop_on_fail=True, expiry=SOLIS_CONTROL.expiry, expiry_first=True,
    )
    now = T0
    for _ in range(200):
        power = plant.at(now).power_kw()
        current = verification.current
        stale = current is not None and current.mode == "expiry"
        sample = Sample(power, 5.0, plant.soc_pct, 1.8, grid_kw=1.8 + power, headroom_a=20.0)
        action, rate = verification.command(now, sample, 10.0)
        result = executor.step(inputs(
            plant, now, action, rate, power_kw=power, house_deficit_kw=1.8, grid_import_kw=max(1.8 + power, 0.0),
            measurement_ok=not stale, expiry_probe=verification.expiry_probe(),
        ))
        for op in result.ops:
            plant.apply(op)
        verification.after(now, result, sample, Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0))
        now += TICK
        if verification.done:
            break
    outcomes = _outcomes(verification)
    assert outcomes["hold"] == "obeyed" and outcomes["expiry"] == "aborted:stale_data"
    assert outcomes["charge"] == outcomes["discharge_house"] == "skipped"
    assert not gate.moment_abort(outcomes["expiry"]) and gate.moment_abort("aborted:fuse_headroom")
    assert gate.moment_abort("aborted:headroom_too_small") and not gate.moment_abort("aborted:write_not_taken:rc_force")
    assert not gate.moment_abort("not_judgeable:fuse_headroom") and not gate.moment_abort(None)


def test_the_dead_man_step_waits_for_the_battery_to_cover_the_house_again_after_the_hold() -> None:
    # A battery slow to take the house up again after the hold's hand-back: the step waits (the executor's own
    # re-arm is as long) and is then timed, instead of finding nothing to stop.
    executor, plant = _plant(bms_kw=10.0)
    slow: dict[str, Any] = {}

    def lag(verification: Any, now: datetime) -> None:
        current = verification.current
        if current is not None and current.mode == "expiry" and current.commanded_at is None:
            slow.setdefault("since", now)
            plant.house_kw = 0.0 if (now - slow["since"]).total_seconds() < 60 else 1.8
        elif "since" in slow:
            plant.house_kw = 1.8

    verification, _results, _end = _test_run(plant, executor, before_look=lag)
    assert slow and _outcomes(verification)["expiry"] == "obeyed"
    assert gate.hardware_test_passed(verification.report(), deadman_required=True)


def test_no_recipes_balanced_charge_cap_rests_on_an_expiry_the_hardware_test_times() -> None:
    # The dead-man's place before the charge lifts no cap: only a fallback within the minute, measured twice by the
    # routine, ever stands in for the HA-down cap, and no recipe's own expiry is one (Solis's is five minutes).
    for control in CONTROLS:
        base = control.with_phases(False)
        assert not gate.expiry_lifts(base.expiry, measured=True, slow=base.slow, fixed_charge=base.fixed_charge)
