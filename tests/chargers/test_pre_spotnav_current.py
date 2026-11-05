"""What a charger carried before SpotNav first wrote its current (`PreSpotnavCurrent`): read once, through the
current path's own read-back, just before the first command that writes a current goes out, and never again.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.const import (
    CONF_CHARGE_CONTROL,
    CONF_CURRENT_CONTROL,
    CONF_CURRENT_LIMIT,
    CONF_MODE,
    CONF_OCPP_CHARGE_POINT_ID,
    CONF_OCPP_CONNECTOR_ID,
    CURRENT_CONTROL_CHANGE_CONFIGURATION,
    CURRENT_CONTROL_NUMBER,
    MODE_GENERIC,
    MODE_OCPP,
)
from custom_components.spotnav.execution.chargers.adapter import build_adapter, ChargerAdapter, PreSpotnavCurrent
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_TARGET_UNAVAILABLE,
    WRITE_REGULATOR,
    WRITE_SESSION_START,
    WRITE_SOLAR,
)
from custom_components.spotnav.flows.charger_detection import detect_charger
from custom_components.spotnav.vehicles.ocpp_identity import OcppConnectorTarget

from ..charger_helpers import adapter_for, Clock, enable_easee_limit_sensor, mock_ocpp_assigned_current
from ..charger_shapes import register_shape, SHAPES

SWITCH = "switch.charger"
NUMBER = "number.charger_limit"
ATTRS = {"unit_of_measurement": "A", "min": 0, "max": 32, "step": 1}


def _recording(adapter: ChargerAdapter) -> list[PreSpotnavCurrent]:
    """Give the adapter a new charger's record (nothing written yet) and collect each save it asks for."""
    saves: list[PreSpotnavCurrent] = []

    async def saved() -> None:
        assert adapter.pre_current is not None
        saves.append(adapter.pre_current)

    adapter.pre_current = PreSpotnavCurrent()
    adapter.pre_current_recorded = saved
    return saves


def _number_adapter(hass: HomeAssistant, amps: str, *, clock: Clock | None = None) -> ChargerAdapter:
    hass.states.async_set(SWITCH, "on")
    hass.states.async_set(NUMBER, amps, ATTRS)
    config = {
        CONF_MODE: MODE_GENERIC,
        CONF_CHARGE_CONTROL: SWITCH,
        CONF_CURRENT_LIMIT: NUMBER,
        CONF_CURRENT_CONTROL: CURRENT_CONTROL_NUMBER,
    }
    return build_adapter(hass, config, ocpp_target=lambda: None, energy_entity_id=None, now=clock or Clock())


def _number_follows(hass: HomeAssistant) -> list[ServiceCall]:
    """`number.set_value` that the number's state follows, as a charger's does."""
    calls: list[ServiceCall] = []

    async def set_value(call: ServiceCall) -> None:
        calls.append(call)
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]), ATTRS)

    hass.services.async_register("number", "set_value", set_value)
    return calls


async def test_the_number_is_read_once_before_the_first_write_and_never_again(hass: HomeAssistant) -> None:
    clock = Clock()
    adapter = _number_adapter(hass, "10", clock=clock)
    calls = _number_follows(hass)
    saves = _recording(adapter)

    assert await adapter.async_set_current(14, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=10)
    assert saves == [PreSpotnavCurrent(recorded=True, amps=10)]

    for amps, reason in ((8, WRITE_REGULATOR), (16, WRITE_SOLAR), (12, WRITE_SESSION_START)):
        clock.advance(120)
        await adapter.async_set_current(amps, reason=reason)
    assert [call.data["value"] for call in calls] == [14, 8, 16, 12]
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=10), "never overwritten"
    assert len(saves) == 1


async def test_a_write_that_sends_nothing_records_nothing_yet(hass: HomeAssistant) -> None:
    """A write refused before anything went out leaves the charger as the person had it: read at the next one."""
    clock = Clock()
    adapter = _number_adapter(hass, "10", clock=clock)
    _number_follows(hass)
    saves = _recording(adapter)
    hass.states.async_set(NUMBER, "unavailable", ATTRS)

    assert await adapter.async_set_current(14, reason=WRITE_SESSION_START) == ASSIGN_TARGET_UNAVAILABLE
    assert adapter.pre_current == PreSpotnavCurrent() and saves == []

    hass.states.async_set(NUMBER, "11", ATTRS)
    clock.advance(120)
    assert await adapter.async_set_current(14, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=11)


@pytest.mark.parametrize("reading", ["0", "3", "not a number"])
async def test_a_reading_that_cannot_be_trusted_is_recorded_as_unknown_and_never_filled_in(
    hass: HomeAssistant, reading: str
) -> None:
    clock = Clock()
    adapter = _number_adapter(hass, reading, clock=clock)
    _number_follows(hass)
    saves = _recording(adapter)

    assert await adapter.async_set_current(14, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=None)
    assert saves == [PreSpotnavCurrent(recorded=True, amps=None)]

    clock.advance(120)
    await adapter.async_set_current(10, reason=WRITE_SESSION_START)
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=None), "what SpotNav wrote is not the person's"


