"""The battery write path's shell on a site, against a fake Sigenergy plant whose entity services behave as the
integration's (read back on the next poll, dropped in read-only mode, raising), behind the site's gate
(`battery/gate.py`): shadow only by default, the opt-in, a passed verification for the plant's firmware."""

from __future__ import annotations

import math

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

from custom_components.spotnav.battery import commands as commands_module, control as control_module, gate
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
        #: The integration never answers (a Modbus or cloud call that hangs): the call waits until cancelled.
        self.hangs = False
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
            if self.hangs:
                await asyncio.Event().wait()
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


def _mode(hass: HomeAssistant, site: MockConfigEntry, mode: str) -> None:
    hass.config_entries.async_update_entry(site, data={**site.data, gate.CONF_MODE: mode})


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
    # Opted in with shadow only stored on (a field-tested Sigenergy plant leaves it off by default; a site that
    # stored it keeps it).
    _options(hass, site, enabled=True, shadow_only=True)
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


async def test_a_write_that_never_answers_is_given_up_on_as_not_taken_and_the_look_ends(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    monkeypatch.setattr(control_module, "CALL_TIMEOUT_S", 0.05)
    plant.hangs = True
    control = await _started(hass, site, controller, plan_source=_hold)
    now = dt_util.now()
    for index in range(4):
        async with asyncio.timeout(5):
            await control.async_look(now + timedelta(seconds=10 * index))
    assert not control._lock.locked()
    # Its own reason: the block clears by itself (`executor.CALL_TIMEOUT_BLOCK_S`), no reset needed.
    assert control.executor.state.blocked == "call_timeout:discharge_limit"
    failed = [row for row in control.log if row["kind"] == "service_failed"]
    assert failed and "no answer" in failed[0]["error"]
    assert any(row["kind"] == "not_taken" and row["why"] == "call_timeout" for row in control.log)
    assert len(plant.calls) == 1
    assert control._failed_looks == 0, "a write given up on is a write not taken, not a failed look"


async def test_a_plant_that_stops_answering_while_held_is_handed_back_once_it_answers_again(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    monkeypatch.setattr(control_module, "CALL_TIMEOUT_S", 0.05)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 3.0, None))
    now = await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active" and hass.states.get(CHARGE).state == "3"
    plant.hangs = True
    calls = len(plant.calls)
    # A plan change needs writes the plant never answers: each look still ends and frees the lock.
    control._plan_source = _hold
    for _ in range(4):
        async with asyncio.timeout(5):
            now = await _looks(control, now, 1)
        assert not control._lock.locked()
    assert len(plant.calls) > calls
    assert any(row["kind"] == "service_failed" for row in control.log)
    # A stop is not held by the hung writes; with the deadline short it leaves the rest stored.
    monkeypatch.setattr(control_module, "HAND_BACK_DEADLINE_S", 0.1)
    async with asyncio.timeout(5):
        await control.async_update_options({gate.CONF_ENABLED: False})
    assert control.executor.state.holds_snapshot
    plant.hangs = False
    monkeypatch.setattr(control_module, "HAND_BACK_DEADLINE_S", 30.0)
    await _looks(control, now, 6)
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(CHARGE).state == "20.6"
    assert not control.executor.state.holds_snapshot


def _plant_silent(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, plant: Plant):
    """Every entity of the plant `unavailable` and no battery reading, as when its Modbus link goes silent; Home
    Assistant skips a service on an unavailable entity. Returns the plant's answer coming back."""
    saved: dict[str, tuple[str, dict[str, Any]]] = {}
    for entry in er.async_entries_for_config_entry(er.async_get(hass), "ENTRY"):
        state = hass.states.get(entry.entity_id)
        if state is not None:
            saved[entry.entity_id] = (state.state, dict(state.attributes))
            hass.states.async_set(entry.entity_id, "unavailable", dict(state.attributes))
    monkeypatch.setattr(BatteryControl, "_battery_power", lambda self: (None, None))
    plant.read_only = True

    def back() -> None:
        plant.read_only = False
        for entity_id, (state, attributes) in saved.items():
            hass.states.async_set(entity_id, state, attributes)
        monkeypatch.setattr(BatteryControl, "_battery_power", lambda self: (plant.power_kw(), 5.0))

    return back


