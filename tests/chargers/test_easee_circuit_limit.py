"""Easee's circuit dynamic limit, read only: nordicopen/easee_hass `d05d7d41` (v2026.11.0) `const.py`
`dynamic_circuit_limit` (on the circuit's master charger, disabled by default; attributes `circuit_id` and
`state_dynamicCircuitCurrentP1..P3`). pyeasee `840ab44a` (v0.8.23) names it the circuit's maximum current, beside the
charger's own dynamic limit, which is what SpotNav sets: a circuit limit below it caps the charger."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.spotnav.const import CURRENT_CONTROL_EASEE
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for
from ..charger_shapes import _e, register_shape, Shape, SHAPES

pytestmark = pytest.mark.usefixtures("offline_relay")

LIMIT = "sensor.easee_dynamic_circuit_limit"


def _circuit(circuit: Any, phases: tuple[float, float, float]) -> dict[str, Any]:
    return {
        "circuit_id": circuit,
        "circuit_ratedCurrent": 25.0,
        **{f"state_dynamicCircuitCurrentP{n}": value for n, value in zip((1, 2, 3), phases, strict=True)},
    }


def _easee(
    *, circuit: Any = 41, limit_circuit: Any = 41, phases: tuple[float, float, float] = (10.0, 10.0, 10.0),
    disabled: bool = False,
) -> Shape:
    shape = SHAPES["easee"]
    entities = tuple(
        replace(entity, attrs={**entity.attrs, "circuit_id": circuit})
        if entity.key == "status"
        else replace(entity, disabled=False) if entity.key == "dynamic_charger_limit" else entity
        for entity in shape.entities
    )
    limit = _e(
        "sensor", "EH123_dynamic_circuit_limit", "dynamic_circuit_limit", str(max(phases)),
        _circuit(limit_circuit, phases), translation_key=True, disabled=disabled,
    )
    return replace(shape, entities=(*entities, limit))


def _adapter(hass: HomeAssistant, shape: Shape):
    ids = register_shape(hass, shape)
    return adapter_for(hass, detect_charger(hass, ids["device_id"]), current_control=CURRENT_CONTROL_EASEE)


async def test_a_circuit_limit_below_the_chargers_caps_it_and_is_told(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _easee(phases=(10.0, 12.0, 10.0)))
    assert adapter.current.setpoint_a() == 16
    assert adapter.circuit_limit_a() == 10.0
    assert adapter.current.describe_state()["circuit_limit"] == {"entity_id": LIMIT, "phases_a": [10.0, 12.0, 10.0]}


async def test_a_circuit_limit_above_the_chargers_caps_nothing_but_is_still_in_the_diagnostics(
    hass: HomeAssistant,
) -> None:
    adapter = _adapter(hass, _easee(phases=(25.0, 25.0, 25.0)))
    assert adapter.circuit_limit_a() is None
    assert adapter.current.describe_state()["circuit_limit"]["phases_a"] == [25.0, 25.0, 25.0]


async def test_another_circuits_limit_or_a_disabled_sensor_is_none(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _easee(limit_circuit=42))
    assert adapter.circuit_limit_a() is None
    assert adapter.current.describe_state()["circuit_limit"] is None


async def test_a_disabled_circuit_limit_sensor_is_none(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _easee(disabled=True))
    assert adapter.circuit_limit_a() is None


async def test_a_circuit_limit_that_reads_nothing_is_none(hass: HomeAssistant) -> None:
    adapter = _adapter(hass, _easee())
    hass.states.async_set(LIMIT, "unavailable", {"circuit_id": 41})
    assert adapter.circuit_limit_a() is None


def test_an_app_that_does_not_read_the_circuit_limit_gets_neither_the_line_nor_its_tone() -> None:
    from custom_components.spotnav.api.webhook import _status_block_for_app

    line = {"code": "charger_circuit_limited", "params": {"limit_a": 10.0}}
    charging = {"code": "charging_now", "params": {"until": None}}
    block = {"tone": "notice", "lines": [charging, line]}
    assert _status_block_for_app(block, set()) == {"tone": "normal", "lines": [charging]}
    assert _status_block_for_app(block, {"circuit_limited"}) == block
