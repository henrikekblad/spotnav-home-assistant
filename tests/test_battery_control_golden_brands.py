"""The battery control's shell, pinned before `battery/control.py` is split, for more than Sigenergy's recipe: Solis
(a force sent by the integration's own services, renewed by SpotNav, a dead-man the hardware test times) and EcoFlow
(a dead-man the integration renews and the hardware test silences), each as a plant in Home Assistant's registry
behind the fake inverter of its own test file (`test_battery_solis_control.Hybrid`, `test_battery_ecoflow.PowerOcean`),
and Sigenergy on the plant of `test_battery_control`.

Per brand, the order of the store's saves and the plant's service calls, look by look
(`fixtures/battery/control_trace_brands.json`), as `test_battery_control_golden` records them: an enter and a stop,
the options changed, failing calls, a start on a stored snapshot, a removed site's stored snapshot; a person's
hardware test from its start to its verdict, one stopped, and one with no room for its charge (`incomplete`); the
routine's check after an update with the old key's answer carried; the plan's own commands shown as the check on a
new key; and, for Sigenergy, the export limit's own instance beside the battery's.

Each scenario ends with a row of what the gate then says (`end`). The clock is frozen and moved look by look, so
nothing depends on the day or the hour the test runs at.

Regenerate only on purpose, in one process (the scenarios share the file):
`SPOTNAV_RECORD_GOLDEN=1 pytest -n 0 tests/test_battery_control_golden_brands.py`."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import freezegun
import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.battery import commands, export_shell as export_shell_module, gate
from custom_components.spotnav.battery.control import BatteryControl, store_key
from custom_components.spotnav.battery.executor import Op
from custom_components.spotnav.runtime import domain_data, site_controller_for

from tests import battery_patch, test_battery_control as sigen
from tests.test_battery_control_golden import _Recorder
from tests.test_battery_ecoflow import STATES as ECOFLOW_STATES, PowerOcean
from tests.test_battery_solis_control import Hybrid
from tests.world import setup_charger_and_site


FIXTURE = Path(__file__).parent / "fixtures" / "battery" / "control_trace_brands.json"
RECORD = bool(os.environ.get("SPOTNAV_RECORD_GOLDEN"))
BRANDS = json.loads((Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8"))
#: The first look of every scenario (the clock is frozen at it and moved with each look).
START = datetime.fromisoformat("2026-11-20T10:00:00+00:00")
TICK = timedelta(seconds=10)
ALL = ("sigenergy", "solis", "ecoflow")

pytestmark = pytest.mark.usefixtures("offline_relay")


class _RegistryPlant:
    """A brand's plant in the registry (`brands.json`, and the controls its recipe writes), its entity services
    answered by the fake inverter `self.fake`, whose readings are shown as the entities' states after each call."""

    brand = ""
    #: The controls the fixture leaves out: (domain, unique id, object id).
    controls: tuple[tuple[str, str, str], ...] = ()
    #: The plant's device's software version, where the adapter reads the firmware from it.
    sw_version: str | None = None

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.raises = False
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.now = START
        fixture = BRANDS[self.brand]
        platform = fixture["platform"]
        owner = MockConfigEntry(domain=platform, entry_id="ENTRY")
        owner.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=owner.entry_id, identifiers={(platform, "plant")}, name="Plant",
            manufacturer=fixture["manufacturer"], sw_version=self.sw_version,
        )
        registry = er.async_get(hass)
        for row in fixture["entities"]:
            domain, object_id = row["entity_id"].split(".", 1)
            entry = registry.async_get_or_create(
                domain, platform, row["unique_id"], device_id=device.id, config_entry=owner,
                suggested_object_id=object_id,
            )
            assert entry.entity_id == row["entity_id"]
            attributes = {key: row[name] for key, name in (("unit_of_measurement", "unit"), ("device_class", "device_class"))
                          if row.get(name)}
            hass.states.async_set(entry.entity_id, row["state"], attributes)
        self.entity: dict[str, str] = {}
        for domain, unique_id, object_id in self.controls:
            entry = registry.async_get_or_create(
                domain, platform, unique_id, device_id=device.id, config_entry=owner, suggested_object_id=object_id,
            )
            self.entity[f"{domain}.{object_id}"] = entry.entity_id
        self.battery = fixture["battery"]
        self.show()

    def serve(self) -> None:
        """Answer the entity services (once the site is set up: Home Assistant's own are registered with it)."""
        hass = self.hass
        for domain, service in (("number", "set_value"), ("select", "select_option"), ("switch", "turn_on"),
                                ("switch", "turn_off")):
            hass.services.async_register(domain, service, self._handler(domain, service))
        for name in self.services():
            domain, service = name.split(".", 1)
            hass.services.async_register(domain, service, self._handler(domain, service))

    def services(self) -> tuple[str, ...]:
        return ()

    def _handler(self, domain: str, service: str) -> Any:
        async def handle(call: ServiceCall) -> None:
            self.calls.append((domain, service, dict(call.data)))
            if self.raises:
                raise HomeAssistantError("Modbus write failed")
            self.take(domain, service, dict(call.data))
            self.show()

        return handle

    def _set(self, entity_id: str, state: str) -> None:
        old = self.hass.states.get(entity_id)
        self.hass.states.async_set(entity_id, state, None if old is None else dict(old.attributes))

    def at(self, now: datetime) -> None:
        self.now = now
        self.fake.at(now)  # type: ignore[attr-defined]
        self.show()

    def take(self, domain: str, service: str, data: dict[str, Any]) -> None:
        raise NotImplementedError

    def show(self) -> None:
        raise NotImplementedError

    def power_kw(self) -> float:
        return float(self.fake.power_kw())  # type: ignore[attr-defined]


