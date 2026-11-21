"""Sigenergy's AC charger (EVAC) as a SpotNav charger: an ordinary amp-controlled charger on the `sigen` platform,
stored as `sigen_ac` (`charger_shapes.SHAPES["sigen_ac"]`, from the TypQxQ "Sigenergy ESS" 2.0.4b2 source).

Detection tells it from the DC charger and the plant's other devices on the same platform, Start and Stop press its
buttons, the current goes to its output current number from 6 A, and whether it charges is read from its system
state, since the start/stop register cannot be read back.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service, MockConfigEntry

from custom_components.spotnav.const import CURRENT_CONTROL_NUMBER, CURRENT_CONTROL_POWER
from custom_components.spotnav.execution.charger_connection import (
    CHARGING,
    CONNECTED,
    DISCONNECTED,
    ERROR,
    normalise_status,
    PAUSED,
    UNKNOWN,
)
from custom_components.spotnav.execution.charger_profiles import device_profile, policy_for, profile_for
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_BELOW_MINIMUM,
    ASSIGN_RATE_LIMITED,
    WRITE_REGULATOR,
    WRITE_SESSION_START,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.site.detected_hass import _has_charger
from custom_components.spotnav.vehicles.discovery import _power_controlled_device

from ..charger_helpers import adapter_for, Clock
from ..charger_shapes import register_shape, SHAPES, Shape

START = "button.sigen_ac_charger_start"
STOP = "button.sigen_ac_charger_stop"
CURRENT = "number.sigen_ac_charger_output_current"
STATE = "sensor.sigen_ac_charger_system_state"
NUMBER_ATTRS = {"unit_of_measurement": "A", "min": 6, "max": 32, "step": 1}


def _found(hass: HomeAssistant):
    ids = register_shape(hass, SHAPES["sigen_ac"])
    found = detect_charger(hass, ids["device_id"])
    assert found is not None
    # The person enabled the number when the flow offered it.
    hass.states.async_set(CURRENT, "16", NUMBER_ATTRS)
    return ids, found


def _add_device(hass: HomeAssistant, shape: Shape, entry: MockConfigEntry, name: str) -> str:
    """One more device of the plant's config entry, with a shape's entities (enabled), entity ids `sigen_<key>`."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("sigen", shape.serial)}, name=name
    )
    registry = er.async_get(hass)
    for entity in shape.entities:
        created = registry.async_get_or_create(
            entity.domain,
            "sigen",
            entity.unique_id,
            suggested_object_id=f"sigen_{entity.key}",
            config_entry=entry,
            device_id=device.id,
            translation_key=entity.key,
            original_device_class=entity.attrs.get("device_class"),
            capabilities={"state_class": entity.attrs["state_class"]} if "state_class" in entity.attrs else None,
            unit_of_measurement=entity.attrs.get("unit_of_measurement"),
        )
        hass.states.async_set(created.entity_id, entity.state, entity.attrs)
    return device.id


# -- detection --------------------------------------------------------------------------------------


async def test_the_ac_charger_is_found_as_an_amp_charger_stored_as_sigen_ac(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)

    assert found.platform == "sigen_ac" and found.role == "charger"
    assert found.control_path == {"kind": "buttons", "start_entity_id": START, "stop_entity_id": STOP}
    assert found.current_limit == CURRENT and found.current_control == CURRENT_CONTROL_NUMBER
    assert found.charging_state == {"entity_id": STATE, "charging_values": ["charging"]}
    # Disabled by default in the integration: offered to be enabled.
    assert CURRENT in found.disabled_useful
    profile = profile_for("sigen_ac")
    assert profile is not None and profile.platform == "sigen" and not profile.power_controlled
    assert policy_for("sigen_ac").zero_pauses and policy_for("sigen_ac").min_interval_s == 10.0


