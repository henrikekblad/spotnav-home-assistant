"""EcoFlow PowerOcean through `ef_powerocean_tcpmodbus` (MaxGrmm `56331de`) or `ecoflow_energy` (shuette42 `771b646`):
read only, found by the key in the unique id.

MaxGrmm's registry (`<config entry id>_<key>`) is the fixture `ecoflow` in `tests/fixtures/battery/brands.json`;
shuette42's (`<device sn>_<key>`) is built here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from custom_components.spotnav.battery import commands, gate
from custom_components.spotnav.battery.adapters import ECOFLOW, adapter_for
from custom_components.spotnav.battery.coexist import coexistence_for
from custom_components.spotnav.battery.commands import ECOFLOW_CONTROL
from custom_components.spotnav.battery.executor import Executor, StepResult
from custom_components.spotnav.battery.facts import EntityFact, Management, battery_facts

from tests.test_battery_facts import _facts
from tests.battery_control_fakes import T0, TICK, FakeBattery, events, inputs, written
from tests.test_site_detection import Registry

SN = "HJ31ZDH4ABC123"


def _fact(unique_id: str, state: str, unit: str | None = None, domain: str = "sensor", disabled: bool = False,
          platform: str = "ecoflow_energy") -> EntityFact:
    return EntityFact(
        entity_id=f"{domain}.{unique_id.lower()}", platform=platform, unique_id=unique_id, device_id="inverter",
        config_entry_id="entry", disabled=disabled, state=state, unit=unit,
    )


def _energy(extra: list[EntityFact] | None = None):
    entities = [
        _fact(f"{SN}_batt_w", "-800", "W"),
        _fact(f"{SN}_soc_pct", "58", "%"),
        _fact(f"{SN}_batt_charge_energy_kwh", "300.5", "kWh"),
        _fact(f"{SN}_batt_discharge_energy_kwh", "280", "kWh"),
        # Not the system's: a STREAM unit's own level and pack power, and a second inverter's.
        _fact(f"{SN}_unit_soc_pct", "11", "%"),
        _fact(f"{SN}_unit_batt_w", "5", "W"),
        _fact(f"{SN}_inverter_2_soc_pct", "22", "%"),
        _fact(f"{SN}_inverter_2_batt_w", "6", "W"),
        _fact(f"{SN}_max_charge_soc_pct", "90", "%"),
        _fact(f"{SN}_unit_batt_charge_energy_kwh", "1", "kWh"),
        *(extra or []),
    ]
    return entities, battery_facts(entities[0], entities, manufacturer="EcoFlow", power_kw=-0.8)


def test_both_integrations_are_the_ecoflow_adapter_and_only_modbus_has_a_control() -> None:
    assert adapter_for("ef_powerocean_tcpmodbus", "EcoFlow") is ECOFLOW
    assert adapter_for("ecoflow_energy", "EcoFlow") is ECOFLOW
    # The cloud integration is a different adapter-less one (its power rows only).
    assert adapter_for("ecoflow_cloud", "EcoFlow") is None
    control = commands.control_for("ecoflow")
    assert control is commands.ECOFLOW_CONTROL and control.adapter is ECOFLOW
    # The adapter itself reads only; the recipe writes `ef_powerocean_tcpmodbus`'s controls.
    assert ECOFLOW.hold is None and ECOFLOW.grid_charge is None
    assert control.hacs_repo == "MaxGrmm/EF-PowerOcean-TcpModbus"


def test_modbus_reads_level_size_counters_and_the_apps_reserve() -> None:
    facts = _facts("ecoflow")

    assert facts.brand == "ecoflow"
    assert facts.soc_pct == 62.0
    assert facts.capacity_kwh == pytest.approx(10.24)
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (812.4, 760.1)
    assert facts.reserve_soc_pct == 10.0
    assert facts.sources["capacity"] == "sensor.ecoflow_powerocean_battery_capacity"
    # The BMS's momentary charge limit is no rating.
    assert facts.rated_charge_kw is None and facts.rated_discharge_kw is None
    assert facts.management.state == "free"


@pytest.mark.parametrize("state, managed", [("self_consumption", False), ("ai", True), ("standby", True)])
def test_modbus_is_free_only_in_self_consumption(state: str, managed: bool) -> None:
    facts = _facts("ecoflow", **{"sensor.ecoflow_powerocean_operating_mode": {"state": state}})

    assert (facts.management.state == "self_managed") is managed


def test_modbus_control_held_by_another_client_is_not_free() -> None:
    facts = _facts("ecoflow", **{"binary_sensor.ecoflow_powerocean_modbus_control": {"state": "on"}})

    assert facts.management.state == "self_managed"
    assert facts.management.reason == "Modbus control:on"


def test_a_zero_level_that_the_integration_marks_unavailable_is_no_reading() -> None:
    facts = _facts("ecoflow", **{"sensor.ecoflow_powerocean_battery_soc": {"state": "unavailable"}})

    assert facts.soc_pct is None


def test_ecoflow_energy_reads_the_systems_level_and_counters_not_a_units() -> None:
    _entities, facts = _energy()

    assert facts.brand == "ecoflow"
    assert facts.soc_pct == 58.0
    assert facts.sources["soc"] == f"sensor.{SN.lower()}_soc_pct"
    assert (facts.charged_total_kwh, facts.discharged_total_kwh) == (300.5, 280.0)
    # No size is an entity there (`batt_remaining_wh` is the energy left).
    entities, _ = _energy([_fact(f"{SN}_batt_remaining_wh", "5000", "Wh")])
    assert battery_facts(entities[0], entities, manufacturer="EcoFlow").capacity_kwh is None


def test_ecoflow_energy_without_a_work_mode_cannot_tell_who_runs_it() -> None:
    _entities, facts = _energy()

    assert facts.management.state == "unknown"


@pytest.mark.parametrize(
    "state, expected", [("self_use", "free"), ("self_powered", "free"), ("ai_schedule", "self_managed"),
                        ("intelligent_plus", "self_managed")],
)
def test_ecoflow_energy_work_mode(state: str, expected: str) -> None:
    entities, _ = _energy([_fact(f"{SN}_work_mode", state, domain="select")])

    facts = battery_facts(entities[0], entities, manufacturer="EcoFlow", power_kw=-0.8)

    assert facts.management.state == expected


def test_ecoflow_energy_modbus_control_switch_on_is_not_free() -> None:
    entities, _ = _energy([
        _fact(f"{SN}_work_mode", "self_use", domain="select"),
        _fact(f"{SN}_modbus_control", "on", domain="switch"),
    ])

    facts = battery_facts(entities[0], entities, manufacturer="EcoFlow", power_kw=-0.8)

    assert facts.management.state == "self_managed"


# ---- the power, charge-positive on both ------------------------------------------------------------------------


def test_the_battery_power_is_read_as_it_is_on_both_integrations() -> None:
    r = Registry()
    r.device("pm", model="PowerOcean", manufacturer="EcoFlow", entry="mg")
    r.add("ef_powerocean_tcpmodbus", "powerocean_battery_power", "01JMG_battery_power", device_class="power", unit="W", device="pm", entry="mg")
    r.add("ef_powerocean_tcpmodbus", "powerocean_battery_power_setpoint", "01JMG_battery_power_setpoint", device_class="power", unit="W", device="pm", entry="mg")
    [mg] = r.detect().batteries
    assert mg.entity_id == "sensor.powerocean_battery_power" and mg.inverted is False

    r = Registry()
    r.device("se", model="PowerOcean", manufacturer="EcoFlow", entry="se")
    for key in ("batt_w", "unit_batt_w", "inverter_2_batt_w", "home_from_batt_w"):
        r.add("ecoflow_energy", f"ocean_{key}", f"{SN}_{key}", device_class="power", unit="W", device="se", entry="se")
    [se] = r.detect().batteries
    assert se.entity_id == "sensor.ocean_batt_w" and se.inverted is False


def test_the_coexistence_row_names_the_apps_own_modes_and_no_aggregator() -> None:
    row = coexistence_for(platform="ecoflow_energy")

    assert row is not None and row.brand == "ecoflow" and row.aggregators == ()
    assert coexistence_for(platform="ef_powerocean_tcpmodbus") is row


# ---- the control (`commands.ECOFLOW_CONTROL`) ------------------------------------------------------------------------

#: What the integration shows with Modbus Control off: the mode and the two powers are unavailable.
STATES = {
    "modbus_control": "off", "battery_mode": "unavailable", "charge_power": "unavailable",
    "discharge_power": "unavailable",
}


@dataclass
class PowerOcean(FakeBattery):
    """A three-phase PowerOcean behind `ef_powerocean_tcpmodbus` as its source reads (`control.py`): the switch starts
    the heartbeat, the mode and its powers are available only while it holds control, the integration keeps each
    power and sends only the selected mode's, Modbus Control off resets its mode to Automatic and writes nothing, and
    the inverter obeys what it was last sent until `window_s` after the last heartbeat, then runs on the app's own
    settings (self-consumption: the battery covers the house)."""

    now: datetime = T0
    window_s: float = 60.0
    #: The inverter ignores the heartbeat's end (a firmware without the dead-man).
    no_deadman: bool = False
    presets: dict[str, float] = field(default_factory=lambda: {"charge_power": 2000.0, "discharge_power": 2000.0})
    mode: str = "automatic"
    #: What the inverter follows: the mode and its power (W), and the last heartbeat.
    command: tuple[str, float] = ("app", 0.0)
    beat_at: datetime | None = None

    def _shown(self) -> None:
        on = self.states["modbus_control"] == "on"
        self.states["battery_mode"] = self.mode if on else "unavailable"
        for name, value in self.presets.items():
            self.states[name] = f"{value:g}" if on else "unavailable"

    def _send(self) -> None:
        power = {"charge_battery": "charge_power", "discharge_battery": "discharge_power"}.get(self.mode)
        self.command = (self.mode, 0.0 if power is None else self.presets[power])

    def at(self, now: datetime) -> PowerOcean:
        """The clock moves: the heartbeat beats while the switch is on."""
        self.now = now
        if self.states["modbus_control"] == "on":
            self.beat_at = now
        return self

    def following(self) -> str:
        if self.beat_at is None or (
            not self.no_deadman and (self.now - self.beat_at).total_seconds() > self.window_s
        ):
            return "app"
        return self.command[0]

    def apply(self, op: Any) -> bool:
        self.writes.append(op)
        if op.role in self.raises:
            return False
        if op.role == "modbus_control":
            on = op.value is True or str(op.value).lower() in ("on", "true")
            self.states["modbus_control"] = "on" if on else "off"
            # On: the integration re-sends its mode (Automatic after a reset). Off: its mode back to Automatic and
            # nothing written; the inverter keeps what it has until the window runs out.
            self.mode = "automatic" if not on else self.mode
            if on:
                self.beat_at = self.now
                self._send()
        elif self.states["modbus_control"] != "on":
            return True  # unavailable: Home Assistant calls nothing
        elif op.role == "battery_mode":
            self.mode = str(op.value)
            self._send()
        else:
            self.presets[op.role] = float(op.value)
            self._send()
        self._shown()
        return True

    def power_kw(self, action: str | None = None, rate_kw: float = 0.0) -> float:
        mode = self.following()
        if not self.obeys or mode in ("app", "automatic"):
            return self.idle_kw
        if mode == "hold_battery":
            return 0.0
        return (self.command[1] if mode == "charge_battery" else -self.command[1]) / 1000.0

    def management(self) -> Management:
        switch = self.states["modbus_control"]
        settings = {"operating mode": "self_consumption", "Modbus control": switch}
        if switch == "off":
            return Management("free", "self_consumption", settings)
        return Management("self_managed", "Modbus control:on", settings)


def _ocean(**kwargs: Any) -> tuple[Executor, PowerOcean]:
    return Executor(ECOFLOW_CONTROL), PowerOcean(ECOFLOW_CONTROL, dict(STATES), idle_kw=-2.0, **kwargs)


def _run(executor: Executor, ocean: PowerOcean, start: datetime, looks: int, action: str | None,
         rate_kw: float = 0.0, **overrides: Any) -> tuple[datetime, list[StepResult]]:
    now = start
    results: list[StepResult] = []
    for _ in range(looks):
        result = executor.step(inputs(ocean.at(now), now, action, rate_kw, power_kw=ocean.power_kw(), **overrides))
        results.append(result)
        for op in result.ops:
            ocean.apply(op)
        now += TICK
    return now, results


def test_each_action_writes_modbus_control_on_the_power_and_the_mode() -> None:
    control = commands.control_for("ecoflow")
    assert control is not None
    assert control.wanted("hold", 2.0) == {"modbus_control": True, "battery_mode": "hold_battery"}
    assert control.wanted("grid_charge", 2.35) == {
        "modbus_control": True, "charge_power": 2300.0, "battery_mode": "charge_battery"
    }
    assert control.wanted("discharge_house", 2.0) == {"modbus_control": True, "battery_mode": "automatic"}
    assert control.wanted("export_discharge", 1.0) == {
        "modbus_control": True, "discharge_power": 1000.0, "battery_mode": "discharge_battery"
    }
    # A power that rounds to 0 W would be "no limit" to the inverter: a hold instead.
    assert control.wanted("grid_charge", 0.05) == control.wanted("hold", 0.0)
    assert control.wanted("export_discharge", 0.0) == control.wanted("hold", 0.0)
    # Nothing is snapshot: the hand-back is the brand's normal (Automatic, then the switch off).
    assert control.wanted("self_consume", 0.0) == {}
    assert commands.NORMAL["ecoflow"] == {"modbus_control": False, "battery_mode": "automatic"}
    assert control.restore_order == ("battery_mode", "modbus_control")
    assert control.hold_idle() and not control.export_signed_by_charge() and control.local_readback


def test_the_dead_man_is_the_integrations_and_never_lifts_a_cap() -> None:
    expiry = ECOFLOW_CONTROL.expiry
    assert (expiry.kind, expiry.renewer, expiry.window_s, expiry.evidence) == (
        "device_deadman", "integration", 60.0, "stated"
    )
    assert expiry.silence == (("modbus_control", False),) and ECOFLOW_CONTROL.watchdog_roles == ()
    assert not gate.expiry_lifts(expiry, measured=True, slow=False, fixed_charge=False)
    assert not gate.expiry_testable(expiry, slow=False, fixed_charge=False)
    assert ECOFLOW_CONTROL.hardware_test_first and not ECOFLOW_CONTROL.field_tested
    assert ECOFLOW_CONTROL.grid_charge_phases == 1 and ECOFLOW_CONTROL.import_limit is None
    # A silence is only for a dead-man an integration renews, and only of the control's own roles.
    with pytest.raises(ValueError):
        commands.Expiry("device_deadman", "spotnav", 60.0, ("x",), renew_s=20, silence=(("x", False),))
    with pytest.raises(ValueError):
        replace(ECOFLOW_CONTROL, expiry=replace(ECOFLOW_CONTROL.expiry, silence=(("nothing", False),)))


def test_every_role_is_found_by_maxgrmms_key_and_not_ecoflow_energys_select() -> None:
    keys = {
        "modbus_control": ("switch", "01JENTRY_modbus_control"),
        "battery_mode": ("select", "01JENTRY_battery_mode"),
        "charge_power": ("number", "01JENTRY_charge_battery_power"),
        "discharge_power": ("number", "01JENTRY_discharge_battery_power"),
    }
    for role in ECOFLOW_CONTROL.roles:
        domain, unique_id = keys[role.name]
        assert role.match.matches(domain, unique_id), role.name
    assert not ECOFLOW_CONTROL.role("battery_mode").match.matches("select", f"{SN}_local_battery_mode")
    assert not ECOFLOW_CONTROL.role("modbus_control").match.matches("binary_sensor", "01JENTRY_modbus_control")


def test_a_charge_turns_modbus_control_on_then_the_power_then_the_mode_each_read_back() -> None:
    executor, ocean = _ocean()
    now, results = _run(executor, ocean, T0, 8, "grid_charge", 2.0)
    assert written(results) == [("modbus_control", True), ("battery_mode", "charge_battery")], "2000 W was set"
    assert [len(result.ops) for result in results[:3]] == [1, 1, 0], "one write a look, each read back first"
    assert executor.state.phase == "active" and ocean.power_kw() == 2.0
    # A lower rate is the power alone, and the mode keeps running.
    now, results = _run(executor, ocean, now, 4, "grid_charge", 1.2)
    assert written(results) == [("charge_power", 1200.0)] and ocean.power_kw() == 1.2
    # A hold is the mode alone; the charge's power is left where it was (harmless outside its mode).
    now, results = _run(executor, ocean, now, 4, "hold")
    assert written(results) == [("battery_mode", "hold_battery")] and ocean.power_kw() == 0.0
    assert ocean.presets["charge_power"] == 1200.0


def test_the_hand_back_sends_automatic_first_then_turns_modbus_control_off() -> None:
    executor, ocean = _ocean()
    now, _results = _run(executor, ocean, T0, 8, "grid_charge", 3.0)
    now, results = _run(executor, ocean, now, 6, "self_consume")
    assert written(results) == [("battery_mode", "automatic"), ("modbus_control", False)]
    assert all(op.restore for result in results for op in result.ops)
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot
    # The inverter left the charge at the first write, while the heartbeat still held control.
    assert ocean.command[0] == "automatic" and ocean.power_kw() == -2.0
    assert ocean.states == STATES | {"charge_power": "unavailable"}


def test_it_starts_only_with_modbus_control_off_and_the_app_in_self_consumption() -> None:
    executor, ocean = _ocean()
    ocean.states["modbus_control"] = "on"
    ocean._shown()
    _now, results = _run(executor, ocean, T0, 2, "hold")
    assert written(results) == [] and executor.state.phase == "idle"
    assert events(results, "not_started")[0]["reason"] == "self_managed:Modbus control:on"


def test_turning_modbus_control_off_alone_leaves_the_command_until_the_inverters_window() -> None:
    # What Home Assistant stopping does, and what the hardware test's silence does: the heartbeat ends.
    executor, ocean = _ocean()
    now, _results = _run(executor, ocean, T0, 8, "grid_charge", 1.0)
    ocean.at(now).apply(_op("modbus_control", False))
    assert ocean.states["battery_mode"] == "unavailable" and ocean.power_kw() == 1.0
    assert ocean.at(now + timedelta(seconds=50)).power_kw() == 1.0
    assert ocean.at(now + timedelta(seconds=70)).power_kw() == -2.0


def _op(role: str, value: Any) -> Any:
    from custom_components.spotnav.battery.executor import Op  # noqa: PLC0415

    return Op(role, "switch", f"switch.ecoflow_{role}", value)


# ---- live writes only after the site's own hardware test --------------------------------------------------------------

KEY = "V5.1.37.13 | ef_powerocean_tcpmodbus 2.10.1"


def _record(key: str = KEY, **statuses: str) -> dict[str, Any]:
    steps = {"hold": "passed", "charge": "passed", "discharge": "passed", "hand_back": "passed"} | statuses
    return {"firmware_key": key, "result": "passed", "steps": [
        *({"part": "battery", "step": step, "status": status, "reason": None} for step, status in steps.items()),
        {"part": "heat", "step": "engage", "status": "failed", "reason": "aborted:no_read_back"},
    ]}


def test_a_hardware_test_passes_for_control_on_its_own_firmware_key_only() -> None:
    assert gate.hardware_test_passed(_record(), KEY)
    # Another firmware or integration version asks for the test again; no test, or none read, never passes.
    assert not gate.hardware_test_passed(_record(), "V5.1.38.2 | ef_powerocean_tcpmodbus 2.10.1")
    assert not gate.hardware_test_passed(None, KEY) and not gate.hardware_test_passed(_record(), None)
    # A discharge that could not be judged (a battery near empty) is no failure; the hold, the charge (its sign) and
    # the hand-back must pass, and nothing may fail or stop.
    assert gate.hardware_test_passed(_record(discharge="not_judgeable"), KEY)
    assert not gate.hardware_test_passed(_record(charge="not_judgeable"), KEY)
    assert not gate.hardware_test_passed(_record(discharge="failed"), KEY)
    assert not gate.hardware_test_passed(_record(hand_back="aborted"), KEY)
    assert not gate.hardware_test_passed(_record(deadman="failed"), KEY)
    assert gate.hardware_test_passed(_record(deadman="not_judgeable"), KEY)
    # The battery part skipped altogether is no pass.
    skipped = {"firmware_key": KEY, "steps": [{"part": "battery", "step": "battery", "status": "skipped"}]}
    assert not gate.hardware_test_passed(skipped, KEY)


@pytest.mark.usefixtures("offline_relay")
async def test_control_waits_as_shadow_only_until_the_sites_hardware_test_passed(
    hass: Any, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    from custom_components.spotnav.battery import control as control_module  # noqa: PLC0415
    from custom_components.spotnav.battery.control import NEED_HARDWARE_TEST, BatteryControl  # noqa: PLC0415
    from tests.test_battery_control import _on, _site  # noqa: PLC0415

    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    waiting = replace(commands.SIGENERGY_CONTROL, hardware_test_first=True)
    monkeypatch.setattr(control_module, "control_for", lambda brand, variant=None: waiting.with_hold(variant))
    control = BatteryControl(hass, controller, plan_source=lambda _now: ("hold", 0.0, None))
    await control.async_start()
    _on(hass, site, control)

    # Opted in, shadow only stored off, the routine passed: still only watching, and the checklist says why.
    assert control.options().shadow_only and control.state() == "shadow_only"
    status = control.status()
    assert status["options"]["battery_control_shadow_only"] is False, "the switch shows what the person chose"
    assert NEED_HARDWARE_TEST in status["needs"]
    # The test itself is what it waits for: never refused for it. Sigenergy's has no dead-man step.
    assert control.hardware_test_refusal() is None and not control.hardware_test_deadman()

    # A passed test on the firmware key now: control runs.
    controller.hardware_test.record = _record(control.firmware_key())
    assert not control.options().shadow_only and control.state() == "on"
    assert NEED_HARDWARE_TEST not in control.status()["needs"]
    # One on another key (an update since) does not count.
    controller.hardware_test.record = _record("other | sigen 1.0")
    assert control.state() == "shadow_only"
    # A field-tested recipe never waits.
    monkeypatch.setattr(
        control_module, "control_for", lambda brand, variant=None: replace(waiting, field_tested=True).with_hold(variant)
    )
    assert control.state() == "on"
    await control.async_shutdown()


# ---- the hardware test times the inverter's own dead-man ---------------------------------------------------------------


def _test_run(ocean: PowerOcean, executor: Executor, *, looks: int = 150, house_kw: float = 2.0,
              headroom_a: float = 20.0) -> tuple[Any, list[StepResult], datetime]:
    """The hardware test's battery part (`Timing.quick`, `stop_on_fail`) with the dead-man step, as
    `BatteryControl.async_hardware_test_start` builds it, on the fake's own clock."""
    from custom_components.spotnav.battery.monitor import Capability  # noqa: PLC0415
    from custom_components.spotnav.battery.verify import (  # noqa: PLC0415
        TEST_CHARGE_SOC_MAX_PCT, Sample, Timing, Verification,
    )

    assert gate.deadman_testable(ECOFLOW_CONTROL.expiry, slow=False, fixed_charge=False)
    verification = Verification(
        executor, T0, firmware="V5.1.37.13", modes=(*gate.BATTERY_MODES, gate.MODE_EXPIRY), timing=Timing.quick(),
        stop_on_fail=True, charge_soc_max_pct=TEST_CHARGE_SOC_MAX_PCT, expiry=ECOFLOW_CONTROL.expiry,
    )
    capability = Capability(rated_charge_kw=10.0, rated_discharge_kw=10.0)
    now = T0
    results: list[StepResult] = []
    for _ in range(looks):
        power = ocean.at(now).power_kw()
        grid = house_kw + power
        sample = Sample(power, 5.0, ocean.soc_pct, house_kw, grid_kw=grid, headroom_a=headroom_a)
        action, rate = verification.command(now, sample, 10.0)
        result = executor.step(inputs(
            ocean, now, action, rate, power_kw=power, house_deficit_kw=house_kw, headroom_a=headroom_a,
            grid_import_kw=max(grid, 0.0), expiry_probe=verification.expiry_probe(),
        ))
        results.append(result)
        for op in result.ops:
            ocean.apply(op)
        verification.after(now, result, sample, capability)
        now += TICK
        if verification.done and executor.state.phase in ("idle", "blocked") and not result.ops:
            break
    return verification, results, now