_SOLIS = "solis_modbus_2403123456_"
_SOLIS_NUMBERS = {"charge_power": 43136, "discharge_power": 43129, "rc_timeout": 43282}


class SolisPlant(_RegistryPlant):
    """`solis_modbus`: the force is the integration's services, read from its two switches and the power sensors."""

    brand = "solis"
    controls = (
        *((domain, f"{_SOLIS}solis_modbus_inverter_rc_{name}", f"solis_rc_{name}")
          for domain in ("number", "sensor") for name in ("force_charge_power", "force_discharge_power", "timeout")),
        ("switch", f"{_SOLIS}43110_5", "solis_allow_grid_charge"),
    )

    def __init__(self, hass: HomeAssistant) -> None:
        self.fake = Hybrid(commands.SOLIS_CONTROL, {"grid_charge_allowed": "on"}, idle_kw=-sigen.HOUSE_KW)
        self.fake.house_kw = sigen.HOUSE_KW
        super().__init__(hass)

    def services(self) -> tuple[str, ...]:
        return tuple(commands.SOLIS_CONTROL.role("rc_force").services.values())

    def take(self, domain: str, service: str, data: dict[str, Any]) -> None:
        fake = self.fake
        if domain == "number":
            name = {"force_charge_power": "charge_power", "force_discharge_power": "discharge_power",
                    "timeout": "rc_timeout"}[data["entity_id"].split("solis_rc_", 1)[1]]
            fake.apply(Op(name, "number", data["entity_id"], data["value"]))
        elif service == "solis_stop_force_charge_discharge":
            fake._write(43135, 0)
        elif domain == "solis_modbus":
            kind = "charge" if service.endswith("force_battery_charge") else "discharge"
            fake._write(43135, 1 if kind == "charge" else 2)
            fake._write(_SOLIS_NUMBERS[f"{kind}_power"], round(data["power_watts"] / 10))
            fake._write(43282, int(data["duration_minutes"]))
        else:
            raise AssertionError(f"never written: {domain}.{service} {data}")

    def show(self) -> None:
        fake = self.fake
        force = fake.reg[43135]
        self._set("switch.solis_s6_eh3p_rc_force_battery_charge", "on" if force == 1 else "off")
        self._set("switch.solis_s6_eh3p_rc_force_battery_discharge", "on" if force == 2 else "off")
        self._set(self.entity["switch.solis_allow_grid_charge"], "on")
        for name, key in (("force_charge_power", "charge_power"), ("force_discharge_power", "discharge_power"),
                          ("timeout", "rc_timeout")):
            register = fake.reg[_SOLIS_NUMBERS[key]]
            self._set(self.entity[f"sensor.solis_rc_{name}"], str(register * 10 if key != "rc_timeout" else register))
            self._set(self.entity[f"number.solis_rc_{name}"], fake.shown[key])


_ECOFLOW = "01JEFPOWEROCEANENTRY0000_"
_ECOFLOW_ROLES = {
    "switch.ecoflow_modbus_control": "modbus_control", "number.ecoflow_charge_battery_power": "charge_power",
    "number.ecoflow_discharge_battery_power": "discharge_power", "select.ecoflow_battery_mode": "battery_mode",
}


