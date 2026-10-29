"""Easee's phase mode as a start-time-only `PhaseSwitch`, from the shape of nordicopen/easee_hass 0.9.74.

`easee.set_charger_phase_mode` takes the charger's `device_id` and `phase_mode` (`1_phase`, `auto_phase`, `3_phase`).
The read-back is the `phase_mode` sensor (unique id `<id>_phase_mode`, translation key `phase_mode`, disabled by
default; `single`, `auto` or `three`), else the status sensor's `config_phaseMode` attribute (1, 2, 3), which the
mandatory status sensor always carries along with `circuit_id`. Easee applies a phase mode only at a pause and the
setting reaches the whole circuit: the capability exists only for a charger alone on its circuit, it is never sent
while the charger charges, and auto is never written.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import CURRENT_CONTROL_EASEE
from custom_components.spotnav.execution.chargers.base import (
    PHASES_INVALID,
    PHASES_TARGET_UNAVAILABLE,
    PHASES_UNSUPPORTED,
    PHASES_WHILE_CHARGING,
    PHASES_WRITE_FAILED,
    PhaseSwitchResult,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for
from ..charger_shapes import _e, register_shape, Shape, SHAPES

pytestmark = pytest.mark.usefixtures("offline_relay")

STATUS = "sensor.easee_status"
PHASE_MODE = "sensor.easee_phase_mode"


def _status_attributes(circuit: Any = 41, phase_mode: int = 2) -> dict[str, Any]:
    return {
        "config_authorizationRequired": False,
        "config_phaseMode": phase_mode,
        "state_outputPhase": 30,
        "site_id": 7,
        "circuit_id": circuit,
        "circuit_ratedCurrent": 25.0,
    }


def _easee(status: str = "awaiting_start", circuit: Any = 41, phase_mode: int = 2) -> Shape:
    shape = SHAPES["easee"]
    entities = tuple(
        replace(entity, state=status, attrs=_status_attributes(circuit, phase_mode)) if entity.key == "status" else entity
        for entity in shape.entities
    )
    phase_mode_sensor = _e("sensor", "EH123_phase_mode", "phase_mode", "auto", translation_key=True, disabled=True)
    return replace(shape, entities=(*entities, phase_mode_sensor))


def _adapter(hass: HomeAssistant, shape: Shape | None = None):
    ids = register_shape(hass, shape or _easee())
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), current_control=CURRENT_CONTROL_EASEE)
    return adapter, ids["device_id"]


def _second_charger(hass: HomeAssistant, circuit: Any) -> None:
    """Another Easee charger of the same account (`EH456`), on `circuit`: its own device and status sensor."""
    entry = MockConfigEntry(domain="easee", entry_id="easee_second")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("easee", "EH456")})
    status = er.async_get(hass).async_get_or_create(
        "sensor",
        "easee",
        "EH456_status",
        suggested_object_id="easee_status_b",
        config_entry=entry,
        device_id=device.id,
        translation_key="easee_status",
    )
    assert status.entity_id == "sensor.easee_status_b"
    hass.states.async_set(status.entity_id, "disconnected", _status_attributes(circuit))


def _serve(hass: HomeAssistant) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def phase_mode(call: ServiceCall) -> None:
        calls.append(dict(call.data))

    hass.services.async_register("easee", "set_charger_phase_mode", phase_mode)
    return calls


async def test_a_charger_alone_on_its_circuit_has_the_start_only_capability(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)

    switch = adapter.phase_switch

    assert switch is not None and switch.start_only is True
    # The phase mode sensor is disabled by default: the read-back is the status sensor's attribute.
    assert switch.entity_id == STATUS


async def test_another_easee_charger_on_the_same_circuit_means_none(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)
    _second_charger(hass, circuit=41)

    assert adapter.phase_switch is None
    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_UNSUPPORTED)


async def test_another_easee_charger_on_another_circuit_is_no_obstacle(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)
    _second_charger(hass, circuit=42)

    assert adapter.phase_switch is not None


@pytest.mark.parametrize("unknown", [None, ""])
async def test_no_circuit_of_its_own_means_none(hass: HomeAssistant, unknown: Any) -> None:
    adapter, _device = _adapter(hass, _easee(circuit=unknown))

    assert adapter.phase_switch is None


async def test_another_charger_whose_circuit_cannot_be_read_means_none(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)
    _second_charger(hass, circuit=42)
    hass.states.async_set("sensor.easee_status_b", "unavailable", {})

    assert adapter.phase_switch is None


async def test_another_charger_with_its_status_sensor_disabled_means_none(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)
    _second_charger(hass, circuit=42)
    er.async_get(hass).async_update_entity("sensor.easee_status_b", disabled_by=er.RegistryEntryDisabler.USER)

    assert adapter.phase_switch is None


async def test_the_read_back_is_the_status_attribute_and_auto_is_unknown(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)
    switch = adapter.phase_switch
    assert switch is not None

    for code, phases in ((1, 1), (3, 3), (2, None)):
        hass.states.async_set(STATUS, "awaiting_start", _status_attributes(phase_mode=code))
        assert switch.phases_now() == phases
    hass.states.async_set(STATUS, "unavailable", {})
    assert switch.phases_now() is None


async def test_the_phase_mode_sensor_is_the_read_back_once_it_is_enabled(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)
    er.async_get(hass).async_update_entity(PHASE_MODE, disabled_by=None)
    hass.states.async_set(STATUS, "awaiting_start", _status_attributes(phase_mode=2))
    switch = adapter.phase_switch
    assert switch is not None and switch.entity_id == PHASE_MODE

    for word, phases in (("single", 1), ("three", 3), ("auto", None)):
        hass.states.async_set(PHASE_MODE, word)
        assert switch.phases_now() == phases
    # The sensor unavailable: the status attribute still says.
    hass.states.async_set(PHASE_MODE, "unavailable")
    hass.states.async_set(STATUS, "awaiting_start", _status_attributes(phase_mode=1))
    assert switch.phases_now() == 1


async def test_a_set_calls_the_service_with_the_chargers_device_never_auto(hass: HomeAssistant) -> None:
    adapter, device_id = _adapter(hass)
    calls = _serve(hass)

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(True)
    assert await adapter.async_set_phases(3) == PhaseSwitchResult(True)

    assert calls == [
        {"device_id": device_id, "phase_mode": "1_phase"},
        {"device_id": device_id, "phase_mode": "3_phase"},
    ]
    log = adapter.command_log()
    assert [(record["kind"], record["result"]) for record in log] == [("phases", "sent"), ("phases", "sent")]
    assert [record["calls"][0]["service"] for record in log] == ["easee.set_charger_phase_mode"] * 2


@pytest.mark.parametrize("status", ["awaiting_start", "ready_to_charge", "completed", "paused"])
async def test_a_paused_or_waiting_charger_can_be_set(hass: HomeAssistant, status: str) -> None:
    adapter, _device = _adapter(hass, _easee(status=status))
    calls = _serve(hass)

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(True)
    assert len(calls) == 1


async def test_never_while_it_charges(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass, _easee(status="charging"))
    calls = _serve(hass)

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_WHILE_CHARGING)
    assert await adapter.async_set_phases(3) == PhaseSwitchResult(False, PHASES_WHILE_CHARGING)
    assert calls == []


async def test_no_device_no_call(hass: HomeAssistant) -> None:
    adapter, device_id = _adapter(hass)
    calls = _serve(hass)
    switch = adapter.phase_switch
    assert switch is not None
    entities = er.async_get(hass)
    for entity in er.async_entries_for_device(entities, device_id, include_disabled_entities=True):
        entities.async_update_entity(entity.entity_id, device_id=None)
    dr.async_get(hass).async_remove_device(device_id)

    assert await switch.async_set_phases(1) == PhaseSwitchResult(False, PHASES_TARGET_UNAVAILABLE)
    assert calls == []


async def test_neither_one_nor_three_is_refused(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)
    calls = _serve(hass)

    assert await adapter.async_set_phases(2) == PhaseSwitchResult(False, PHASES_INVALID)  # type: ignore[arg-type]
    assert calls == []


async def test_a_service_error_is_a_failed_switch(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)

    async def refuse(call: ServiceCall) -> None:
        raise HomeAssistantError("Forbidden")

    hass.services.async_register("easee", "set_charger_phase_mode", refuse)

    assert await adapter.async_set_phases(1) == PhaseSwitchResult(False, PHASES_WRITE_FAILED)
    assert adapter.command_log()[-1]["result"] == "error"


async def test_easee_reports_no_switching_status(hass: HomeAssistant) -> None:
    adapter, _device = _adapter(hass)

    assert adapter.phase_switch is not None and adapter.phase_switch.switching() is False