def test_the_dead_man_step_turns_modbus_control_off_alone_and_times_the_inverters_fallback() -> None:
    executor, ocean = _ocean()
    verification, results, end = _test_run(ocean, executor)

    report = verification.report()
    outcomes = {mode: row["outcome"] for mode, row in report["modes"].items()}
    assert outcomes == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "expiry": "obeyed", "normal": "restored"
    }
    row = report["modes"]["expiry"]
    # The fake inverter obeys 60 s after its last heartbeat, seen at the next look.
    assert row["scope"] == "hold" and row["window_s"] == 60.0 and 60.0 <= row["fallback_s"] <= 70.0
    # The silence is the switch off alone, in the hold; then the plant is taken back (the switch on, the inverter's
    # own self-consumption) and handed back as ever.
    silenced = events(results, "silenced")
    assert len(silenced) == 1 and silenced[0]["reason"] == "modbus_control"
    # The dead-man step's writes: the hold (the switch on, Hold Battery), the silence (the switch off alone: the mode
    # is never written while it is unavailable), the plant taken back (the switch on: the integration sends
    # Automatic) and the hand-back (the switch off).
    assert written(results)[-5:] == [
        ("modbus_control", True), ("battery_mode", "hold_battery"), ("modbus_control", False),
        ("modbus_control", True), ("modbus_control", False),
    ]
    assert any(reading.get("retake") == "done" for reading in row["readings"])
    assert executor.state.phase == "idle" and not executor.state.holds_snapshot
    assert ocean.states["modbus_control"] == "off" and ocean.command[0] == "automatic"
    assert end - T0 <= timedelta(minutes=15)