class EcoflowPlant(_RegistryPlant):
    """`ef_powerocean_tcpmodbus`: a switch whose heartbeat the integration sends, two powers and a mode."""

    brand = "ecoflow"
    sw_version = "V5.1.37.13"
    controls = (
        ("switch", f"{_ECOFLOW}modbus_control", "ecoflow_modbus_control"),
        ("number", f"{_ECOFLOW}charge_battery_power", "ecoflow_charge_battery_power"),
        ("number", f"{_ECOFLOW}discharge_battery_power", "ecoflow_discharge_battery_power"),
        ("select", f"{_ECOFLOW}battery_mode", "ecoflow_battery_mode"),
    )

    def __init__(self, hass: HomeAssistant) -> None:
        self.fake = PowerOcean(commands.ECOFLOW_CONTROL, dict(ECOFLOW_STATES), idle_kw=-sigen.HOUSE_KW)
        super().__init__(hass)

    def take(self, domain: str, service: str, data: dict[str, Any]) -> None:
        entity_id = data["entity_id"]
        role = _ECOFLOW_ROLES[next(key for key, value in self.entity.items() if value == entity_id)]
        value: Any = service == "turn_on" if domain == "switch" else data.get("value", data.get("option"))
        self.fake.apply(Op(role, domain, entity_id, value))

    def show(self) -> None:
        for key, role in _ECOFLOW_ROLES.items():
            self._set(self.entity[key], self.fake.states[role])
        self._set("binary_sensor.ecoflow_powerocean_modbus_control", self.fake.states["modbus_control"])


class SigenPlant(sigen.Plant):
    """The owner's plant of `test_battery_control`."""

    brand = "sigenergy"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.battery = sigen._plant(hass)

    def serve(self) -> None:
        super().__init__(self.hass)
        self.obeys = True

    def at(self, now: datetime) -> None:
        return None


PLANTS = {"sigenergy": SigenPlant, "solis": SolisPlant, "ecoflow": EcoflowPlant}


