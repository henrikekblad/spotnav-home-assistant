"""The battery write path's shell on a site, against a fake Sigenergy plant whose entity services behave as the
integration's (read back on the next poll, dropped in read-only mode, raising), behind the site's gate
(`battery/gate.py`): shadow only by default, the opt-in, a passed verification for the plant's firmware."""

from __future__ import annotations

import asyncio
import json
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

from custom_components.spotnav.battery import control as control_module, gate
from custom_components.spotnav.battery.control import BatteryControl, ControlNotReachable, TrialRefused, store_key
from custom_components.spotnav.runtime import site_controller_for
from custom_components.spotnav.site.site_capacity_controller import SiteCapacityController
from tests.world import setup_charger_and_site

pytestmark = pytest.mark.usefixtures("offline_relay")

PACKAGE = Path(__file__).parent.parent / "custom_components" / "spotnav"
SIGEN = json.loads((Path(__file__).parent / "fixtures" / "battery" / "brands.json").read_text(encoding="utf-8"))["sigenergy"]
SWITCH = "switch.sigen_plant_remote_ems_controlled_by_home_assistant"
MODE = "select.sigen_plant_remote_ems_control_mode"
DISCHARGE = "number.sigen_plant_ess_max_discharging_limit"
CHARGE = "number.sigen_plant_ess_max_charging_limit"
WORK_MODE = "sensor.sigen_plant_ems_work_mode"
FIRMWARE = "sensor.sigen_inverter_firmware_version"
CONTROLS = {SWITCH: "off", MODE: "unavailable", DISCHARGE: "23.04", WORK_MODE: "max_self_consumption"}
#: The site's own reading of a charger's last current (`_site` replaces it with no car).
_CHARGER_ALLOWED_A = SiteCapacityController.charger_allowed_a
#: The house's own load in the fake (kW): the battery covers it when it may.
HOUSE_KW = 2.0
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
            if isinstance(self.raises, Exception):
                raise self.raises
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


    def power_kw(self) -> float:
        """The battery's power as the plant runs it now (kW, + charging): it covers the house in Maximum Self
        Consumption, stops under a hold (discharge limit 0), charges at its limit in Command Charging."""
        if not self.obeys:
            return -HOUSE_KW
        state = self.hass.states.get
        if state(SWITCH).state == "on" and state(MODE).state == "Command Charging (Grid First)":
            return float(state(CHARGE).state)
        if state(DISCHARGE).state == "0":
            return 0.0
        return -HOUSE_KW


async def _site(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], *, own: bool = False,
    **site_kwargs: Any,
):
    """A site with the owner's plant. Its own battery control is shut down unless `own` (a test makes its own)."""
    battery = _plant(hass)
    _charger, site = await setup_charger_and_site(hass, battery_aggregate_power_entity=battery, **site_kwargs)
    controller = site_controller_for(hass, site.entry_id)
    assert controller is not None
    plant = Plant(hass)
    plant.obeys = True  # type: ignore[attr-defined]
    plant.storage = hass_storage  # type: ignore[attr-defined]
    plant.entry_id = site.entry_id  # type: ignore[attr-defined]
    monkeypatch.setattr(BatteryControl, "_battery_power", lambda self: (plant.power_kw(), 5.0))
    monkeypatch.setattr(BatteryControl, "_grid_import_kw", lambda self: HOUSE_KW + plant.power_kw())
    original = BatteryControl.inputs

    def inputs(self, now, control, **kwargs):
        return replace(original(self, now, control, **kwargs), headroom_a=20.0, measurement_ok=True)

    monkeypatch.setattr(BatteryControl, "inputs", inputs)
    # No car at the site's charger: its last current is 0 A (the HA-down grid charge cap counts it).
    monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, charger: 0.0)
    # No phase exports (the export side's fuse rule counts the sun's export).
    monkeypatch.setattr(BatteryControl, "_phase_export_a", lambda self, volts: [0.0, 0.0, 0.0])
    export_original = BatteryControl.export_inputs

    def export_inputs(self, now, control, **kwargs):
        return replace(export_original(self, now, control, **kwargs), measurement_ok=True)

    monkeypatch.setattr(BatteryControl, "export_inputs", export_inputs)
    if not own:
        await controller.battery_control.async_shutdown()
    return site, controller, plant


def _options(hass: HomeAssistant, site: MockConfigEntry, **flags: bool) -> None:
    hass.config_entries.async_update_entry(
        site, data={**site.data, **{f"battery_control_{key}": value for key, value in flags.items()}}
    )


def _passed(control: BatteryControl, **outcomes: str) -> None:
    """A passed verification report for the plant's firmware as the control reads it now."""
    control.facts = control._read_facts()
    key = control.firmware_key()
    modes = {"hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored"} | outcomes
    control.reports[key] = gate.merge_report(
        None, {"brand": "sigenergy", "started_at": dt_util.now().isoformat(),
               "modes": {mode: {"mode": mode, "outcome": outcome} for mode, outcome in modes.items()}},
        key=key, attempts=1, now=dt_util.now(),
    )


def _on(hass: HomeAssistant, site: MockConfigEntry, control: BatteryControl, **flags: bool) -> None:
    """Control on: opted in, shadow only off, the verification passed."""
    _options(hass, site, enabled=True, shadow_only=False, **flags)
    control._enabled_seen = True
    _passed(control)


async def _started(hass, site, controller, plan_source=None, **kwargs) -> BatteryControl:
    control = BatteryControl(hass, controller, plan_source=plan_source, **kwargs)
    await control.async_start()
    _on(hass, site, control)
    return control


def _hold(_now):
    return ("hold", 0.0, None)


# --- the gate ------------------------------------------------------------------------------------------------------