@pytest.mark.parametrize(("silent_s", "handed_back"), [(90, False), (240, True)])
async def test_a_plant_silent_for_a_minute_and_a_half_is_ridden_out_and_a_longer_silence_handed_back(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], silent_s: int,
    handed_back: bool,
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 3.0, None))
    now = await _looks(control, dt_util.now(), 6)
    assert control.executor.state.phase == "active"
    calls, rows = len(plant.calls), len(list(control.log))
    back = _plant_silent(hass, monkeypatch, plant)
    now = await _looks(control, now, silent_s // 10)
    back()
    now = await _looks(control, now, 40)
    new = list(control.log)[rows:]
    hand_backs = [row["reason"] for row in new if row["kind"] == "hand_back"]
    assert control.executor.state.blocked is None and control.executor.state.phase == "active"
    assert hass.states.get(MODE).state == "Command Charging (Grid First)"
    if handed_back:
        assert hand_backs == ["stale_data"], "handed back once, never blocked, and taken again after the rearm"
    else:
        assert hand_backs == [] and len(plant.calls) == calls and new == []


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
    _options(hass, site, enabled=True, shadow_only=True)
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
    assert control.state() == "on" and control.allowed() == {"hold", "grid_charge", "discharge_house", "export_discharge"}
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
    assert control.allowed() == {"hold", "grid_charge", "discharge_house", "export_discharge"} and control.report()["attempts"] == 2


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
    assert status["battery_found"] is True, "a plant's battery is named in the status"
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


def _preheat_switch(hass: HomeAssistant, state: str, cells: str) -> str:
    """The `sigen` integration's "ESS Preheating Enable" switch on the plant's device, and the cells at `cells` °C."""
    registry = er.async_get(hass)
    battery = registry.async_get(SIGEN["battery"])
    assert battery is not None
    entry = registry.async_get_or_create(
        "switch", "sigen", "ENTRY_sigen_plant_plant_ess_preheating_enable", device_id=battery.device_id,
        config_entry=hass.config_entries.async_get_entry("ENTRY"), suggested_object_id="sigen_plant_ess_preheating_enable",
    )
    hass.states.async_set(entry.entity_id, state)
    hass.states.async_set("sensor.sigen_plant_ess_average_cell_temperature", cells, {"unit_of_measurement": "°C"})
    return entry.entity_id


async def test_a_cold_battery_with_preheating_off_gets_one_line_while_under_control_and_nothing_is_written(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    switch = _preheat_switch(hass, "off", "2.1")
    control = await _started(hass, site, controller, plan_source=_hold)
    _passed(control)
    now = await _looks(control, dt_util.now(), 2)
    assert control.status()["preheat"] is None, "half an hour first"
    now = await _looks(control, now + timedelta(minutes=31), 2)
    line = control.status()["preheat"]
    assert line is not None and line["code"] == "off" and line["entity_id"] == switch and line["cell_c"] == 2.1
    assert line["device_id"] is not None
    assert [event["kind"] for event in control.log if event["kind"].startswith("preheat")] == ["preheat_shown"]
    assert all(call[2].get("entity_id") != switch for call in plant.calls), "the heater is never written"
    # Turned on by the person: the line clears itself.
    hass.states.async_set(switch, "on")
    await _looks(control, now, 1)
    assert control.status()["preheat"] is None
    assert control.snapshot()["preheat"]["counts"] == {"shown_off": 1}


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
        # Shown to act and reading at most what keeps the worst phase under the fuse (7.93 kW in all with 4 kW on
        # one phase): only the live clamp on the measured headroom (started at 6.47 kW under the plant's cover of the
        # house, which is counted out of the headroom, and up after the dwell).
        (25.0, True, "7.5", "13.2"),
        # Shown to act but reading more than that (the plant limits the total, not each phase): the cap stays.
        (25.0, True, "10", "3.935"),
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
    await _looks(control, dt_util.now(), 16)
    assert hass.states.get(CHARGE).state[:len(written)] == written
    assert any(row["kind"] == "restricted" for row in control.log) is (written != "13.2")


async def test_the_status_suggests_an_import_limit_that_covers_the_worst_phase_and_never_writes_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    # The total rule alone here: the per-phase hold as not recorded (it ships recorded for Sigenergy).
    _field_tested(monkeypatch, field_tested=False)
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 15.0, None))
    # The limit's entity is disabled (the integration's default): nothing read, the suggestion still made.
    block = control.status()["grid_charge_cap"]["import_limit"]
    # Held per phase (`commands.ImportLimitPhases`): never while the owner's on-site test is not recorded.
    # The per-phase rule counts the site's own safety margin (1 A by default): 3 x 19 A x 231 V.
    unrecorded = {"max_kw": 13.16, "holds": False, "why": "not_field_tested"}
    assert block == {"value_kw": None, "max_kw": 4.47, "suggested_kw": 4.4, "holds": False, "why": "sources_unknown",
                     "per_phase": unrecorded}
    await _looks(control, dt_util.now(), 1)
    # The fuse as three-phase power (13.8 kW), as left after the on-site test: read, and refused as a guard.
    _import_limit(hass, "13.8")
    _options(hass, site, import_limit_acts=True)
    cap = control.status()["grid_charge_cap"]
    assert cap["import_limit"] == {"value_kw": 13.8, "max_kw": 4.47, "suggested_kw": 4.4, "holds": False, "why": None,
                                   "per_phase": unrecorded}
    assert cap["rule"] == "conservative" and cap["cap_kw"] < gate.MIN_RATE_KW
    await _looks(control, dt_util.now(), 6)
    assert plant.calls == [], "no grid charge under a limit that lets one phase pass the fuse"
    # Set to the suggested value by the person: the rule holds and the charge runs; SpotNav wrote the limit never.
    _import_limit(hass, "4.4")
    cap = control.status()["grid_charge_cap"]
    assert cap["import_limit"]["holds"] is True and cap["rule"] == "import_limit" and cap["cap_kw"] is None
    await _looks(control, dt_util.now() + timedelta(minutes=5), 6)
    assert hass.states.get(MODE).state == "Command Charging (Grid First)"
    assert not any(call[2].get("entity_id") == IMPORT_LIMIT for call in plant.calls)
    # A car that keeps its last 6 A if Home Assistant stops takes 12 A of the total: 4.4 kW no longer guards.
    monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, charger: 6.0)
    cap = control.status()["grid_charge_cap"]
    assert cap["import_limit"] == {"value_kw": 4.4, "max_kw": 1.7, "suggested_kw": 4.4, "holds": False, "why": None,
                                   "per_phase": {"max_kw": None, "holds": False, "why": "not_field_tested"}}
    assert cap["rule"] == "conservative"
    # Not shown to act on site: read and suggested, never in force.
    monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, charger: 0.0)
    _options(hass, site, import_limit_acts=False)
    assert control.status()["grid_charge_cap"]["import_limit"]["holds"] is False