class World:
    """A site with one brand's plant, the frozen clock and the recorder of its saves and calls."""

    def __init__(self, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: Any, brand: str) -> None:
        self.hass, self.monkeypatch, self.freezer, self.brand = hass, monkeypatch, freezer, brand
        self.house_kw = sigen.HOUSE_KW
        #: The least headroom on a phase (A) from the site's import now (kW).
        self.headroom_of: Any = lambda _grid_kw: 20.0
        self.plan: Any = ("hold", 0.0, None)
        self.sell = 50.0
        self.now = START

    async def build(self, hass_storage: dict[str, Any]) -> World:
        hass, monkeypatch = self.hass, self.monkeypatch
        self.freezer.move_to(START)
        # The event loop keeps the real clock: a hand-back's deadline and a call's timeout still run out.
        monkeypatch.setattr(hass.loop, "time", freezegun.api.real_monotonic)
        plant = self.plant = PLANTS[self.brand](hass)
        _charger, site = await setup_charger_and_site(hass, battery_aggregate_power_entity=plant.battery)
        self.site = site
        controller = self.controller = site_controller_for(hass, site.entry_id)
        assert controller is not None
        plant.serve()
        plant.storage, plant.entry_id = hass_storage, site.entry_id
        world = self
        monkeypatch.setattr(sigen, "HOUSE_KW", self.house_kw)
        monkeypatch.setattr(BatteryControl, "_battery_power", lambda self: (plant.power_kw(), 5.0))
        monkeypatch.setattr(BatteryControl, "_grid_import_kw", lambda self: world.house_kw + plant.power_kw())
        original = BatteryControl.inputs

        def inputs(self: BatteryControl, now: datetime, control: Any, **kwargs: Any) -> Any:
            grid = max(world.house_kw + plant.power_kw(), 0.0)
            return replace(original(self, now, control, **kwargs), headroom_a=world.headroom_of(grid),
                           measurement_ok=True)

        monkeypatch.setattr(BatteryControl, "inputs", inputs)
        monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, charger: 0.0)
        monkeypatch.setattr(BatteryControl, "_phase_export_a", lambda self, volts: [0.0, 0.0, 0.0])
        export_original = BatteryControl.export_inputs

        def export_inputs(self: BatteryControl, now: datetime, control: Any, **kwargs: Any) -> Any:
            return replace(export_original(self, now, control, **kwargs), measurement_ok=True)

        monkeypatch.setattr(BatteryControl, "export_inputs", export_inputs)
        monkeypatch.setattr(export_shell_module, "learning_gate_open", lambda _hass, _site: True)
        monkeypatch.setattr(BatteryControl, "_integration_version", lambda self, domain: world.version if domain else None)
        await controller.battery_control.async_shutdown()
        self.recorder = _Recorder(monkeypatch, store_key(site.entry_id))
        self.recorder.watch(plant)
        return self

    version = "1.0.0"

    def control(self) -> BatteryControl:
        return BatteryControl(
            self.hass, self.controller, plan_source=lambda _now: self.plan, sell_source=lambda _now: self.sell
        )

    def options(self, **flags: bool) -> None:
        sigen._options(self.hass, self.site, **flags)

    async def sign_up(self, control: BatteryControl) -> None:
        """The person signed up as a tester of the brand: its first hardware test may run."""
        control.tester = {"brand": self.brand, "asked_at": "2026-11-19T10:00:00+01:00", "ref": "SN-TEST"}
        await domain_data(self.hass).beta.async_set(on=True)

    def passed(self, control: BatteryControl, **outcomes: str) -> str:
        """A passed report of the routine on the key the control reads now."""
        control.facts = control._read_facts()
        key = control.firmware_key()
        modes = {"hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"} | outcomes
        control.reports[key] = gate.merge_report(
            None, {"brand": self.brand, "started_at": dt_util.now().isoformat(),
                   "modes": {mode: {"mode": mode, "outcome": outcome} for mode, outcome in modes.items()}},
            key=key, attempts=1, now=dt_util.now(),
        )
        return key

    async def running(self) -> BatteryControl:
        """A started control that may write: opted in, the check passed and, for a recipe that waits for one, the
        plant's hardware test passed and the control code entered."""
        control = self.control()
        await control.async_start()
        self.options(enabled=True, shadow_only=False)
        control._enabled_seen = True
        self.passed(control)
        if self.brand != "sigenergy":
            await self.sign_up(control)
            control.hardware_brands = [self.brand]
            control.control_codes = {self.brand: {**sigen._CODE, "brand": self.brand}}
        assert control.state() == "on", (control.state(), control._needs())
        return control

    async def looks(self, control: BatteryControl, count: int) -> None:
        for _ in range(count):
            await self.look(control)

    async def look(self, control: BatteryControl) -> None:
        self.recorder.mark("look")
        self.freezer.move_to(self.now)
        self.plant.at(self.now)
        await control.async_look(self.now)
        self.now += TICK

    def mark(self, what: str) -> None:
        self.recorder.mark(what)

    def end(self, control: BatteryControl) -> None:
        """What the gate says at the scenario's end, as a last row."""
        hardware = control.hardware_report
        executor, export = control.executor, control.export_executor
        self.recorder.log.append(["end", json.loads(json.dumps({
            "state": control.state(),
            "needs": control._needs(),
            "allowed": sorted(control.allowed()),
            "due": control.hardware_test_due(),
            "refusal": control.hardware_test_refusal(),
            "hardware_brands": list(control.hardware_brands),
            "hardware_failed": len(control.hardware_failed),
            "hardware": None if hardware is None else {
                "completed": hardware.get("completed"), "modes": _outcomes(hardware),
                "missing": control.hardware_test_missing(hardware),
            },
            "report": _outcomes(control.report()),
            "via": {mode: row.get("via") for mode, row in ((control.report() or {}).get("modes") or {}).items()
                    if row.get("via")},
            "carried": sorted((control.carried_from() or {})),
            "reports": len(control.reports),
            "phase": None if executor is None else executor.state.phase,
            "export_phase": None if export is None else export.state.phase,
            "log": _kinds(control),
        }, default=str))])

    def check(self, name: str) -> None:
        name = f"{self.brand}/{name}"
        stored = json.loads(FIXTURE.read_text()) if FIXTURE.exists() else {}
        if RECORD:
            stored[name] = self.recorder.log
            FIXTURE.write_text(json.dumps(stored, indent=1, sort_keys=True) + "\n")
            return
        assert json.dumps(self.recorder.log, sort_keys=True) == json.dumps(stored[name], sort_keys=True), name


def _outcomes(report: Any) -> dict[str, Any] | None:
    return None if report is None else {mode: row.get("outcome") for mode, row in (report.get("modes") or {}).items()}


def _kinds(control: BatteryControl) -> list[str]:
    """The log's kinds in order, a run of one kind once."""
    kinds: list[str] = []
    for row in control.log:
        kind = f"{row.get('kind')}:{row.get('reason')}" if row.get("reason") is not None else str(row.get("kind"))
        if not kinds or kinds[-1] != kind:
            kinds.append(kind)
    return kinds


