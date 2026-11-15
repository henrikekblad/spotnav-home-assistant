"""The battery write path's shell on a site, against a fake Sigenergy plant whose entity services behave as the
integration's (read back on the next poll, dropped in read-only mode, raising). Also: the path is unreachable in
production, nothing refers to it, and it refuses to act while `ON_REACHABLE` is off."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.battery import control as control_module, shadow
from custom_components.spotnav.battery.control import BatteryControl, ControlNotReachable, store_key
from custom_components.spotnav.runtime import site_controller_for
from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

PACKAGE = Path(__file__).parent.parent / "custom_components" / "spotnav"
SIGEN = json.loads((Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8"))["sigenergy"]
SWITCH = "switch.sigen_plant_remote_ems_controlled_by_home_assistant"
MODE = "select.sigen_plant_remote_ems_control_mode"
DISCHARGE = "number.sigen_plant_ess_max_discharging_limit"
CHARGE = "number.sigen_plant_ess_max_charging_limit"
WORK_MODE = "sensor.sigen_plant_ems_work_mode"
CONTROLS = {SWITCH: "off", MODE: "unavailable", DISCHARGE: "23.04", WORK_MODE: "max_self_consumption"}


def _plant(hass: HomeAssistant) -> str:
    """The owner's plant (`brands.json`) with its Remote EMS controls enabled and its charge limit never set."""
    owner = MockConfigEntry(domain="sigen", entry_id="ENTRY")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("sigen", "plant")}, name="Sigen Plant", manufacturer="Sigenergy"
    )
    registry = er.async_get(hass)
    rows = [*SIGEN["entities"], {
        "entity_id": CHARGE, "unique_id": "ENTRY_sigen_plant_plant_ess_max_charging_limit", "state": "unknown",
        "unit": "kW", "disabled": False,
    }]
    for row in rows:
        domain, object_id = row["entity_id"].split(".", 1)
        control = row["entity_id"] in CONTROLS or row["entity_id"] == CHARGE
        if row["disabled"] and not control:
            continue
        entry = registry.async_get_or_create(
            domain, "sigen", row["unique_id"], device_id=device.id, config_entry=owner, suggested_object_id=object_id
        )
        assert entry.entity_id == row["entity_id"]
        state = CONTROLS.get(row["entity_id"], row["state"])
        attributes: dict[str, Any] = {"unit_of_measurement": row["unit"]} if row.get("unit") else {}
        if domain == "number":
            attributes["step"] = 0.001
        hass.states.async_set(entry.entity_id, state, attributes)
    return SIGEN["battery"]