@pytest.mark.parametrize(
    ("case", "why"),
    [("foreign_pv", "foreign_pv"), ("single_phase", "plant_not_three_phase"), ("dc_charger", "dc_charger"),
     ("unread", "sources_unknown")],
)
async def test_the_import_limit_rule_is_refused_where_the_plant_does_not_see_every_source_or_spread_evenly(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], case: str, why: str
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    _import_limit(hass, "4.0")
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 15.0, None))
    _options(hass, site, import_limit_acts=True)
    now = dt_util.now()
    await control._async_read_sources(now)
    assert control.status()["grid_charge_cap"]["import_limit"]["holds"] is True
    assert control.import_limit_guards() is True
    if case == "foreign_pv":
        # The Energy dashboard's sun is another inverter's (not an entity of the plant's integration entry).
        control._sources = (now, control._foreign_sun(("sensor.other_inverter_energy",)))
        assert control._foreign_sun(("sensor.sigen_plant_battery_power",)) is None
    elif case == "single_phase":
        monkeypatch.setattr(BatteryControl, "_plant_phases", lambda self, control: 1)
    elif case == "dc_charger":
        charger = hass.config_entries.async_get_entry((controller.config.get("charger_entry_ids") or [])[0])
        hass.config_entries.async_update_entry(charger, data={**charger.data, "current_control": "power"})
    else:
        control._sources = None
    cap = control.status()["grid_charge_cap"]
    assert cap["import_limit"]["holds"] is False and cap["import_limit"]["why"] == why
    assert cap["rule"] == "conservative" and control._grid_charge_cap_kw(now, control.options()) < gate.MIN_RATE_KW
    # The DC charger's own cap asks the same, except that the charger itself is no reason against it.
    assert control.import_limit_guards() is (case == "dc_charger")


async def test_a_charger_the_hardware_test_holds_counts_nothing_only_in_the_test_s_caps(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=40.0)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    charger = (controller.config.get("charger_entry_ids") or [])[0]
    monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, entry_id: 16.0)
    counted: list[str] = []
    monkeypatch.setattr(
        type(controller.charger_test_hold), "counts_nothing", lambda self, entry_id: counted.append(entry_id) or True
    )
    now = dt_util.now()
    assert control._cars_allowed_a(held_off=True) == 16.0, "only while a hardware test runs"
    control._hw_run = True
    assert control._cars_allowed_a() == 16.0, "normal control never asks the hold"
    assert counted == []
    assert control._cars_allowed_a(held_off=True) == 0.0 and counted == [charger]
    held = control._phase_caps_kw(now, held_off=True)
    assert all(a >= b for a, b in zip(held, control._phase_caps_kw(now), strict=True))
    assert sum(held) > sum(control._phase_caps_kw(now)), "16 A more room on each phase"
    # Where the hold says the charger may not count nothing, it keeps its current.
    monkeypatch.setattr(type(controller.charger_test_hold), "counts_nothing", lambda self, entry_id: False)
    assert control._cars_allowed_a(held_off=True) == 16.0
    assert control._phase_caps_kw(now, held_off=True) == control._phase_caps_kw(now)
    control._hw_run = False


async def test_the_import_limit_guards_a_charger_only_while_it_holds_now_counting_the_other_chargers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    await control._async_read_sources(dt_util.now())
    charger = (controller.config.get("charger_entry_ids") or [])[0]
    # The person's word alone is not enough: the limit must read, and read little enough.
    _options(hass, site, import_limit_acts=True)
    assert control.import_limit_guards(without_charger=charger) is False, "the limit reads nothing"
    _import_limit(hass, "13.8")
    assert control.import_limit_guards(without_charger=charger) is False, "the fuse as power guards no phase"
    _import_limit(hass, "4.4")
    assert control.import_limit_guards(without_charger=charger) is True
    _options(hass, site, import_limit_acts=False)
    assert control.import_limit_guards(without_charger=charger) is False
    _options(hass, site, import_limit_acts=True)
    # The charger's own (lifted) current never undoes the lift; another charger's does.
    monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, entry_id: 30.0)
    assert control.import_limit_guards(without_charger=charger) is True
    assert control.import_limit_guards(without_charger="another") is False
    assert control._cars_allowed_a(without=charger) == 0.0 and control._cars_allowed_a() == 30.0
    # Anything that cannot be read: not guarded.
    monkeypatch.setattr(type(controller.phase_peaks), "learned", lambda self, now: 1 / 0)
    assert control.import_limit_guards(without_charger=charger) is False


