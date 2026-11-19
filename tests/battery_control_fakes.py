"""Fake batteries for the write path's tests: entity states per role that the executor's writes change the way the
real integrations do (checked against their source, see `battery/commands.py`), with the failure modes the
research names: a read-only integration that drops writes silently, an entity that goes unavailable, another
system that changes a value, a battery that ignores what it is told."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from custom_components.spotnav.battery.commands import BrandControl, answered, service_call
from custom_components.spotnav.battery.executor import ExecInputs, Executor, Op, RoleRead, StepResult
from custom_components.spotnav.battery.facts import BatteryFacts, Management

T0 = datetime.fromisoformat("2026-11-14T02:00:00+01:00")
TICK = timedelta(seconds=10)

SIGENERGY_STATES = {
    "remote_ems": "off",
    "control_mode": "unavailable",
    "charge_limit": "unknown",  # the register's "never set" (Sigenergy 2.0.1 shows it as unknown)
    "discharge_limit": "23.04",
}
SOLAREDGE_STATES = {
    "control_mode": "Remote Control",
    "ac_charge_policy": "Always Allowed",
    "command_mode": "Maximize Self Consumption",
    "charge_limit": "5000",
    "discharge_limit": "5000",
    "command_timeout": "3600",
}
HUAWEI_STATES = {"forcible": "Stopped", "discharge_power": "5000", "charge_power": "5000"}
FERROAMP_STATES = {"command": "unknown"}
SOLAX_STATES = {"power_control": "Disabled", "active_power": "0", "autorepeat": "0", "trigger": "unknown"}

STEPS = {"charge_limit": 0.001, "discharge_limit": 0.001}


@dataclass
class FakeBattery:
    control: BrandControl
    states: dict[str, str]
    #: The battery's power (kW, + charging) with nothing written, and what each action makes it.
    idle_kw: float = -3.0
    obeys: bool = True
    read_only: bool = False
    #: Roles whose service call raises.
    raises: set[str] = field(default_factory=set)
    #: Roles the integration does not have.
    missing: set[str] = field(default_factory=set)
    #: Roles whose writes the integration drops silently (no error, no change).
    drops: set[str] = field(default_factory=set)
    action: str | None = None
    rate_kw: float = 0.0
    writes: list[Op] = field(default_factory=list)
    soc_pct: float = 50.0
    on_grid: bool = True
    work_mode_override: str | None = None
    #: Sigenergy's 40031 keeps its last value while Remote EMS is off; 0 (PCS Remote Control) on a new plant.
    register_40031: str = "PCS Remote Control"
    # SolaX sends on the trigger; this is what the inverter got.
    sent: dict[str, str] = field(default_factory=dict)
    #: The attributes per role of a device that answers each request in them (Ferroamp's ack or nak).
    attributes: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: How many of the next requests a device that answers them refuses (`nak`).
    refuse_next: int = 0

    def reads(self) -> dict[str, RoleRead]:
        out: dict[str, RoleRead] = {}
        for role in self.control.roles:
            if role.name in self.missing:
                continue
            entity_id = f"{role.method if role.method != 'service' else 'sensor'}.{self.control.brand}_{role.name}"
            out[role.name] = RoleRead(
                entity_id=entity_id, state=answered(role, self.states.get(role.name), self.attributes.get(role.name)),
                found=True, enabled=True,
                step=STEPS.get(role.name) if self.control.brand == "sigenergy" else role.step, device_id="DEVICE",
            )
        return out

    def apply(self, op: Op) -> bool:
        """Make one write; `False` when the call raised."""
        self.writes.append(op)
        if op.role in self.raises:
            return False
        if self.read_only or op.role in self.drops:
            return True
        brand = self.control.brand
        if op.method == "switch":
            # As Home Assistant's service would take it (`commands.service_call`).
            self.states[op.role] = "on" if service_call("switch", op.entity_id, op.value)[1] == "turn_on" else "off"
            if brand == "sigenergy":
                # 40031 keeps its last value; on a plant never in Remote EMS that is 0, PCS Remote Control.
                self.states["control_mode"] = self.register_40031 if op.value is True else "unavailable"
        elif op.method == "select" and brand == "sigenergy":
            self.states[op.role] = self.register_40031 = str(op.value)
        elif op.method == "button":
            self.sent = dict(self.states)
        elif op.method == "service":
            value = str(op.value)
            if brand == "ferroamp":
                kind, _, power = value.partition(":")
                self.states[op.role] = f"{kind} ({power})" if power else kind
                # The hub answers on the response topic; the integration keeps it beside the request.
                status = "nak" if self.refuse_next > 0 else "ack"
                self.refuse_next = max(0, self.refuse_next - 1)
                self.attributes[op.role] = {"transId": str(len(self.writes)), "status": status}
            elif value == "stop":
                self.states[op.role] = "Stopped"
            else:
                kind, _, power = value.partition(":")
                self.states[op.role] = f"{'Charging' if kind == 'charge' else 'Discharging'} at {power}W for 30 minutes"
        elif op.method == "number":
            value = float(op.value)
            self.states[op.role] = str(int(value)) if brand in ("solaredge", "huawei", "solax") else f"{value:g}"
        else:
            self.states[op.role] = str(op.value)
        return True

    def management(self) -> Management:
        brand = self.control.brand
        if brand == "sigenergy":
            mode = self.work_mode_override or ("remote_ems" if self.states.get("remote_ems") == "on" else "max_self_consumption")
            state = "free" if mode == "max_self_consumption" else "self_managed"
            return Management(state, "self_consumption" if state == "free" else f"EMS work mode:{mode}", {"EMS work mode": mode})
        if brand == "solaredge":
            mode = self.states["control_mode"]
            state = "free" if mode == "Remote Control" else "self_managed"
            return Management(state, "self_consumption", {"storage control mode": mode})
        if brand in ("huawei", "ferroamp"):
            return Management("free", "self_consumption", {"working mode": "maximise_self_consumption"})
        mode = self.states["power_control"]
        state = "free" if mode == "Disabled" else "self_managed"
        return Management(state, "self_consumption", {"use mode": "Self Use Mode", "remote control": mode})

    def power_kw(self, action: str | None, rate_kw: float) -> float:
        if not self.obeys or action is None:
            return self.idle_kw
        if action == "hold":
            return 0.0
        if action == "grid_charge":
            return rate_kw
        if action == "discharge_house":
            return -2.0
        return self.idle_kw

    def facts(self, power_kw: float) -> BatteryFacts:
        return BatteryFacts(
            battery_entity_id="sensor.battery_power", integration="x", brand=self.control.brand, soc_pct=self.soc_pct,
            power_kw=power_kw, rated_charge_kw=10.0, rated_discharge_kw=10.0, firmware="V100R001C21SPC117",
            on_grid=self.on_grid, management=self.management(),
        )


def inputs(
    battery: FakeBattery, now: datetime, action: str | None, rate_kw: float = 0.0, **overrides: Any
) -> ExecInputs:
    """A look at the fake battery while the executor runs `executed` (what it last confirmed)."""
    power = overrides.pop("power_kw", None)
    if power is None:
        power = battery.power_kw(battery.action, battery.rate_kw)
    base = ExecInputs(
        now=now, facts=battery.facts(power), reads=battery.reads(), action=action, rate_kw=rate_kw,
        battery_age_s=5.0, measurement_ok=True, headroom_a=20.0, grid_import_kw=1.0,
    )
    facts_overrides = {key: overrides.pop(key) for key in list(overrides) if key in ("on_grid", "management")}
    if facts_overrides:
        base = replace(base, facts=replace(base.facts, **facts_overrides))
    return replace(base, **overrides)


def run(
    executor: Executor, battery: FakeBattery, start: datetime, looks: int, action: str | None, rate_kw: float = 0.0,
    **overrides: Any,
) -> tuple[datetime, list[StepResult]]:
    """`looks` looks `TICK` apart: each step's writes are made on the fake before the next look."""
    now = start
    results: list[StepResult] = []
    failed: tuple[str, ...] = ()
    for _ in range(looks):
        result = executor.step(inputs(battery, now, action, rate_kw, failed=failed, **overrides))
        results.append(result)
        failed = tuple(op.role for op in result.ops if not battery.apply(op))
        state = executor.state
        battery.action = state.action if state.phase == "active" else None
        battery.rate_kw = state.rate_kw
        now += TICK
    return now, results


def written(results: list[StepResult]) -> list[tuple[str, Any]]:
    return [(op.role, op.value) for result in results for op in result.ops]


def events(results: list[StepResult], kind: str) -> list[dict[str, Any]]:
    return [event for result in results for event in result.events if event["kind"] == kind]