@pytest.fixture
async def world(
    request: pytest.FixtureRequest, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, freezer: Any,
    hass_storage: dict[str, Any],
) -> World:
    return await World(hass, monkeypatch, freezer, request.param).build(hass_storage)


def _for(*brands: str) -> Any:
    return pytest.mark.parametrize("world", brands, indirect=True)


# ---- the scenarios of `test_battery_control_golden`, per brand ----------------------------------------------------


@_for("solis", "ecoflow")
async def test_an_enter_stores_the_snapshot_before_its_first_write_and_a_stop_hands_back(world: World) -> None:
    control = await world.running()
    # Past a fast watchdog's renewal (Solis's, every two minutes).
    await world.looks(control, 16)
    world.mark("stop")
    await control.async_stop()
    log = world.recorder.log
    first_call = next(index for index, row in enumerate(log) if row[0] == "call")
    assert any(row[0] == "save" and "state.snapshot" in row[1] for row in log[:first_call])
    world.end(control)
    world.check("enter_and_stop")


@_for("solis", "ecoflow")
async def test_a_write_the_options_no_longer_allow_becomes_a_hand_back(world: World) -> None:
    control = await world.running()
    await world.looks(control, 6)
    world.mark("shadow only")
    world.options(shadow_only=True)
    await world.looks(control, 6)
    world.mark("stop")
    await control.async_stop()
    world.end(control)
    world.check("options_changed")


@_for("solis", "ecoflow")
async def test_calls_that_fail_are_retried_and_then_given_up_on(world: World) -> None:
    control = await world.running()
    world.plant.raises = True
    await world.looks(control, 6)
    world.mark("the plant takes writes again")
    world.plant.raises = False
    await world.looks(control, 6)
    world.mark("stop")
    battery_patch.patch(world.monkeypatch, "HAND_BACK_DEADLINE_S", 0.05)
    await control.async_stop()
    world.end(control)
    world.check("failing_calls")


async def _stored_hold(world: World) -> None:
    """The site's hold cut short at a stop: its snapshot is in the store, the plant still holds."""
    control = await world.running()
    await world.looks(control, 6)
    world.plant.raises = True
    battery_patch.patch(world.monkeypatch, "HAND_BACK_DEADLINE_S", 0.05)
    await control.async_shutdown()
    world.plant.raises = False
    stored = world.plant.storage[store_key(world.site.entry_id)]["data"]["state"]
    assert stored["phase"] == "restoring" and (stored["snapshot"] or stored["entities"])


@_for("solis", "ecoflow")
async def test_a_start_on_a_stored_snapshot_hands_it_back_first(world: World) -> None:
    await _stored_hold(world)
    control = world.control()
    world.mark("start")
    await control.async_start()
    await world.looks(control, 6)
    world.mark("shutdown")
    await control.async_shutdown()
    world.end(control)
    world.check("stored_start")


@_for("solis", "ecoflow")
async def test_a_removed_sites_stored_snapshot_is_handed_back_and_the_store_removed(world: World) -> None:
    await _stored_hold(world)
    world.mark("removed")
    battery_patch.patch(world.monkeypatch, "HAND_BACK_DEADLINE_S", 30.0)
    battery_patch.patch(world.monkeypatch, "HAND_BACK_LOOK_S", 0.01)
    await BatteryControl.async_remove_stored(world.hass, world.site.entry_id, dict(world.site.data))
    world.check("removed_site")


# ---- a person's hardware test -------------------------------------------------------------------------------------


async def _test_ready(world: World) -> BatteryControl:
    """Opted in and signed up as a tester, nothing passed yet: the hardware test may start."""
    control = world.control()
    await control.async_start()
    world.options(enabled=True, shadow_only=world.brand == "sigenergy")
    await world.sign_up(control)
    control.facts = control._read_facts()
    return control


async def _until_ended(world: World, control: BatteryControl, most: int = 900) -> None:
    for _ in range(most):
        if not control.hardware_test_progress()["running"]:
            return
        await world.look(control)
    raise AssertionError(f"the hardware test never ended: {control.hardware_test_progress()}")