def _sigen_entity(hass: HomeAssistant, domain: str, key: str, object_id: str, state: str | None) -> str:
    """An entity of the owner's plant by its `sigen` unique-id key, enabled and reading `state` (`None`: disabled,
    the integration's default for these)."""
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id="ENTRY", identifiers={("sigen", "plant")})
    entry = registry.async_get_or_create(
        domain, "sigen", f"ENTRY_sigen_plant_{key}", device_id=device.id,
        config_entry=hass.config_entries.async_get_entry("ENTRY"), suggested_object_id=object_id,
    )
    registry.async_update_entity(
        entry.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION if state is None else None
    )
    if state is None:
        hass.states.async_remove(entry.entity_id)
    else:
        hass.states.async_set(entry.entity_id, state)
    return entry.entity_id


def _per_phase_plant(hass: HomeAssistant, **states: str | None) -> None:
    """The readings a limit held per phase needs, as on the owner's plant: independent phase power control on, the
    grid sensor online, the Output Type three-phase with neutral."""
    readings = {"switch": "on", "grid_sensor": "online", "output_type": "l1_l2_l3_n"} | states
    _sigen_entity(hass, "switch", "plant_independent_phase_power_control_enable",
                  "sigen_plant_independent_phase_power_control", readings["switch"])
    _sigen_entity(hass, "sensor", "plant_grid_sensor_status", "sigen_plant_grid_sensor_status",
                  readings["grid_sensor"])
    _sigen_entity(hass, "sensor", "inverter_output_type", "sigen_inverter_output_type", readings["output_type"])


def _field_tested(monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    """The recipe with the owner's on-site test of the per-phase hold recorded (it ships unrecorded)."""
    phases = replace(commands_module.SIGENERGY_IMPORT_LIMIT_PHASES, **({"field_tested": True} | changes))
    tested = replace(commands_module.SIGENERGY_CONTROL, import_limit_phases=phases)
    monkeypatch.setattr(
        commands_module, "CONTROLS",
        tuple(tested if c is commands_module.SIGENERGY_CONTROL else c for c in commands_module.CONTROLS),
    )


def test_the_shipped_recipe_records_the_owner_s_test_of_the_limit_held_per_phase_and_no_per_phase_output() -> None:
    phases = commands_module.SIGENERGY_CONTROL.import_limit_phases
    assert phases is not None and phases.field_tested is True and phases.phase_output_max_kw is None
    assert phases.sensor_online == "online" and phases.output_type == "l1_l2_l3_n"


@pytest.mark.parametrize(
    ("case", "why"),
    [
        ("held", None),
        ("unrecorded", "not_field_tested"),
        ("not_said", "not_said_to_act"),
        ("switch_off", "phase_control_off"),
        ("switch_unread", "phase_control_off"),
        ("sensor_offline", "grid_sensor_offline"),
        ("sensor_unread", "grid_sensor_offline"),
        ("output_unread", "output_type_unread"),
        ("single_phase", "plant_not_three_phase"),
        ("no_neutral", "output_type_not_supported"),
        ("foreign_pv", "foreign_pv"),
        ("sources_unread", "sources_unknown"),
        ("dc_charger", "dc_charger"),
        ("too_high", None),
        ("unset", None),
        ("car", None),
    ],
)
async def test_a_limit_held_per_phase_lifts_the_cap_only_while_every_condition_reads_so(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], case: str, why: str | None
) -> None:
    """The owner's 20 A fuse, where the cap leaves no grid charge at all: a limit held per phase at most 3 x (20 - 2)
    A x 231 V (12.47 kW) lifts it, with the recipe's fact recorded and the site's own word that its limit acts."""
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    _field_tested(monkeypatch, field_tested=case != "unrecorded")
    _per_phase_plant(hass, **{
        "switch_off": {"switch": "off"}, "switch_unread": {"switch": None}, "sensor_offline": {"grid_sensor": "offline"},
        "sensor_unread": {"grid_sensor": None}, "output_unread": {"output_type": None},
        "single_phase": {"output_type": "l_n"}, "no_neutral": {"output_type": "l1_l2_l3"},
    }.get(case, {}))
    if case != "unset":
        _import_limit(hass, "13.8" if case == "too_high" else "12.4")
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 15.0, None))
    now = dt_util.now()
    await control._async_read_sources(now)
    if case == "foreign_pv":
        control._sources = (now, control._foreign_sun(("sensor.other_inverter_energy",)))
    elif case == "sources_unread":
        control._sources = None
    elif case == "dc_charger":
        charger = hass.config_entries.async_get_entry((controller.config.get("charger_entry_ids") or [])[0])
        hass.config_entries.async_update_entry(charger, data={**charger.data, "current_control": "power"})
    elif case == "car":
        monkeypatch.setattr(type(controller), "charger_allowed_a", lambda self, charger: 6.0)
    _options(hass, site, import_limit_acts=case != "not_said")
    assert control._import_limit_phase_refusal() == why
    cap = control.status()["grid_charge_cap"]
    held = case == "held"
    assert cap["import_limit"]["per_phase"]["holds"] is held and cap["import_limit"]["per_phase"]["why"] == why
    assert cap["import_limit"]["holds"] is held
    assert (cap["rule"] == "import_limit") is held and (cap["cap_kw"] is None) is held
    if not held:
        assert control._grid_charge_cap_kw(now, control.options()) < gate.MIN_RATE_KW, "today's rule: no charge"
    if case == "car":
        assert cap["import_limit"]["per_phase"]["max_kw"] is None, "the house and the car alone pass the fuse"