def test_a_firmware_without_the_dead_man_fails_the_step_and_is_still_taken_back() -> None:
    executor, ocean = _ocean(no_deadman=True)
    verification, _results, _end = _test_run(ocean, executor)

    outcomes = {mode: row["outcome"] for mode, row in verification.report()["modes"].items()}
    assert outcomes["expiry"] == "ignored" and outcomes["normal"] == "restored"
    assert gate.mode_verdict(outcomes["expiry"]) == "fail"
    # The hold the inverter kept was ended by taking the plant back: Automatic sent with the heartbeat running.
    assert ocean.command[0] == "automatic" and ocean.states["modbus_control"] == "off"


def test_the_card_shows_the_dead_man_step_before_the_hand_back_only_where_it_runs() -> None:
    from custom_components.spotnav.hardware_test import DEADMAN_STEP, battery_steps  # noqa: PLC0415

    plain = {"modes": {"hold": {"outcome": "obeyed"}, "normal": {"outcome": "restored"}}}
    assert [row["step"] for row in battery_steps(plain, None, False)] == ["hold", "charge", "discharge", "hand_back"]
    timed = {"modes": {**plain["modes"], "expiry": {"outcome": "obeyed"}}}
    rows = battery_steps(timed, "expiry", True)
    assert [row["step"] for row in rows] == ["hold", "charge", "discharge", DEADMAN_STEP[0], "hand_back"]
    assert rows[3]["status"] == "passed"
    assert battery_steps({"modes": {"expiry": {"outcome": None}}}, "expiry", True)[3]["status"] == "running"


def test_shadow_only_shows_what_it_would_write_and_hand_back_and_writes_nothing() -> None:
    from custom_components.spotnav.battery.executor import preview  # noqa: PLC0415

    _executor, ocean = _ocean()
    seen = preview(ECOFLOW_CONTROL, inputs(ocean, T0, "grid_charge", 3.0, power_kw=ocean.power_kw()))
    assert seen.blocker is None
    assert [(op.role, op.value) for op in seen.writes] == [
        ("modbus_control", True), ("charge_power", 3000.0), ("battery_mode", "charge_battery")
    ]
    assert [(op.role, op.value) for op in seen.restores] == [("battery_mode", "automatic"), ("modbus_control", False)]
    assert ocean.writes == [] and ocean.states == STATES