@_for(*ALL)
async def test_a_hardware_test_from_its_start_to_its_verdict(world: World) -> None:
    control = await _test_ready(world)
    world.mark("hardware test")
    assert await control.async_hardware_test_start() is None
    world.now += TICK
    await _until_ended(world, control)
    world.mark("ended")
    world.end(control)
    # The developer's code is what control then waits for; with it the plan runs.
    control.control_codes = {world.brand: {**sigen._CODE, "brand": world.brand}}
    world.options(shadow_only=False)
    await world.looks(control, 6)
    world.mark("stop")
    await control.async_stop()
    world.end(control)
    world.check("hardware_test")


@_for(*ALL)
async def test_a_hardware_test_stopped_hands_back_and_leaves_its_report_unfinished(world: World) -> None:
    control = await _test_ready(world)
    world.mark("hardware test")
    assert await control.async_hardware_test_start() is None
    world.now += TICK
    await world.looks(control, 3)
    world.mark("stopped")
    await control.async_hardware_test_stop("hardware_test_deadline")
    assert control.hardware_report is not None and control.hardware_report["completed"] is False
    await world.looks(control, 3)
    world.end(control)
    world.check("hardware_test_stopped")


@_for(*ALL)
async def test_a_hardware_test_with_no_room_for_its_charge_is_incomplete(world: World) -> None:
    # The tester's site: the battery covers a 4.7 kW house and the worst phase has 24 A left under a 25 A fuse.
    world.house_kw = 4.7
    world.monkeypatch.setattr(sigen, "HOUSE_KW", 4.7)
    if world.brand != "sigenergy":
        world.plant.fake.idle_kw = -4.7
        world.plant.fake.house_kw = 4.7
    world.headroom_of = lambda grid_kw: 24.0 - grid_kw * 1000.0 / 230.94
    control = await _test_ready(world)
    world.mark("hardware test")
    assert await control.async_hardware_test_start() is None
    world.now += TICK
    await _until_ended(world, control)
    world.end(control)
    world.check("hardware_test_incomplete")


# ---- the routine's check after an update, and the plan's own commands as that check --------------------------------


@_for(*ALL)
async def test_after_an_update_control_runs_on_the_old_keys_answer_until_the_routine_checked_the_new_key(
    world: World,
) -> None:
    control = await world.running()
    await world.looks(control, 6)
    world.mark("the integration was updated")
    world.version = "9.9.9"
    await world.looks(control, 3)
    world.end(control)
    # The plan leaves the battery to itself: the routine's moment.
    world.mark("the plan leaves the battery")
    world.plan = None
    for _ in range(400):
        await world.look(control)
        if any(row.get("kind") == "verify_done" for row in control.log):
            break
    else:
        raise AssertionError("the routine never finished its check")
    world.end(control)
    world.mark("stop")
    await control.async_stop()
    world.check("reverify_carry")


@_for(*ALL)
async def test_the_plans_own_commands_on_a_new_key_are_credited_as_its_check(world: World) -> None:
    world.monkeypatch.setattr(gate, "suitable", lambda moment: "car_charging")
    control = await world.running()
    await world.looks(control, 6)
    world.mark("the integration was updated")
    world.version = "9.9.9"
    world.plan = None
    await world.looks(control, 12)
    for slot, count in ((("hold", 0.0, None), 70), (None, 12), (("grid_charge", 2.0, None), 70), (None, 12),
                        (("discharge_house", 3.0, None), 70), (None, 12)):
        world.mark(f"the plan: {slot and slot[0]}")
        world.plan = slot
        await world.looks(control, count)
    world.end(control)
    world.mark("shutdown")
    await control.async_shutdown()
    again = world.control()
    world.mark("start")
    await again.async_start()
    world.end(again)
    await again.async_shutdown()
    world.check("plan_proof")


# ---- the export limit's own instance ------------------------------------------------------------------------------


@_for("sigenergy")
async def test_the_export_limit_runs_as_its_own_instance_beside_the_battery(world: World) -> None:
    sigen._export_limit(world.hass)
    control = await world.running()
    await world.looks(control, 6)
    world.mark("the sell price falls below zero")
    world.sell = -12.0
    await world.looks(control, 8)
    world.mark("the plan leaves the battery")
    world.plan = None
    await world.looks(control, 8)
    world.mark("the sell price is back")
    world.sell = 50.0
    await world.looks(control, 8)
    world.mark("below zero again, then a stop")
    world.sell = -12.0
    await world.looks(control, 6)
    await control.async_shutdown()
    world.end(control)
    again = world.control()
    world.mark("start")
    await again.async_start()
    await world.looks(again, 6)
    world.end(again)
    await again.async_shutdown()
    world.check("export_instance")