@pytest.mark.parametrize(("margin", "limit", "held"), [(0.0, "13.8", True), (1.0, "13.8", False), (1.0, "13.1", True),
                                                       (None, "13.8", False)])
async def test_a_limit_held_per_phase_counts_the_site_s_own_safety_margin(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any], margin: float | None,
    limit: str, held: bool,
) -> None:
    """The owner's 20 A fuse with his margin of 0 A allows 13.8 kW (the fuse itself, 3 x 20 A x 231 V = 13.86 kW); a
    site with the default 1 A, 13.16 kW. The total rule and the executor keep their own margin."""
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    _field_tested(monkeypatch)
    _per_phase_plant(hass)
    _import_limit(hass, limit)
    data = {k: v for k, v in site.data.items() if k != "safety_margin_a"}
    hass.config_entries.async_update_entry(site, data=data if margin is None else {**data, "safety_margin_a": margin})
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 15.0, None))
    _options(hass, site, import_limit_acts=True)
    await control._async_read_sources(dt_util.now())
    cap = control.status()["grid_charge_cap"]
    assert cap["import_limit"]["per_phase"]["holds"] is held, cap
    assert (cap["cap_kw"] is None) is held
    assert cap["import_limit"]["max_kw"] == 4.47, "the total rule keeps the executor's 2 A"


async def test_a_limit_held_per_phase_is_read_again_at_every_look_and_a_running_charge_falls_back_to_the_cap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=25.0)
    _field_tested(monkeypatch)
    _per_phase_plant(hass)
    _import_limit(hass, "15.0")  # under 3 x 23 A x 231 V = 15.93 kW
    control = await _started(hass, site, controller, plan_source=lambda _now: ("grid_charge", 15.0, None))
    _options(hass, site, import_limit_acts=True)
    now = await _looks(control, dt_util.now(), 16)
    # Only the live clamp on the measured headroom, as under the total rule.
    assert hass.states.get(CHARGE).state[:4] == "13.2"
    assert not any(row["kind"] == "restricted" for row in control.log)
    assert not any(call[2].get("entity_id") == IMPORT_LIMIT for call in plant.calls), "SpotNav never writes it"
    # Someone turns independent phase power control off in the app: the next looks put the cap back.
    _per_phase_plant(hass, switch="off")
    await _looks(control, now, 3)
    assert hass.states.get(CHARGE).state[:5] == "3.935"
    assert any(row["kind"] == "restricted" and row["reason"] == "grid_charge_cap" for row in control.log)