async def test_shadow_only_is_the_default_and_refuses_every_write_while_it_logs_what_it_would_do(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = BatteryControl(hass, controller, plan_source=_hold, sell_source=lambda _now: -12.0)
    await control.async_start()
    _options(hass, site, enabled=True)  # opted in, shadow only left on (the default)
    _passed(control)
    assert control.state() == "shadow_only"
    now = dt_util.now()
    for index in range(6):
        await control.async_look(now + timedelta(seconds=10 * index))
    assert plant.calls == []
    would = [row for row in control.log if row["kind"] == "would_do"]
    assert len(would) == 1 and would[0]["reason"] == "hold" and would[0]["blocker"] is None
    assert [write["role"] for write in would[0]["writes"]] == ["discharge_limit", "remote_ems", "control_mode"]
    with pytest.raises(ControlNotReachable):
        await control.async_tick()
    with pytest.raises(ControlNotReachable):
        await control.async_verify_step()
    with pytest.raises(ControlNotReachable):
        await control.async_export_tick()
    with pytest.raises(TrialRefused) as refused:
        await control.async_trial("hold", None, 5, force=False)
    assert refused.value.code == "shadow_only"
    await control.async_stop()
    assert plant.calls == []
    assert hass.states.get(DISCHARGE).state == "23.04" and hass.states.get(EXPORT).state == "11.5"


async def test_nothing_is_done_while_the_opt_in_is_off(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, shadow_only=False)
    _passed(control)
    for index in range(4):
        await control.async_look(dt_util.now() + timedelta(seconds=10 * index))
    assert control.state() == "off" and plant.calls == []
    assert not any(row["kind"] in ("enter", "write", "would_do", "verify_started") for row in control.log)


async def test_enabling_without_a_passed_verification_runs_no_plan_action(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: ("grid_charge", 3.0, None))
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    with pytest.raises(ControlNotReachable):
        await control.async_tick()
    # A car charging makes the moment unsuitable: the routine waits, and nothing of the plan is written.
    monkeypatch.setattr(type(controller.battery_shadow), "grid_charging", lambda self: (("entry_a",), 7.0))
    for index in range(4):
        await control.async_look(dt_util.now() + timedelta(seconds=10 * index))
    assert plant.calls == []
    assert control.state() == "waiting" and control.status()["line"] == "waiting:car_charging"


# --- the writes ----------------------------------------------------------------------------------------------------


async def test_a_hold_is_written_in_order_read_back_with_the_snapshot_stored_first_and_restored_after_a_restart(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
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


async def test_a_restart_waits_for_a_late_integration_before_it_restores(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = dt_util.now()
    for index in range(6):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert control.executor.state.phase == "active"
    # HA restarts; the plant's integration loads late: its switch is the registry's restored placeholder and its
    # services are not there yet.
    hass.states.async_set(SWITCH, "unavailable", {"restored": True})
    hass.services.async_remove("switch", "turn_off")

    def loaded() -> None:
        hass.services.async_register("switch", "turn_off", plant._handler("switch", "turn_off"))
        hass.states.async_set(SWITCH, "on", {})

    monkeypatch.setattr(control_module, "STARTUP_LOOK_S", 0.01)
    hass.loop.call_later(0.1, loaded)
    plant.calls.clear()
    again = BatteryControl(hass, controller, plan_source=_hold)
    await again.async_start()
    assert [(domain, service) for domain, service, _data in plant.calls] == [
        ("number", "set_value"), ("switch", "turn_off")
    ]
    assert not any(row["kind"] in ("not_taken", "service_failed") for row in again.log)
    assert again.executor.state.phase == "idle" and hass.states.get(SWITCH).state == "off"


async def test_a_restart_waits_for_the_controls_only_so_long(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = dt_util.now()
    for index in range(6):
        await control.async_tick(now + timedelta(seconds=10 * index))
    hass.states.async_set(SWITCH, "unavailable", {"restored": True})
    monkeypatch.setattr(control_module, "STARTUP_LOOK_S", 0.01)
    monkeypatch.setattr(control_module, "STARTUP_WAIT_S", 0.05)
    monkeypatch.setattr(control_module, "HAND_BACK_DEADLINE_S", 0.1)
    plant.calls.clear()
    again = BatteryControl(hass, controller, plan_source=_hold)
    await again.async_start()
    assert plant.calls, "after the wait the restore goes ahead with what is there"
    assert again.log[0]["reason"] == "restart"


async def test_a_stop_hands_the_battery_back_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 3.0, None))
    now = dt_util.now()
    for index in range(6):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert hass.states.get(MODE).state == "Command Charging (Grid First)"
    assert hass.states.get(CHARGE).state == "3"
    await control.async_stop()
    assert hass.states.get(SWITCH).state == "off"
    assert hass.states.get(CHARGE).state == "20.6", "a never-set limit comes back as the rating"
    assert control.executor.state.phase == "idle"


async def test_a_read_only_plant_drops_the_write_and_the_control_blocks_without_writing_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plant.read_only = True
    control = await _started(hass, site, controller, plan_source=_hold)
    now = dt_util.now()
    for index in range(12):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert len(plant.calls) == 1
    assert control.executor.state.phase == "blocked"
    assert control.executor.state.blocked.startswith("write_not_taken:discharge_limit")


async def test_a_write_that_raises_is_not_taken_and_handed_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plant.raises = True
    control = await _started(hass, site, controller, plan_source=_hold)
    now = dt_util.now()
    for index in range(4):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert control.executor.state.blocked.startswith("write_not_taken:discharge_limit:service_failed")
    assert any(row["kind"] == "service_failed" for row in control.log)
    assert len(plant.calls) == 1


class ModbusIOException(Exception):
    """Not a Home Assistant error: what a Modbus library may raise through an integration's service."""


async def test_a_write_that_raises_any_error_is_not_taken_and_the_state_is_still_stored_and_stop_still_runs(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = dt_util.now()
    for index in range(2):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert control.executor.state.phase == "active"
    plant.raises = ModbusIOException("no response from the plant")
    for index in range(2, 4):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert any(row["kind"] == "service_failed" for row in control.log)
    stored = hass_storage[store_key(site.entry_id)]["data"]["state"]
    assert stored["snapshot"] == {"charge_limit": "unknown", "discharge_limit": "23.04"}
    assert stored["phase"] == control.executor.state.phase
    monkeypatch.setattr(control_module, "HAND_BACK_DEADLINE_S", 0.1)
    await control.async_shutdown()  # never raises: the hand-back is stored for the next start
    assert hass_storage[store_key(site.entry_id)]["data"]["state"]["phase"] == "restoring"


async def test_a_plant_in_ai_mode_is_left_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_set(WORK_MODE, "ai_mode")
    control = await _started(hass, site, controller, plan_source=_hold)
    await control.async_tick()
    assert plant.calls == []
    assert control.log[-1]["reason"].startswith("self_managed")


async def test_the_verification_routine_starts_through_the_shell(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller)
    control.reports.clear()
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
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    sell = {"now": -12.0}
    control = await _started(hass, site, controller, plan_source=lambda _now: None, sell_source=lambda _now: sell["now"])
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


async def test_the_export_limit_is_taken_beside_spotnav_s_own_battery_control_and_kept_through_its_hand_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    plan: dict[str, Any] = {"now": ("hold", 0.0, None)}
    sell = {"now": 3.0}
    control = await _started(hass, site, controller, plan_source=lambda _now: plan["now"], sell_source=lambda _now: sell["now"])
    now = dt_util.now()
    for index in range(4):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert hass.states.get(SWITCH).state == "on" and hass.states.get(WORK_MODE).state == "remote_ems"
    assert control.export_inputs(now, control.export_executor.control).sibling_active
    # A negative price while SpotNav's own battery control holds Remote EMS: the export limit is still taken.
    sell["now"] = -12.0
    plant.calls.clear()
    for index in range(4):
        await control.async_tick(now + timedelta(minutes=1, seconds=10 * index))
    assert plant.calls == [("number", "set_value", {"entity_id": EXPORT, "value": 0.0})]
    assert control.export_executor.state.phase == "active"
    # The battery is handed back (Remote EMS off): the export limit stays at 0 kW, SpotNav's.
    plan["now"] = ("self_consume", 0.0, None)
    plant.calls.clear()
    for index in range(6):
        await control.async_tick(now + timedelta(minutes=2, seconds=10 * index))
    assert hass.states.get(SWITCH).state == "off" and control.executor.state.phase == "idle"
    assert all(data.get("entity_id") != EXPORT for _domain, _service, data in plant.calls)
    assert hass.states.get(EXPORT).state == "0" and control.export_executor.state.phase == "active"
    assert not control.export_inputs(now, control.export_executor.control).sibling_active


async def test_a_stop_and_a_restart_hand_the_battery_back_before_the_export_limit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = await _started(hass, site, controller, plan_source=_hold, sell_source=lambda _now: -12.0)
    now = dt_util.now()
    for index in range(6):
        await control.async_tick(now + timedelta(seconds=10 * index))
    assert hass.states.get(DISCHARGE).state == "0" and hass.states.get(EXPORT).state == "0"

    def order() -> tuple[int, int]:
        ids = [data.get("entity_id") for _domain, _service, data in plant.calls]
        battery = max(index for index, entity_id in enumerate(ids) if entity_id in (SWITCH, MODE, DISCHARGE))
        return battery, ids.index(EXPORT)

    plant.calls.clear()
    await control.async_stop()
    battery_last, export_first = order()
    assert battery_last < export_first, plant.calls
    # Again, now as a restart finds both snapshots stored.
    for index in range(6):
        await control.async_tick(dt_util.now() + timedelta(hours=1, seconds=10 * index))
    assert hass.states.get(DISCHARGE).state == "0" and hass.states.get(EXPORT).state == "0"
    plant.calls.clear()
    again = BatteryControl(hass, controller, plan_source=_hold)
    await again.async_start()
    battery_last, export_first = order()
    assert battery_last < export_first, plant.calls


async def test_the_export_limit_is_never_written_while_shadow_only(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None, sell_source=lambda _now: -12.0)
    await control.async_start()
    _options(hass, site, enabled=True)
    with pytest.raises(ControlNotReachable):
        await control.async_export_tick()
    await control.async_look()
    await control.async_stop()
    assert plant.calls == [] and hass.states.get(EXPORT).state == "11.5"


# --- the verification routine through the shell -------------------------------------------------------------------


async def _looks(control: BatteryControl, start, count: int, step_s: float = 10.0):
    now = start
    for _ in range(count):
        await control.async_look(now)
        now += timedelta(seconds=step_s)
    return now


async def test_enabling_runs_the_check_and_control_runs_the_plan_only_once_it_passed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: ("grid_charge", 3.0, None))
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    now = await _looks(control, dt_util.now(), 1)
    assert control.state() == "verifying" and control.status()["line"] == "checking"
    assert control.status()["verification"]["mode"] == "hold"
    now = await _looks(control, now, 400)
    report = control.report()
    assert gate.report_status(report) == "passed", report
    assert {mode: row["outcome"] for mode, row in report["modes"].items()} == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored",
    }
    assert control.state() == "on" and control.allowed() == {"hold", "grid_charge", "discharge_house"}
    # Now the plan runs: a grid charge at 3 kW.
    await _looks(control, now + timedelta(minutes=5), 6)
    assert hass.states.get(MODE).state == "Command Charging (Grid First)" and hass.states.get(CHARGE).state == "3"
    # The report is stored per firmware key and survives a restart.
    stored = hass_storage[store_key(site.entry_id)]["data"]["reports"]
    assert gate.report_status(stored[control.firmware_key()]) == "passed"


async def test_a_battery_that_does_not_hold_fails_the_check_and_control_stays_off(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    plant.obeys = False
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    now = await _looks(control, dt_util.now(), 300)
    assert control.state() == "failed" and control.allowed() == frozenset()
    assert control.status()["line"] == "failed:hold:ignored"
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    plant.calls.clear()
    await _looks(control, now + timedelta(hours=2), 6)
    assert plant.calls == [], "no plan action and no new check without the person turning it on again"
    # Off and on again: the check runs again.
    _options(hass, site, enabled=False)
    await _looks(control, now + timedelta(hours=3), 1)
    _options(hass, site, enabled=True)
    await _looks(control, now + timedelta(hours=4), 1)
    assert control.state() == "verifying"


async def test_a_mode_that_cannot_be_judged_now_is_tried_again_later_not_failed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    house = {"known": False}
    monkeypatch.setattr(
        BatteryControl, "_grid_import_kw", lambda self: HOUSE_KW + plant.power_kw() if house["known"] else None
    )
    control = BatteryControl(hass, controller)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    started = dt_util.now()
    now = await _looks(control, started, 250)
    report = control.report()
    assert control.verification is None and report["attempts"] == 1
    assert report["modes"]["discharge_house"]["outcome"] == "not_judgeable:house_deficit_unknown"
    assert gate.report_status(report) == "passed" and control.allowed() == {"hold", "grid_charge"}
    # Too soon for another run; an hour on, only the open mode (and the hand-back check) is tried.
    house["known"] = True
    await _looks(control, now, 2)
    assert control.verification is None and control.waiting_for == "retry_gap"
    now = await _looks(control, started + timedelta(hours=1, minutes=1), 1)
    assert [result.mode for result in control.verification.results] == ["discharge_house", "normal"]
    await _looks(control, now, 200)
    assert control.report()["modes"]["discharge_house"]["outcome"] == "obeyed"
    assert control.allowed() == {"hold", "grid_charge", "discharge_house"} and control.report()["attempts"] == 2


async def test_no_check_runs_while_the_firmware_was_never_read(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_remove(FIRMWARE)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    await _looks(control, dt_util.now(), 20)
    status = control.status()
    assert control.verification is None and plant.calls == []
    assert status["state"] == "waiting" and status["line"] == "waiting:firmware_unknown"
    assert "firmware_unknown" in status["needs"]
    # The firmware reads: the check starts.
    hass.states.async_set(FIRMWARE, "V100R001C21SPC117")
    await _looks(control, dt_util.now() + timedelta(minutes=5), 1)
    assert control.state() == "verifying" and "firmware_unknown" not in control.status()["needs"]


async def test_normal_left_open_is_judged_on_a_hold_written_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    control._enabled_seen = True
    _passed(control, normal="skipped")
    control.reports[control.firmware_key()]["last_run_at"] = (dt_util.now() - timedelta(hours=3)).isoformat()
    assert control.state() == "waiting"
    now = await _looks(control, dt_util.now(), 1)
    assert [result.mode for result in control.verification.results] == ["hold", "normal"]
    await _looks(control, now, 200)
    assert control.report()["modes"]["normal"]["outcome"] == "restored" and control.state() == "on"
    assert any(call[2].get("entity_id") == DISCHARGE and call[2].get("value") == 0.0 for call in plant.calls)
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"


async def test_a_firmware_change_hands_back_and_checks_again_at_the_next_suitable_evening(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    noon = dt_util.as_local(dt_util.now()).replace(hour=12, minute=0)
    await _looks(control, noon, 6)
    assert control.executor.state.phase == "active"
    hass.states.async_set(FIRMWARE, "V100R001C21SPC118")
    await _looks(control, noon + timedelta(minutes=1), 6)
    assert control.executor.state.phase == "idle" and hass.states.get(SWITCH).state == "off"
    assert control.state() == "waiting" and control.status()["line"] == "waiting_after_update:not_evening"
    await _looks(control, noon.replace(hour=19), 1)
    assert control.state() == "verifying" and control.status()["line"] == "checking_after_update"


async def test_a_look_writes_nothing_once_the_opt_in_is_turned_off_under_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    flipped = {"done": False}
    original = BatteryControl._read_facts

    def flip(self):
        # Another path writes the entry just after the look read the options.
        if not flipped["done"] and self._lock.locked():
            flipped["done"] = True
            _options(hass, site, enabled=False)
        return original(self)

    monkeypatch.setattr(BatteryControl, "_read_facts", flip)
    await control.async_look(dt_util.now())
    assert flipped["done"] and plant.calls == []


async def test_a_settings_write_waits_for_the_look_in_progress(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await control._lock.acquire()
    update = asyncio.ensure_future(control.async_update_options({gate.CONF_ENABLED: False}))
    await asyncio.sleep(0.01)
    assert not update.done() and control.options().enabled, "the entry is written only under the lock"
    control._lock.release()
    await update
    assert not control.options().enabled


# --- a part of the site that raises ---------------------------------------------------------------------------------


async def test_a_held_car_read_that_raises_neither_stops_the_look_nor_blocks_the_hand_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active"

    def boom(self, charger):
        raise RuntimeError("site read failed")

    monkeypatch.setattr(type(controller), "load_balancing_limit", boom)
    inputs = control.inputs(dt_util.now(), control.executor.control)
    assert inputs.car_first and not inputs.car_held, "the cautious defaults"
    await _looks(control, dt_util.now() + timedelta(minutes=2), 3)
    assert control._failed_looks == 0 and control.executor.state.phase == "active"
    status = await control.async_update_options({gate.CONF_ENABLED: False})
    assert status["state"] == "off"
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"


async def test_a_stop_hands_back_on_the_fewest_reads_when_the_look_s_inputs_raise(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active"

    def boom(self, now, control, **kwargs):
        raise RuntimeError("site read failed")

    monkeypatch.setattr(BatteryControl, "inputs", boom)
    await control.async_update_options({gate.CONF_ENABLED: False})  # does not raise
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    assert control.executor.state.phase == "idle" and not control.executor.state.holds_snapshot


async def test_looks_that_keep_failing_hand_the_plant_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    now = await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active"

    def boom(self, now, options):
        raise RuntimeError("plan read failed")

    monkeypatch.setattr(BatteryControl, "_control_slot", boom)
    now = await _looks(control, now, control_module.FAILED_LOOKS_HAND_BACK - 1)
    assert hass.states.get(DISCHARGE).state == "0", "a failure or two is waited out"
    await _looks(control, now, 6)
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    assert not control.executor.state.holds_snapshot


# --- the owner's answers --------------------------------------------------------------------------------------------


async def test_a_plant_in_ai_mode_is_only_watched_without_the_acknowledgement(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_set(WORK_MODE, "ai_mode")
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 4)
    assert plant.calls == []
    status = control.status()
    assert "ai_takeover_ack" in status["needs"] and status["line"] == "watching:ai_mode"


async def test_with_the_acknowledgement_ai_mode_is_taken_over_and_turning_off_leaves_the_reminder(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_set(WORK_MODE, "ai_mode")
    control = await _started(hass, site, controller, plan_source=_hold)
    _options(hass, site, ai_takeover_ack=True)
    now = await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active" and hass.states.get(DISCHARGE).state == "0"
    assert control.took_from_ai
    status = await control.async_update_options({gate.CONF_ENABLED: False})
    # Handed back before the answer, to Maximum Self Consumption (Remote EMS off), with the reminder for the card.
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(WORK_MODE).state == "max_self_consumption"
    assert hass.states.get(DISCHARGE).state == "23.04"
    assert [note["code"] for note in status["notes"]] == ["ai_mode_back"]
    # The person sets AI mode again in the app: the reminder is done.
    hass.states.async_set(WORK_MODE, "ai_mode")
    await _looks(control, now + timedelta(minutes=1), 1)
    assert control.status()["notes"] == []


async def test_withdrawing_the_acknowledgement_hands_an_ai_mode_takeover_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_set(WORK_MODE, "ai_mode")
    control = await _started(hass, site, controller, plan_source=_hold)
    _options(hass, site, ai_takeover_ack=True)
    now = await _looks(control, dt_util.now(), 6)
    assert control.took_from_ai and hass.states.get(DISCHARGE).state == "0"
    status = await control.async_update_options({gate.CONF_AI_TAKEOVER_ACK: False})
    now = await _looks(control, now, 6)
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    status = control.status()
    assert [note["code"] for note in status["notes"]] == ["ai_mode_back"]
    assert "ai_takeover_ack" in status["needs"] and status["line"] == "watching:ai_mode"
    # Not taken again from Maximum Self Consumption; once AI mode is back, only watched.
    plant.calls.clear()
    now = await _looks(control, now + timedelta(minutes=10), 6)
    assert plant.calls == []
    hass.states.async_set(WORK_MODE, "ai_mode")
    await _looks(control, now, 3)
    assert plant.calls == [] and not control.took_from_ai and control.status()["notes"] == []


async def test_back_to_shadow_only_after_an_ai_mode_takeover_leaves_the_reminder(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_set(WORK_MODE, "ai_mode")
    control = await _started(hass, site, controller, plan_source=_hold)
    _options(hass, site, ai_takeover_ack=True)
    await _looks(control, dt_util.now(), 6)
    assert control.took_from_ai
    status = await control.async_update_options({gate.CONF_SHADOW_ONLY: True})
    assert hass.states.get(WORK_MODE).state == "max_self_consumption"
    assert [note["code"] for note in status["notes"]] == ["ai_mode_back"]


async def test_a_check_that_fails_after_an_ai_mode_takeover_leaves_the_reminder(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    hass.states.async_set(WORK_MODE, "ai_mode")
    plant.obeys = False
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False, ai_takeover_ack=True)
    await _looks(control, dt_util.now(), 300)
    status = control.status()
    assert status["state"] == "failed" and hass.states.get(WORK_MODE).state == "max_self_consumption"
    assert [note["code"] for note in status["notes"]] == ["ai_mode_back"]


IMPORT_LIMIT = "number.sigen_plant_grid_import_limitation"


def _import_limit(hass: HomeAssistant, state: str) -> None:
    """The plant's Grid Import Limitation (register 40040), enabled and reading `state` kW."""
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id="ENTRY", identifiers={("sigen", "plant")})
    owner = hass.config_entries.async_get_entry("ENTRY")
    entry = registry.async_get_or_create(
        "number", "sigen", "ENTRY_sigen_plant_plant_grid_maximum_import_limitation", device_id=device.id,
        config_entry=owner, suggested_object_id="sigen_plant_grid_import_limitation",
    )
    assert entry.entity_id == IMPORT_LIMIT
    hass.states.async_set(IMPORT_LIMIT, state, {"unit_of_measurement": "kW"})


@pytest.mark.parametrize(
    ("fuse_a", "import_limit_acts", "limit", "written"),
    [
        # The default 4 kW peak on one phase leaves 5.7 A of 25 A (less the 2 A margin): 3.935 kW balanced.
        (25.0, False, None, "3.935"),
        # Shown to act and reading under the fuse: only the live clamp on the measured headroom.
        (25.0, True, "10", "12.47"),
        # Shown to act but reading above the fuse: the cap stays.
        (25.0, True, "100", "3.935"),
    ],
)
async def test_the_grid_charge_is_capped_per_phase_until_the_import_limit_is_shown_to_act_and_set_under_the_fuse(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], fuse_a: float,
    import_limit_acts: bool, limit: str | None, written: str,
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=fuse_a)
    if limit is not None:
        _import_limit(hass, limit)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 15.0, None))
    _options(hass, site, import_limit_acts=import_limit_acts)
    await _looks(control, dt_util.now(), 6)
    assert hass.states.get(CHARGE).state[:len(written)] == written
    assert any(row["kind"] == "restricted" for row in control.log) is (written != "12.47")


async def test_no_grid_charge_where_the_cap_leaves_too_little(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 15.0, None))
    await _looks(control, dt_util.now(), 6)
    assert plant.calls == []
    assert [row["reason"] for row in control.log if row["kind"] == "restricted"] == ["grid_charge_cap"]


async def test_a_discharge_to_the_grid_keeps_the_sun_s_export_and_its_own_under_the_fuse_on_each_phase(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=25.0)
    export = {"a": [15.0, 3.0, 3.0]}
    monkeypatch.setattr(BatteryControl, "_phase_export_a", lambda self, volts: export["a"])
    control = await _started(hass, site, controller, plan_source=lambda _now: ("export_discharge", 20.0, None))
    _options(hass, site, allow_sell=True)
    await _looks(control, dt_util.now(), 6)
    # 25 A less the 2 A margin less the sun's 15 A on L1 leaves 8 A a phase: 5.543 kW balanced.
    volts = 400.0 / 3 ** 0.5
    assert float(hass.states.get(DISCHARGE).state) == pytest.approx(8.0 * 3 * volts / 1000, abs=0.002)
    assert [row["reason"] for row in control.log if row["kind"] == "restricted"] == ["export_fuse_cap"]
    # The sun's export near the fuse: no discharge to the grid; a phase not known: none either.
    # (The fake battery discharges 2 kW, 2.9 A a phase, of the export it is told to.)
    export["a"] = [26.0, 3.0, 3.0]
    await _looks(control, dt_util.now() + timedelta(minutes=5), 6)
    assert hass.states.get(SWITCH).state == "off"
    export["a"] = None
    assert control._export_cap_kw() == 0.0


async def test_the_check_leaves_the_charge_mode_for_later_where_the_cap_leaves_no_grid_charge(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    await _looks(control, dt_util.now(), 400)
    report = control.report()
    assert report["modes"]["charge"]["outcome"] == "not_judgeable:grid_charge_cap"
    assert gate.report_status(report) == "passed" and "grid_charge" not in control.allowed()
    assert not any(call[2].get("option") == "Command Charging (Grid First)" for call in plant.calls), "no grid charge"


async def test_the_chargers_last_currents_count_against_the_grid_charge_cap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=40.0)
    control = BatteryControl(hass, controller)
    control.facts = control._read_facts()
    # The charger's last current: what load balancing wrote, else what SpotNav asked; 16 A when neither is known.
    charger = (controller.config.get("charger_entry_ids") or [])[0]
    monkeypatch.setattr(type(controller), "charger_allowed_a", _CHARGER_ALLOWED_A)
    assert controller.charger_allowed_a(charger) is None and control._cars_allowed_a() == 16.0
    monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, entry_id: 10.0)
    assert control._cars_allowed_a() == 10.0
    volts = 400.0 / 3 ** 0.5
    assert control._grid_charge_cap_kw(dt_util.now(), control.options()) == pytest.approx(
        (40.0 - 2.0 - 10.0 - 4000.0 / volts) * 3 * volts / 1000
    )


async def test_the_learned_per_phase_house_peaks_replace_the_one_phase_rule_after_two_weeks(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    from custom_components.spotnav.site.phase_peaks import DAY_MINUTES, MARGIN_A, READY_DAYS, HourMark

    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    control = BatteryControl(hass, controller)
    control.facts = control._read_facts()
    now = dt_util.now()
    volts = 400.0 / 3 ** 0.5
    # Nothing learned yet: 4 kW on one phase leaves no grid charge worth running on 20 A.
    assert control._grid_charge_cap_kw(now, control.options()) < gate.MIN_RATE_KW
    cap = control.status()["grid_charge_cap"]
    assert cap["rule"] == "conservative" and cap["cap_kw"] < gate.MIN_RATE_KW and cap["main_fuse_a"] == 20.0
    assert cap["learned"]["ready"] is False
    # Two weeks of counted hours: L3 at most 9 A.
    hour = int(now.timestamp() // 3600)
    marks = controller.phase_peaks.peaks.hours
    for day in range(READY_DAYS):
        for offset in range(DAY_MINUTES // 30 + 1):
            marks[hour - 24 * day - offset - 1] = HourMark(house_a=(5.0, 3.0, 9.0), minutes=30, total_all=17.0,
                                                           total_counted=17.0)
    expected = (20.0 - 2.0 - (9.0 + MARGIN_A)) * 3 * volts / 1000
    assert control._grid_charge_cap_kw(now, control.options()) == pytest.approx(expected)
    cap = control.status()["grid_charge_cap"]
    assert cap["rule"] == "learned" and cap["cap_kw"] == pytest.approx(expected, abs=0.01)
    assert cap["learned"]["peaks_a"] == {"L1": 5.0 + MARGIN_A, "L2": 3.0 + MARGIN_A, "L3": 9.0 + MARGIN_A}
    assert control.snapshot()["status"]["grid_charge_cap"]["learned"]["days_counted"] == READY_DAYS


async def test_the_site_samples_its_house_per_phase_without_raising(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    start = dt_util.utcnow().timestamp()
    for second in range(0, 240, 10):
        controller.phase_peaks.sample(controller, start + second)
    assert not controller.phase_peaks._failed


async def test_selling_from_the_battery_is_off_by_default(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("export_discharge", 4.0, None))
    now = await _looks(control, dt_util.now(), 4)
    assert plant.calls == []
    assert [row["reason"] for row in control.log if row["kind"] == "restricted"] == ["not_verified:export_discharge"]
    _options(hass, site, allow_sell=True)
    await _looks(control, now, 6)
    assert hass.states.get(MODE).state == "Command Discharging (PV First)" and hass.states.get(DISCHARGE).state == "4"


# --- the site's wiring ------------------------------------------------------------------------------------------------


async def test_the_site_starts_its_battery_control_in_the_background_and_unload_hands_back_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, own=True)
    control = controller.battery_control
    await hass.async_block_till_done()
    assert control._started and control._timer_cancel is not None
    assert controller.battery_shadow.spotnav_acting() is False
    control._plan_source = lambda _now: ("grid_charge", 3.0, None)
    _on(hass, site, control)
    await _looks(control, dt_util.now(), 6)
    assert hass.states.get(MODE).state == "Command Charging (Grid First)"
    # SpotNav's own grid charge is no one else's: the outside-control watch is told.
    assert controller.battery_shadow.spotnav_acting() is True
    seen: dict[str, str] = {}
    shadow_shutdown = controller.battery_shadow.async_shutdown

    async def shutdown() -> None:
        seen["switch"] = hass.states.get(SWITCH).state
        await shadow_shutdown()

    monkeypatch.setattr(controller.battery_shadow, "async_shutdown", shutdown)
    assert await hass.config_entries.async_unload(site.entry_id)
    assert seen == {"switch": "off"}, "the battery was handed back before the rest of the site shut down"
    assert hass.states.get(CHARGE).state == "20.6"


async def test_the_plant_s_changes_count_as_spotnav_s_own_for_a_while_after_a_hand_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    controller.battery_shadow.spotnav_acting = control.acting
    await _looks(control, dt_util.now(), 6)
    assert controller.battery_shadow.spotnav_acting()
    await control.async_stop()
    assert control.executor.state.phase == "idle"
    # The work-mode sensor may show the hand-back a poll late: still SpotNav's own.
    assert controller.battery_shadow.spotnav_acting()
    released = control.executor.state.released_at
    later = released + timedelta(seconds=control_module.ACTING_GRACE_S + 1)
    monkeypatch.setattr(control_module.dt_util, "now", lambda: later)
    assert not controller.battery_shadow.spotnav_acting()


async def test_home_assistant_s_stop_saves_the_sessions_first_and_bounds_the_batteries_hand_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    import custom_components.spotnav as integration  # noqa: PLC0415
    from custom_components.spotnav.runtime import charger_data  # noqa: PLC0415

    site, controller, plant = await _site(hass, monkeypatch, hass_storage, own=True)
    await hass.async_block_till_done()
    order: list[str] = []
    chargers = [data for entry in hass.config_entries.async_entries("spotnav")
                if (data := charger_data(hass, entry.entry_id)) is not None]
    assert chargers
    for data in chargers:
        flush = data.controller.async_flush_session

        async def flushed(flush=flush) -> None:
            order.append("session")
            await flush()

        monkeypatch.setattr(data.controller, "async_flush_session", flushed)

    async def slow_hand_back() -> None:
        # A plant that never answers; the site's own shutdown after it finds the hand-back done.
        order.append("battery")
        if order.count("battery") == 1:
            await asyncio.sleep(3600)

    monkeypatch.setattr(controller.battery_control, "async_shutdown", slow_hand_back)
    monkeypatch.setattr(integration, "BATTERY_STOP_TOTAL_S", 0.05)
    async with asyncio.timeout(5):
        await integration._async_stop(hass)
    first = order.index("battery")
    assert first > 0 and set(order[:first]) == {"session"}, order
    assert controller._closed
    await BatteryControl.async_shutdown(controller.battery_control)


async def test_removing_the_site_hands_a_stored_snapshot_back_before_the_store_goes(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _export_limit(hass)
    control = await _started(hass, site, controller, plan_source=_hold, sell_source=lambda _now: -12.0)
    await _looks(control, dt_util.now(), 6)
    assert hass.states.get(DISCHARGE).state == "0" and hass.states.get(EXPORT).state == "0"
    key = store_key(site.entry_id)
    # The unload's hand-back did not finish (the plant did not answer): the snapshots are stored.
    plant.raises = True
    monkeypatch.setattr(control_module, "HAND_BACK_DEADLINE_S", 0.05)
    await BatteryControl.async_remove_stored(hass, site.entry_id, dict(site.data))
    assert key in hass_storage, "kept while the plant could not be handed back"
    assert hass_storage[key]["data"]["state"]["snapshot"]
    plant.raises = False
    await BatteryControl.async_remove_stored(hass, site.entry_id, dict(site.data))
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    assert hass.states.get(EXPORT).state == "11.5"
    assert key not in hass_storage


async def test_after_a_restart_the_restore_runs_before_the_first_look(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active"
    # HA stopped without handing back; the plant's integration loads late after the restart.
    hass.states.async_set(SWITCH, "unavailable", {"restored": True})
    monkeypatch.setattr(control_module, "STARTUP_LOOK_S", 0.01)
    again = BatteryControl(hass, controller, plan_source=_hold)
    again.async_launch(site)
    await asyncio.sleep(0.05)
    assert not again._started and again._timer_cancel is None, "no look before the restore"
    hass.states.async_set(SWITCH, "on", {})
    for _ in range(50):
        await asyncio.sleep(0.02)
        if again._timer_cancel is not None:
            break
    assert again._started and again._timer_cancel is not None
    assert again.log[0]["reason"] == "restart" and hass.states.get(SWITCH).state == "off"
    await again.async_shutdown()


async def test_a_start_that_raises_is_tried_again_and_hands_the_plant_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active" and hass.states.get(SWITCH).state == "on"
    monkeypatch.setattr(control_module, "START_RETRY_S", 0.01)
    again = BatteryControl(hass, controller, plan_source=_hold)
    calls = {"n": 0}
    original = BatteryControl._read_facts

    def flaky(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("registry not ready")
        return original(self)

    monkeypatch.setattr(BatteryControl, "_read_facts", flaky)
    again.async_launch(site)
    for _ in range(50):
        await asyncio.sleep(0.02)
        if again._timer_cancel is not None:
            break
    assert again._started and again._timer_cancel is not None and calls["n"] >= 2
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    await again.async_shutdown()


async def test_an_unload_before_the_start_finished_still_hands_back_what_it_holds(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active"
    # The restore waits for a control that never loads; the site unloads meanwhile.
    hass.states.async_set(MODE, "PCS Remote Control", {"restored": True})
    monkeypatch.setattr(control_module, "STARTUP_LOOK_S", 0.01)
    again = BatteryControl(hass, controller, plan_source=_hold)
    again.async_launch(site)
    await asyncio.sleep(0.05)
    assert not again._started and again.executor is not None and again.executor.state.holds_snapshot
    await again.async_shutdown()
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"


# --- the owner's test action and settings --------------------------------------------------------------------------


async def test_the_test_action_ends_by_itself_and_hands_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    await control.async_trial("grid_charge", 3.0, 2, force=False)
    now = await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    assert hass.states.get(MODE).state == "Command Charging (Grid First)" and hass.states.get(CHARGE).state == "3"
    await _looks(control, now + timedelta(minutes=3), 6)
    assert control.trial is None and hass.states.get(SWITCH).state == "off"
    assert any(row["kind"] == "trial_ended" for row in control.log)


async def test_the_test_action_s_grid_charge_follows_the_cap_at_each_look_and_its_power_the_rating(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    _options(hass, site, allow_sell=True)
    await control.async_trial("grid_charge", 3.0, 30, force=False)
    now = await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    assert hass.states.get(MODE).state == "Command Charging (Grid First)" and hass.states.get(CHARGE).state == "3"
    # A car starts at 5 A: the cap leaves no grid charge, and the test action stops charging.
    monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, charger: 5.0)
    await _looks(control, now, 6)
    assert control.trial is not None and hass.states.get(SWITCH).state == "off"
    await control.async_trial("stop", None, 1, force=False)
    # The power asked for is bounded by the battery's own rating.
    rating = control.facts.max_discharge_kw
    assert rating is not None
    answer = await control.async_trial("export_discharge", rating + 30.0, 5, force=False)
    assert answer["trial"]["rate_kw"] == pytest.approx(rating)


async def test_the_test_action_is_refused_while_the_opt_in_is_off_even_forced(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller)
    await control.async_start()
    assert control.options() == gate.Options()
    with pytest.raises(TrialRefused) as refused:
        await control.async_trial("grid_charge", 6.0, 5, force=True)
    assert refused.value.code == "not_enabled"
    _options(hass, site, shadow_only=False)
    with pytest.raises(TrialRefused) as refused:
        await control.async_trial("hold", None, 5, force=False)
    assert refused.value.code == "not_enabled"
    await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    assert plant.calls == [] and control.trial is None


async def test_the_test_action_waits_for_hold_and_normal_to_pass_unless_forced(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    control._enabled_seen = True
    monkeypatch.setattr(BatteryControl, "_async_maybe_verify", lambda self, now, options: asyncio.sleep(0))
    assert control.state() == "waiting"
    with pytest.raises(TrialRefused) as refused:
        await control.async_trial("grid_charge", 6.0, 5, force=False)
    assert refused.value.code == "not_verified" and plant.calls == []
    # Forced: the owner's on-site test runs before the verification.
    await control.async_trial("hold", None, 5, force=True)
    await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    assert hass.states.get(DISCHARGE).state == "0"


async def test_a_forced_test_action_ends_and_hands_back_when_the_opt_in_is_turned_off(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller)
    await control.async_start()
    _options(hass, site, enabled=True)  # shadow only stays on
    await control.async_trial("hold", None, 60, force=True)
    await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    assert hass.states.get(DISCHARGE).state == "0"
    await control.async_update_options({gate.CONF_ENABLED: False})
    assert control.trial is None
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    plant.calls.clear()
    await _looks(control, dt_util.now() + timedelta(seconds=60), 4)
    assert plant.calls == []


async def test_a_forced_test_action_ends_at_the_next_look_when_the_opt_in_goes_off_elsewhere(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller)
    await control.async_start()
    _options(hass, site, enabled=True)
    await control.async_trial("hold", None, 60, force=True)
    await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    _options(hass, site, enabled=False)  # the entry written by another path
    await _looks(control, dt_util.now() + timedelta(seconds=60), 4)
    assert control.trial is None and hass.states.get(SWITCH).state == "off"
    assert any(row["kind"] == "trial_ended" for row in control.log)


async def test_back_to_shadow_only_hands_the_plant_back_and_a_write_that_is_no_restore_is_refused(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 6)
    assert hass.states.get(DISCHARGE).state == "0"
    await control.async_update_options({gate.CONF_SHADOW_ONLY: True})
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    assert control.state() == "shadow_only" and control.executor.state.phase == "idle"
    # The last guard: a write that is not a hand-back is refused when the caller's gate does not allow it.
    op = control_module.Op("discharge_limit", "number", DISCHARGE, 0.0)
    plant.calls.clear()
    assert await control._async_write(op, control.executor, allowed=False) is False
    assert plant.calls == [] and control.log[-1]["kind"] == "refused"


async def test_the_battery_yields_to_a_held_car_unless_the_car_s_departure_has_room(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 3.0, None))
    monkeypatch.setattr(type(controller), "load_balancing_limit", lambda self, charger: (8.0, "house"))
    room = {"value": False}
    monkeypatch.setattr(BatteryControl, "_held_cars_have_room", lambda self, now, held: room["value"])
    inputs = control.inputs(dt_util.now(), control.executor.control if control.executor else control._executor().control)
    assert inputs.car_held and inputs.car_first, "car first: the battery's grid charge gives way"
    room["value"] = True
    inputs = control.inputs(dt_util.now(), control.executor.control)
    assert inputs.car_held and not inputs.car_first, "a car whose departure has room backs off first"


@pytest.mark.parametrize(
    ("departure_h", "windows_h", "behind_kwh", "room"),
    [
        # 22 kWh planned in two hours at 11 kW, ten hours to go: room to spare.
        (10.0, (1.0, 3.0), 0.0, True),
        # 27.5 kWh due within three hours: no room.
        (3.0, (0.0, 2.5), 0.0, False),
        # One hour planned (11 kWh) five hours out has room, unless the running window is 30 kWh behind already.
        (5.0, (1.0, 2.0), 0.0, True),
        (5.0, (1.0, 2.0), 30.0, False),
    ],
)
async def test_a_held_car_has_room_by_its_remaining_need_against_the_time_to_its_departure(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], departure_h: float,
    windows_h: tuple[float, float], behind_kwh: float, room: bool,
) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    from custom_components.spotnav import runtime  # noqa: PLC0415

    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller)
    now = dt_util.now()
    start, end = (now + timedelta(hours=windows_h[0]), now + timedelta(hours=windows_h[1]))
    plan = SimpleNamespace(
        departure=(now + timedelta(hours=departure_h)).isoformat(), auto_owned=True, windows=[(start, end)],
        energy_kwh=11.0 * (windows_h[1] - windows_h[0]), power_kw=11.0, phases=3, amps=16,
    )
    monkeypatch.setattr(runtime, "controller_for", lambda _hass, _entry_id: SimpleNamespace(plan=plan))
    monkeypatch.setattr(
        runtime, "preview_for", lambda _hass, _entry_id: SimpleNamespace(import_target_shortfall_kwh=lambda: behind_kwh)
    )
    assert control._held_cars_have_room(now, ["charger"]) is room
    assert control._held_cars_have_room(now, []) is False



async def test_control_runs_the_hold_variant_the_verification_passed_and_a_restart_hands_it_back_mode_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    key = control.firmware_key()
    control.reports[key]["modes"]["hold"] |= {
        "variant": "pv_first",
        "variants": [{"variant": "msc_no_discharge", "outcome": "ignored"}, {"variant": "pv_first", "outcome": "obeyed"}],
    }
    now = dt_util.now()
    for index in range(6):
        await control.async_tick(now + timedelta(seconds=10 * index))
    calls = [(data.get("value"), data.get("option")) for _domain, service, data in plant.calls if service != "turn_on"]
    # The charge limit at the plant's rating, then mode 4.
    assert calls == [
        (20.6, None), (None, "Command Charging (PV First)")
    ]
    assert plant.calls[0][2]["entity_id"] == CHARGE and hass.states.get(DISCHARGE).state == "23.04"
    assert control.executor.state.variant == "pv_first"

    again = BatteryControl(hass, controller, plan_source=_hold)
    plant.calls.clear()
    await again.async_start()
    # The mode first (back to Maximum Self Consumption), then Remote EMS off; the charge limit keeps the rating.
    assert plant.calls[0][2].get("option") == "Maximum Self Consumption"
    assert hass.states.get(SWITCH).state == "off" and again.executor.state.phase == "idle"