async def test_a_number_whose_state_is_not_what_the_charger_applies_is_unknown(hass: HomeAssistant) -> None:
    """OpenEVSE's number reads its stored maximum, not the claim a write makes: nothing to give back."""
    ids = register_shape(hass, SHAPES["openevse"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    async_mock_service(hass, "number", "set_value")
    _recording(adapter)

    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=None)


async def test_an_adapter_without_a_record_reads_nothing(hass: HomeAssistant) -> None:
    """The hand-back's own adapter keeps no record: its write is never taken for the person's."""
    adapter = _number_adapter(hass, "10")
    _number_follows(hass)
    assert adapter.pre_current is None
    assert await adapter.async_set_current(14, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current is None


async def test_ocpp_reads_the_connectors_assigned_current_before_the_first_write(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.cp_charge_control", "on")
    config = {
        CONF_MODE: MODE_OCPP,
        CONF_CHARGE_CONTROL: "switch.cp_charge_control",
        CONF_CURRENT_LIMIT: "",
        CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CONF_OCPP_CHARGE_POINT_ID: "cp",
        CONF_OCPP_CONNECTOR_ID: 2,
    }
    adapter = build_adapter(hass, config, ocpp_target=lambda: OcppConnectorTarget("cp", 2), energy_entity_id=None)
    configured = mock_ocpp_assigned_current(hass, "1.32,2.16")
    _recording(adapter)

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=16)
    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [call.data["value"] for call in configured] == ["1.32,2.10", "1.32,2.12"]
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=16)


async def test_ocpp_without_an_entry_for_the_connector_is_unknown(hass: HomeAssistant) -> None:
    hass.states.async_set("switch.cp_charge_control", "on")
    config = {
        CONF_MODE: MODE_OCPP,
        CONF_CHARGE_CONTROL: "switch.cp_charge_control",
        CONF_CURRENT_LIMIT: "",
        CONF_CURRENT_CONTROL: CURRENT_CONTROL_CHANGE_CONFIGURATION,
        CONF_OCPP_CHARGE_POINT_ID: "cp",
        CONF_OCPP_CONNECTOR_ID: 2,
    }
    adapter = build_adapter(hass, config, ocpp_target=lambda: OcppConnectorTarget("cp", 2), energy_entity_id=None)
    mock_ocpp_assigned_current(hass, "1.32")
    _recording(adapter)

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=None)


async def _easee(hass: HomeAssistant, limit: str | None) -> ChargerAdapter:
    ids = register_shape(hass, SHAPES["easee"])
    if limit is not None:
        enable_easee_limit_sensor(hass, limit)
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    async_mock_service(hass, "easee", "set_charger_dynamic_limit")
    async_mock_service(hass, "easee", "action_command")
    return adapter


async def test_easee_reads_its_dynamic_limit_before_the_first_write(hass: HomeAssistant) -> None:
    adapter = await _easee(hass, "20")
    _recording(adapter)

    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=20)


async def test_easee_reads_its_dynamic_limit_before_its_first_pause(hass: HomeAssistant) -> None:
    """Easee's pause limits the dynamic current to 0 A: the limit is read before that command too."""
    adapter = await _easee(hass, "20")
    _recording(adapter)

    assert await adapter.async_stop() is True
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=20)


async def test_easee_without_its_limit_sensor_is_unknown(hass: HomeAssistant) -> None:
    """The limit sensor is disabled by default: nothing is read from the cloud in its place."""
    adapter = await _easee(hass, None)
    _recording(adapter)

    assert await adapter.async_set_current(12, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert adapter.pre_current == PreSpotnavCurrent(recorded=True, amps=None)


async def test_a_plain_switch_stop_is_not_a_current_write(hass: HomeAssistant) -> None:
    adapter = _number_adapter(hass, "10")
    async_mock_service(hass, "switch", "turn_off")
    _recording(adapter)

    assert await adapter.async_stop() is True
    assert adapter.pre_current == PreSpotnavCurrent(), "still to be read at the first current write"


@pytest.mark.parametrize(
    ("raw", "record_exists", "expected"),
    [
        (None, False, PreSpotnavCurrent()),
        (None, True, PreSpotnavCurrent(recorded=True, amps=None)),
        ({"recorded": True, "amps": 16}, True, PreSpotnavCurrent(recorded=True, amps=16)),
        ({"recorded": True, "amps": None}, True, PreSpotnavCurrent(recorded=True, amps=None)),
        ({"recorded": False, "amps": None}, True, PreSpotnavCurrent()),
        ({"recorded": True, "amps": 3}, True, PreSpotnavCurrent(recorded=True, amps=None)),
        ({"recorded": True, "amps": "16"}, True, PreSpotnavCurrent(recorded=True, amps=None)),
        ("16", True, PreSpotnavCurrent(recorded=True, amps=None)),
    ],
)
def test_the_stored_record(raw: Any, record_exists: bool, expected: PreSpotnavCurrent) -> None:
    """A new charger has no record and still reads; a record from before this was kept counts as unknown, as does
    one that cannot be read."""
    assert PreSpotnavCurrent.from_store(raw, record_exists=record_exists) == expected
    if expected.recorded or raw is not None:
        assert PreSpotnavCurrent.from_store(expected.to_store(), record_exists=True) == expected