class Plant:
    """The integration's entity services: a write reads back at once, unless the plant is read-only."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.read_only = False
        self.raises = False
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.stored_at_first_call: Any = None
        for domain, service in (("number", "set_value"), ("select", "select_option"), ("switch", "turn_on"),
                                ("switch", "turn_off")):
            hass.services.async_register(domain, service, self._handler(domain, service))

    def _set(self, entity_id: str, state: str) -> None:
        old = self.hass.states.get(entity_id)
        self.hass.states.async_set(entity_id, state, None if old is None else dict(old.attributes))

    def _handler(self, domain: str, service: str):
        async def handle(call: ServiceCall) -> None:
            if not self.calls:
                self.stored_at_first_call = self.storage.get(store_key(self.entry_id), {}).get("data")
            self.calls.append((domain, service, dict(call.data)))
            if self.raises:
                raise HomeAssistantError("Modbus write failed")
            if self.read_only:
                return  # the integration logs an error and returns
            entity_id = call.data["entity_id"]
            entity_id = entity_id[0] if isinstance(entity_id, list) else entity_id
            if domain == "number":
                self._set(entity_id, f"{float(call.data['value']):g}")
            elif domain == "select":
                self._set(entity_id, call.data["option"])
            else:
                on = service == "turn_on"
                self._set(entity_id, "on" if on else "off")
                self._set(MODE, "PCS Remote Control" if on else "unavailable")
                self._set(WORK_MODE, "remote_ems" if on else "max_self_consumption")

        return handle


@pytest.fixture
def reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow, "ON_REACHABLE", True)


async def _site(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]):
    battery = _plant(hass)
    _charger, site = await setup_charger_and_site(hass, battery_aggregate_power_entity=battery)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    plant = Plant(hass)
    plant.storage = hass_storage  # type: ignore[attr-defined]
    plant.entry_id = site.entry_id  # type: ignore[attr-defined]
    monkeypatch.setattr(BatteryControl, "_battery_power", lambda self: (-3.0, 5.0))
    original = BatteryControl.inputs

    def inputs(self, now, control, *, stopping=None):
        return replace(original(self, now, control, stopping=stopping), headroom_a=20.0, measurement_ok=True)

    monkeypatch.setattr(BatteryControl, "inputs", inputs)
    export_original = BatteryControl.export_inputs

    def export_inputs(self, now, control, *, stopping=None):
        return replace(export_original(self, now, control, stopping=stopping), measurement_ok=True)

    monkeypatch.setattr(BatteryControl, "export_inputs", export_inputs)
    return site, controller, plant


def _hold(_now):
    return ("hold", 0.0, None)


# --- unreachable ---------------------------------------------------------------------------------------------------


def test_the_write_path_is_unreachable_in_this_release() -> None:
    """The shadow parts share the executor's pure rules (`executor.preview`, `commands.py`), so they import those;
    the shell that writes (`control.py`) and a live executor stay unreachable from the integration."""
    assert shadow.ON_REACHABLE is False
    pattern = re.compile(r"battery\.(control|verify)\b|from \.(control|verify) import|\bBatteryControl\b|\bExecutor\(")
    allowed = {f"battery/{name}" for name in ("control.py", "executor.py", "verify.py")}
    users = [
        str(path.relative_to(PACKAGE)) for path in PACKAGE.rglob("*.py")
        if str(path.relative_to(PACKAGE)) not in allowed and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert users == [], "nothing in the integration may construct or import the write path"
    # Only the shell calls a service; the shared pure modules call none.
    callers = [
        str(path.relative_to(PACKAGE)) for path in (PACKAGE / "battery").glob("*.py")
        if "async_call" in path.read_text(encoding="utf-8")
    ]
    assert callers == ["battery/control.py"]
    services = (PACKAGE / "services.yaml").read_text(encoding="utf-8") if (PACKAGE / "services.yaml").exists() else ""
    assert "battery_control" not in services and "verify" not in services


async def test_the_shell_refuses_to_look_or_write_while_unreachable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    with pytest.raises(ControlNotReachable):
        await control.async_tick()
    with pytest.raises(ControlNotReachable):
        await control.async_verify_step()
    await control.async_stop()
    assert plant.calls == []
    assert hass.states.get(DISCHARGE).state == "23.04"


# --- the writes ----------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("reachable")
async def test_a_hold_is_written_in_order_read_back_with_the_snapshot_stored_first_and_restored_after_a_restart(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    now = dt_util.now()
    for index in range(6):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert [(domain, service) for domain, service, _data in plant.calls] == [
        ("number", "set_value"), ("switch", "turn_on"), ("select", "select_option")
    ]
    assert plant.calls[0][2] == {"entity_id": DISCHARGE, "value": 0.0}
    assert plant.calls[2][2]["option"] == "Maximum Self Consumption"
    # The snapshot was on disk before the first write.
    assert plant.stored_at_first_call["state"]["snapshot"] == {"charge_limit": "unknown", "discharge_limit": "23.04"}
    assert control.executor.state.phase == "active"
    assert control.snapshot()["executor"]["state"]["action"] == "hold"

    # HA stops without handing back; the next start restores before anything else.
    again = BatteryControl(hass, controller, plan_source=_hold)
    plant.calls.clear()
    await again.async_start()
    # The discharge limit first: the battery covers a peak again before Remote EMS goes off.
    assert [(domain, service) for domain, service, _data in plant.calls] == [
        ("number", "set_value"), ("switch", "turn_off")
    ]
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(DISCHARGE).state == "23.04"
    assert hass.states.get(CHARGE).state == "unknown", "a limit it never wrote is left as found"
    assert again.executor.state.phase == "idle"
    assert again.log[0]["reason"] == "restart"
    assert site.entry_id


@pytest.mark.usefixtures("reachable")
async def test_a_stop_hands_the_battery_back_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: ("grid_charge", 3.0, None))
    await control.async_start()
    now = dt_util.now()
    for index in range(6):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert hass.states.get(MODE).state == "Command Charging (Grid First)"
    assert hass.states.get(CHARGE).state == "3"
    await control.async_stop()
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(CHARGE).state == "20.6", "a never-set limit comes back as the rating"
    assert control.executor.state.phase == "idle"


@pytest.mark.usefixtures("reachable")
async def test_a_read_only_plant_drops_the_write_and_the_control_blocks_without_writing_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plant.read_only = True
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    now = dt_util.now()
    for index in range(12):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert len(plant.calls) == 1
    assert control.executor.state.phase == "blocked"
    assert control.executor.state.blocked.startswith("write_not_taken:discharge_limit")


@pytest.mark.usefixtures("reachable")
async def test_a_write_that_raises_is_not_taken_and_handed_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plant.raises = True
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    now = dt_util.now()
    for index in range(4):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert control.executor.state.blocked.startswith("write_not_taken:discharge_limit:service_failed")
    assert any(row["kind"] == "service_failed" for row in control.log)
    assert len(plant.calls) == 1


@pytest.mark.usefixtures("reachable")
async def test_a_plant_in_ai_mode_is_left_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_set(WORK_MODE, "ai_mode")
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    await control.async_tick()
    assert plant.calls == []
    assert control.log[-1]["reason"].startswith("self_managed")


@pytest.mark.usefixtures("reachable")
async def test_the_verification_routine_starts_through_the_shell(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller)
    await control.async_start()
    now = dt_util.now()
    for index in range(4):
        await control.async_verify_step(now + timedelta(seconds=10 * index))
    assert control.verification is not None and control.verification.firmware == "V100R001C21SPC117"
    assert control.verification.current.mode == "hold"
    assert [service for _domain, service, _data in plant.calls][:2] == ["set_value", "turn_on"]
    await control.async_stop()
    assert hass.states.get(SWITCH).state == "off"
    assert control_module.HAND_BACK_DEADLINE_S > 0


# --- the export limit --------------------------------------------------------------------------------------------------

EXPORT = "number.sigen_plant_grid_export_limitation"


def _export_limit(hass: HomeAssistant, state: str = "11.5") -> None:
    """The plant's Grid Export Limitation as on the owner's registry, enabled."""
    owner = hass.config_entries.async_get_entry("ENTRY")
    device = dr.async_get(hass).async_get_or_create(config_entry_id="ENTRY", identifiers={("sigen", "plant")})
    entry = er.async_get(hass).async_get_or_create(
        "number", "sigen", "ENTRY_sigen_plant_plant_grid_point_maximum_export_limitation", device_id=device.id,
        config_entry=owner, suggested_object_id="sigen_plant_grid_export_limitation",
    )
    assert entry.entity_id == EXPORT
    hass.states.async_set(EXPORT, state, {"unit_of_measurement": "kW", "step": 0.001})


async def test_the_export_limit_is_zero_below_a_zero_sell_price_and_back_after_without_touching_the_battery(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    monkeypatch.setattr(shadow, "ON_REACHABLE", True)
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    sell = {"now": -12.0}
    control = BatteryControl(hass, controller, plan_source=lambda _now: None, sell_source=lambda _now: sell["now"])
    await control.async_start()
    now = dt_util.now()
    for index in range(4):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert plant.calls == [("number", "set_value", {"entity_id": EXPORT, "value": 0.0})]
    assert plant.stored_at_first_call["export"]["state"]["snapshot"] == {"export_limit": "11.5"}
    assert control.export_executor.state.phase == "active" and control.acting()
    assert control.snapshot()["export"]["state"]["action"] == "curtail_export"

    # HA stops without handing back; the next start restores the limit before anything else.
    again = BatteryControl(hass, controller, plan_source=lambda _now: None, sell_source=lambda _now: sell["now"])
    plant.calls.clear()
    await again.async_start()
    assert plant.calls == [("number", "set_value", {"entity_id": EXPORT, "value": 11.5})]
    assert hass.states.get(EXPORT).state == "11.5"

    # The price turns positive while curtailing: handed back to the value found.
    for index in range(4):
        await again.async_tick(now + timedelta(minutes=5, seconds=10 * index))
    plant.calls.clear()
    sell["now"] = 3.0
    for index in range(4):
        await again.async_tick(now + timedelta(minutes=10, seconds=10 * index))
    assert plant.calls == [("number", "set_value", {"entity_id": EXPORT, "value": 11.5})]
    assert again.export_executor.state.phase == "idle" and not again.acting()
    assert hass.states.get(DISCHARGE).state == "23.04" and hass.states.get(SWITCH).state == "off"


async def test_the_export_limit_is_never_written_while_unreachable(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    _site_entry, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None, sell_source=lambda _now: -12.0)
    await control.async_start()
    with pytest.raises(ControlNotReachable):
        await control.async_export_tick()
    await control.async_stop()
    assert plant.calls == [] and hass.states.get(EXPORT).state == "11.5"