async def test_a_standing_boost_or_raise_counts_where_the_import_limit_lifts_a_charger(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """What SpotNav itself leaves standing in the house if Home Assistant stops (a boost it started, a raise held)
    counts against the plant's import limit as the chargers' last currents do, in the guard and in its status."""
    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    control = await _started(hass, site, controller, plan_source=lambda _now: None)
    await control._async_read_sources(dt_util.now())
    charger = (controller.config.get("charger_entry_ids") or [])[0]
    _options(hass, site, import_limit_acts=True)
    _import_limit(hass, "4.4")
    assert control.import_limit_guards(without_charger=charger) is True
    status = control.status()["grid_charge_cap"]["import_limit"]
    assert status["holds"] is True
    monkeypatch.setattr(BatteryControl, "_heat_standing_a", lambda self: 30.0)
    assert control.import_limit_guards(without_charger=charger) is False
    standing = control.status()["grid_charge_cap"]["import_limit"]
    assert standing["holds"] is False and (standing["max_kw"] or 0.0) < (status["max_kw"] or 0.0)


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
    _mode(hass, site, "max_profit")
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
    from custom_components.spotnav.site.phase_peaks import (
        EXTRA_MARGIN_A, HISTORY_DAY_WEIGHT, MARGIN_A, MIN_READY_DAYS, READY_DAYS, SPREAD_HOURS, HourMark,
    )

    site, controller, plant = await _site(hass, monkeypatch, hass_storage, main_fuse_a=20.0)
    control = BatteryControl(hass, controller)
    control.facts = control._read_facts()
    now = dt_util.now()
    volts = 400.0 / 3 ** 0.5
    # Nothing learned yet: 4 kW on one phase leaves no grid charge worth running on 20 A.
    assert control._grid_charge_cap_kw(now, control.options()) < gate.MIN_RATE_KW
    cap = control.status()["grid_charge_cap"]
    assert cap["rule"] == "conservative" and cap["cap_kw"] < gate.MIN_RATE_KW and cap["main_fuse_a"] == 20.0
    assert cap["learned"]["ready"] is False and cap["learned"]["days_left"] == READY_DAYS
    # The house card's status line: the learning holds the grid charge back once grid charging is allowed.
    assert control.learning_hold(now) is None
    monkeypatch.setattr(BatteryControl, "allowed", lambda self, options=None: frozenset({"grid_charge"}))
    # The days left are those to the first usable days: the cap changes then.
    assert control.learning_hold(now) == {"what": "phase_peaks", "days_left": MIN_READY_DAYS, "cap_kw": 0.0}
    # Counted hours, L3 at most 9 A: the first usable days, read back from the recorder's history.
    hour = int(now.timestamp() // 3600)
    marks = controller.phase_peaks.peaks.hours

    def keep(days: range, *, backfilled: bool = False) -> None:
        # Each of the days before today, its counted minutes spread over twelve of its clock hours.
        for day in days:
            for offset in range(SPREAD_HOURS):
                marks[(hour // 24 - 1 - day) * 24 + offset] = HourMark(
                    house_a=(5.0, 3.0, 9.0), minutes=30, total_all=17.0, total_counted=17.0, backfilled=backfilled
                )

    conservative = cap["cap_kw"]
    keep(range(MIN_READY_DAYS - 1), backfilled=True)
    assert control.status()["grid_charge_cap"]["rule"] == "conservative"
    keep(range(MIN_READY_DAYS), backfilled=True)
    expected = (20.0 - 2.0 - (9.0 + MARGIN_A)) * 3 * volts / 1000
    growing = (20.0 - 2.0 - (9.0 + MARGIN_A + EXTRA_MARGIN_A)) * 3 * volts / 1000
    assert control._grid_charge_cap_kw(now, control.options()) == pytest.approx(growing)
    cap = control.status()["grid_charge_cap"]
    assert cap["rule"] == "learning" and cap["cap_kw"] == pytest.approx(growing, abs=0.01)
    # What it would be without the learning, and once it is done.
    assert cap["cap_conservative_kw"] == conservative and cap["cap_learned_kw"] == pytest.approx(expected, abs=0.01)
    assert cap["learned"]["extra_margin_a"] == EXTRA_MARGIN_A and cap["learned"]["usable"] is True
    assert cap["learned"]["days_from_history"] == MIN_READY_DAYS
    assert cap["learned"]["hours_from_history"] == MIN_READY_DAYS * SPREAD_HOURS
    assert cap["learned"]["days_spread"] == MIN_READY_DAYS and cap["learned"]["days_from_history_dropped"] == 0
    # The house card says so while the cap is below the battery's rating.
    assert control.learning_hold(now) == {
        # The days read back weigh half toward the end of the learning.
        "what": "phase_peaks", "days_left": math.ceil(READY_DAYS - MIN_READY_DAYS * HISTORY_DAY_WEIGHT),
        "cap_kw": pytest.approx(growing, abs=0.01),
    }
    # A house so near the fuse that the further margin costs nothing holds nothing back by its learning.
    kept = dict(marks)
    for mark in marks.values():
        mark.house_a = (5.0, 3.0, 17.0)
    cap = control.status()["grid_charge_cap"]
    assert cap["rule"] == "learning" and cap["cap_kw"] == 0.0 == cap["cap_learned_kw"]
    assert control.learning_hold(now) is None
    for mark in kept.values():
        mark.house_a = (5.0, 3.0, 9.0)
    # Two weeks in all, the later days sampled live: not done yet, the days read back weighing half.
    keep(range(MIN_READY_DAYS, READY_DAYS))
    cap = control.status()["grid_charge_cap"]
    assert cap["rule"] == "learning" and cap["learned"]["days_left"] == math.ceil(MIN_READY_DAYS * HISTORY_DAY_WEIGHT)
    live_days = math.ceil(READY_DAYS - MIN_READY_DAYS * HISTORY_DAY_WEIGHT)
    keep(range(MIN_READY_DAYS, MIN_READY_DAYS + live_days))
    assert control._grid_charge_cap_kw(now, control.options()) == pytest.approx(expected)
    cap = control.status()["grid_charge_cap"]
    assert cap["rule"] == "learned" and cap["cap_kw"] == pytest.approx(expected, abs=0.01)
    assert cap["cap_learned_kw"] == cap["cap_kw"] and cap["learned"]["extra_margin_a"] == 0.0
    assert cap["learned"]["days_from_history"] == MIN_READY_DAYS
    assert cap["learned"]["peaks_a"] == {"L1": 5.0 + MARGIN_A, "L2": 3.0 + MARGIN_A, "L3": 9.0 + MARGIN_A}
    assert control.snapshot()["status"]["grid_charge_cap"]["learned"]["days_counted"] == MIN_READY_DAYS + live_days
    assert control.learning_hold(now) is None


async def test_the_site_samples_its_house_per_phase_without_raising(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    start = dt_util.utcnow().timestamp()
    for second in range(0, 240, 10):
        controller.phase_peaks.sample(controller, start + second)
    assert not controller.phase_peaks._failed


async def test_selling_from_the_battery_is_off_in_self_consumption(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    _mode(hass, site, "self_consumption")
    control = await _started(hass, site, controller, plan_source=lambda _now: ("export_discharge", 4.0, None))
    now = await _looks(control, dt_util.now(), 4)
    assert plant.calls == []
    assert [row["reason"] for row in control.log if row["kind"] == "restricted"] == ["not_verified:export_discharge"]
    _mode(hass, site, "balanced")
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
    monkeypatch.setattr(integration, "SITE_STOP_HAND_BACK_S", 0.05)
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


async def test_removing_the_site_is_not_held_by_a_plant_that_never_answers(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    await _looks(control, dt_util.now(), 6)
    assert hass.states.get(DISCHARGE).state == "0"
    key = store_key(site.entry_id)
    plant.hangs = True
    monkeypatch.setattr(control_module, "CALL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(control_module, "HAND_BACK_DEADLINE_S", 0.2)
    async with asyncio.timeout(5):
        await BatteryControl.async_remove_stored(hass, site.entry_id, dict(site.data))
    assert hass_storage[key]["data"]["state"]["snapshot"], "kept while the plant could not be handed back"


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
    _mode(hass, site, "max_profit")
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
    assert await control._async_write(op, control.executor, allowed=False) == "refused"
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
    # The charge limit at 0, then mode 4.
    assert calls == [(0.0, None), (None, "Command Charging (PV First)")]
    assert plant.calls[0][2]["entity_id"] == CHARGE and hass.states.get(DISCHARGE).state == "23.04"
    assert control.executor.state.variant == "pv_first"

    again = BatteryControl(hass, controller, plan_source=_hold)
    plant.calls.clear()
    await again.async_start()
    # The mode first (back to Maximum Self Consumption), then Remote EMS off.
    assert plant.calls[0][2].get("option") == "Maximum Self Consumption"
    assert hass.states.get(SWITCH).state == "off" and again.executor.state.phase == "idle"


async def _forced_hold_site(hass, monkeypatch, hass_storage, variants):
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=False)
    control._enabled_seen = True
    monkeypatch.setattr(BatteryControl, "_async_maybe_verify", lambda self, now, options: asyncio.sleep(0))
    if variants is not None:
        control.reports[control.firmware_key()] = {"modes": {"hold": {
            "outcome": variants[-1]["outcome"], "variant": variants[-1]["variant"], "variants": variants,
        }}}
    return control, plant


async def test_a_forced_test_hold_runs_the_first_variant_not_recorded_as_failed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, _plant = await _forced_hold_site(
        hass, monkeypatch, hass_storage, [{"variant": "msc_no_discharge", "outcome": "ignored"}]
    )
    await control.async_trial("hold", None, 5, force=True)
    await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    assert control.executor.control.variant == "pv_first"
    assert hass.states.get(MODE).state == "Command Charging (PV First)" and hass.states.get(CHARGE).state == "0"
    assert hass.states.get(DISCHARGE).state == "23.04"


async def test_a_forced_test_hold_is_refused_when_every_variant_failed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, plant = await _forced_hold_site(hass, monkeypatch, hass_storage, [
        {"variant": "msc_no_discharge", "outcome": "ignored"},
        {"variant": "pv_first", "outcome": "aborted:not_obeyed:hold:charging"},
    ])
    with pytest.raises(TrialRefused) as refused:
        await control.async_trial("hold", None, 5, force=True)
    assert refused.value.code == "hold_variants_failed" and plant.calls == []
    # Another test action still runs.
    await control.async_trial("grid_charge", 2.0, 5, force=True)
    assert control.trial is not None


async def test_without_a_passed_variant_the_executor_runs_the_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    control, _plant = await _forced_hold_site(hass, monkeypatch, hass_storage, None)
    executor = control._executor()
    assert executor.use_variant("pv_first")  # left from an earlier verification
    await control.async_trial("hold", None, 5, force=True)
    await _looks(control, dt_util.now() + timedelta(seconds=10), 4)
    assert control.executor.control.variant == "msc_no_discharge"
    assert hass.states.get(DISCHARGE).state == "0" and hass.states.get(MODE).state == "Maximum Self Consumption"


async def test_the_shadow_shows_the_hold_variant_the_verification_passed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=True)
    _passed(control)
    control.reports[control.firmware_key()]["modes"]["hold"] |= {
        "variant": "pv_first",
        "variants": [{"variant": "msc_no_discharge", "outcome": "ignored"}, {"variant": "pv_first", "outcome": "obeyed"}],
    }
    assert control.hold_variant() == "pv_first" and controller.battery_shadow.hold_variant() == "pv_first"
    now = dt_util.now()
    for index in range(3):
        await control.async_look(now + timedelta(seconds=10 * index))
    assert plant.calls == []
    would = [row for row in control.log if row["kind"] == "would_do"]
    assert [(write["role"], write["value"]) for write in would[0]["writes"]] == [
        ("charge_limit", 0.0), ("remote_ems", True), ("control_mode", "Command Charging (PV First)")
    ]
    assert controller.battery_shadow.inputs(now).variant == "pv_first"


async def test_a_stored_allow_sell_becomes_the_max_profit_mode_once_and_a_chosen_mode_is_left_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    _options(hass, site, allow_sell=True)
    assert gate.CONF_MODE not in site.data
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    assert site.data[gate.CONF_MODE] == "max_profit" and control.options().mode == "max_profit"
    assert gate.CONF_ALLOW_SELL not in site.data, "the legacy option is removed once migrated"
    await control.async_update_options({gate.CONF_MODE: "balanced"})
    again = BatteryControl(hass, controller, plan_source=_hold)
    await again.async_start()
    assert site.data[gate.CONF_MODE] == "balanced", "migrated once; the legacy option is never read again"
    assert again.options().mode == "balanced" and again.options().allow_sell, "Balanced sells through its gate"
    assert "battery_mode" in control.status()["options"]
    assert gate.CONF_ALLOW_SELL not in control.status()["options"]


async def test_a_stored_allow_sell_with_a_chosen_mode_is_removed_and_the_mode_kept(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    _options(hass, site, allow_sell=True)
    _mode(hass, site, "balanced")
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    assert site.data[gate.CONF_MODE] == "balanced" and gate.CONF_ALLOW_SELL not in site.data


async def test_balanced_and_max_profit_let_control_run_the_selling_action(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, _plant = await _site(hass, monkeypatch, hass_storage)
    control = await _started(hass, site, controller, plan_source=_hold)
    for mode, sells in (("self_consumption", False), ("balanced", True), ("max_profit", True)):
        await control.async_update_options({gate.CONF_MODE: mode})
        assert ("export_discharge" in control.status()["allowed_actions"]) is sells


async def test_the_status_names_the_device_that_holds_a_disabled_control(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    """The card opens the battery's device page from the checklist: the device of the first control to enable."""
    _site_entry, controller, _plant_calls = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=lambda _now: None)
    await control.async_start()
    assert control.status()["device_id"] is None
    registry = er.async_get(hass)
    registry.async_update_entity(SWITCH, disabled_by=er.RegistryEntryDisabler.USER)
    status = control.status()
    assert any(need.startswith("control_disabled:") for need in status["needs"])
    assert status["device_id"] == registry.async_get(SWITCH).device_id is not None


async def test_a_hardware_test_runs_under_shadow_only_in_minutes_and_leaves_the_routine_s_report_alone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    assert control.hardware_test_refusal() == "not_enabled"
    assert await control.async_hardware_test_start() == "not_enabled"
    _options(hass, site, enabled=True, shadow_only=True)
    control.facts = control._read_facts()
    assert control.hardware_test_refusal() is None
    assert await control.async_hardware_test_start() is None
    assert control.hardware_test_progress()["running"] and control.hardware_test_progress()["mode"] == "hold"
    assert control.state() == "shadow_only"
    started = dt_util.now()
    now = started
    while control.hardware_test_progress()["running"] and now < started + timedelta(minutes=15):
        now = await _looks(control, now, 1)
    progress = control.hardware_test_progress()
    assert not progress["running"] and now - started <= timedelta(minutes=10)
    assert {mode: row["outcome"] for mode, row in progress["report"]["modes"].items()} == {
        "hold": "obeyed", "charge": "obeyed", "discharge_house": "obeyed", "normal": "restored",
    }
    assert control.reports == {}, "the test never counts as the verification routine"
    assert control.state() == "shadow_only"
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"


async def test_a_hardware_test_stopped_hands_the_plant_back_at_once(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller, plan_source=_hold)
    await control.async_start()
    _options(hass, site, enabled=True, shadow_only=True)
    control.facts = control._read_facts()
    assert await control.async_hardware_test_start() is None
    await _looks(control, dt_util.now() + timedelta(seconds=10), 3)
    assert hass.states.get(SWITCH).state == "on"
    await control.async_hardware_test_stop("hardware_test_deadline")
    assert not control.hardware_test_progress()["running"]
    assert hass.states.get(SWITCH).state == "off" and hass.states.get(DISCHARGE).state == "23.04"
    assert control.hardware_report is not None and control.hardware_report["completed"] is False


async def test_a_field_tested_recipe_or_a_beta_tester_leaves_shadow_only_off_by_default(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, hass_storage: dict[str, Any]
) -> None:
    site, controller, plant = await _site(hass, monkeypatch, hass_storage)
    control = BatteryControl(hass, controller)
    await control.async_start()
    control.facts = control._read_facts()
    # Not yet for Sigenergy (its on-site tests are rerun first): shadow only stays on by default.
    assert control.options().shadow_only is True
    monkeypatch.setattr(control_module, "recipe_field_tested", lambda brand: brand == "sigenergy")
    assert control.options().shadow_only is False
    _options(hass, site, shadow_only=True)
    assert control.options().shadow_only is True, "a stored value is kept"
    hass.config_entries.async_update_entry(
        site, data={key: value for key, value in site.data.items() if key != gate.CONF_SHADOW_ONLY}
    )
    monkeypatch.setattr(control_module, "recipe_field_tested", lambda _brand: False)
    assert control.options().shadow_only is True
    monkeypatch.setattr(control_module, "learning_gate_open", lambda _hass, _site: True)
    assert control.options().shadow_only is False, "a beta tester's site"