async def test_a_plant_with_a_dc_and_an_ac_charger_offers_each_as_its_own(hass: HomeAssistant) -> None:
    """One config entry, three devices: the DC charger, the AC charger and the inverter. Each charger is detected as its
    own kind, and the inverter as none."""
    dc_ids = register_shape(hass, SHAPES["sigen"])
    entry = hass.config_entries.async_get_entry("sigen")
    ac_device = _add_device(hass, SHAPES["sigen_ac"], entry, "Sigen AC Charger")
    inverter = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("sigen", "inverter_1")}, name="Sigen Inverter"
    )
    er.async_get(hass).async_get_or_create(
        "sensor", "sigen", "sig1_inverter_1_ess_battery_soc", config_entry=entry, device_id=inverter.id,
        translation_key="inverter_ess_battery_soc",
    )

    dc = detect_charger(hass, dc_ids["device_id"])
    ac = detect_charger(hass, ac_device)
    other = detect_charger(hass, inverter.id)

    assert dc is not None and dc.platform == "sigen" and dc.current_control == CURRENT_CONTROL_POWER
    assert dc.control_path["start_entity_id"] == "button.sigen_dc_charger_start"
    assert ac is not None and ac.platform == "sigen_ac" and ac.current_control == CURRENT_CONTROL_NUMBER
    assert ac.control_path["start_entity_id"] == "button.sigen_ac_charger_start"
    assert ac.current_limit == "number.sigen_ac_charger_output_current"
    assert other is not None and other.role is None and "not_a_charger_device" in other.notes

    registry = er.async_get(hass)
    assert device_profile("sigen", er.async_entries_for_device(registry, ac_device)) is profile_for("sigen_ac")
    assert device_profile("sigen", er.async_entries_for_device(registry, dc_ids["device_id"])) is profile_for("sigen")
    # Only the DC charger's own current sensors are on the car's battery side.
    assert _power_controlled_device(hass, dc_ids["device_id"]) is True
    assert _power_controlled_device(hass, ac_device) is False


async def test_a_plant_with_only_an_ac_charger_has_a_charger(hass: HomeAssistant) -> None:
    plant = MockConfigEntry(domain="sigen", entry_id="plant_only")
    plant.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", "sigen", "plant_only_plant_battery_soc", config_entry=plant, translation_key="plant_ess_soc"
    )
    assert _has_charger(er.async_get(hass), plant) is False

    register_shape(hass, SHAPES["sigen_ac"])
    assert _has_charger(er.async_get(hass), hass.config_entries.async_get_entry("sigen")) is True


# -- the system state -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "connection"),
    [
        ("initializing", UNKNOWN),
        ("not_connected", DISCONNECTED),
        ("reserving", CONNECTED),
        ("preparing", PAUSED),
        ("ev_ready", PAUSED),
        ("charging", CHARGING),
        ("fault", ERROR),
        ("error", ERROR),
    ],
)
def test_the_connection_comes_from_the_system_state(state: str, connection: str) -> None:
    assert normalise_status("sigen_ac", state) == connection


async def test_whether_it_charges_is_read_from_the_system_state(hass: HomeAssistant) -> None:
    """The start/stop register is write-only: the buttons say nothing, the system state does."""
    _ids, found = _found(hass)
    adapter = adapter_for(hass, found)

    assert adapter.charging_state() is True
    hass.states.async_set(STATE, "ev_ready")
    assert adapter.charging_state() is False
    hass.states.async_set(STATE, "preparing")
    assert adapter.charging_state() is False
    assert adapter.progress_status() == "SuspendedEV"
    adapter.cancel_pending_reads()


# -- Start, Stop and the current --------------------------------------------------------------------


async def test_start_and_stop_press_its_buttons(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    presses = async_mock_service(hass, "button", "press")
    async_mock_service(hass, "number", "set_value")
    adapter = adapter_for(hass, found)

    assert await adapter.async_start() is True
    assert await adapter.async_stop() is True

    assert [call.data["entity_id"] for call in presses] == [START, STOP]
    adapter.cancel_pending_reads()


async def test_the_current_is_written_to_its_number_from_6_a_at_most_every_10_s(hass: HomeAssistant) -> None:
    _ids, found = _found(hass)
    writes: list[Any] = []

    async def set_value(call) -> None:
        writes.append(call)
        hass.states.async_set(CURRENT, str(call.data["value"]), NUMBER_ATTRS)

    hass.services.async_register("number", "set_value", set_value)
    clock = Clock()
    adapter = adapter_for(hass, found, clock=clock)

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    clock.advance(5)
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_RATE_LIMITED
    clock.advance(6)
    assert await adapter.async_set_current(12, reason=WRITE_REGULATOR) == ASSIGN_ASSIGNED
    clock.advance(11)
    # The number refuses less than 6 A: a pause is the Stop button, never a write.
    assert await adapter.async_set_current(5, reason=WRITE_REGULATOR) == ASSIGN_BELOW_MINIMUM

    assert [(call.data["entity_id"], call.data["value"]) for call in writes] == [(CURRENT, 10), (CURRENT, 12)]
    adapter.cancel_pending_reads()
