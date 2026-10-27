"""An Easee command goes to the charger's device as it is now, not as it was at setup.

Easee's services address a charger by device id, and log rather than raise when that id names no charger.
Removed and added again, the Easee integration gives the charger a new device; the entity ids stay. The
device is therefore resolved when a command is sent, from the registry entry of the charger's configured
entities, falling back to the id stored at setup; when neither names an existing device, the paths are not
ready and nothing is sent.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.spotnav.const import CURRENT_CONTROL_EASEE
from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_TARGET_UNAVAILABLE,
    WRITE_SESSION_START,
)
from custom_components.spotnav.execution.controller import ChargingController
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for, detected_config
from ..charger_shapes import register_shape, SHAPES

pytestmark = pytest.mark.usefixtures("offline_relay")


def _serve(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Easee's two services, recording which device each call named."""
    calls: list[tuple[str, str]] = []

    async def command(call: ServiceCall) -> None:
        calls.append((call.data["device_id"], call.data["action_command"]))

    async def limit(call: ServiceCall) -> None:
        calls.append((call.data["device_id"], f"limit {call.data['current']}"))

    hass.services.async_register("easee", "action_command", command)
    hass.services.async_register("easee", "set_charger_dynamic_limit", limit)
    return calls


def _readd(hass: HomeAssistant, old_device_id: str) -> str:
    """The Easee integration removed and added again: a new config entry and device, the same entity ids."""
    entry = MockConfigEntry(domain="easee", entry_id="easee_again")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    new = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("easee", "EH123-again")})
    entities = er.async_get(hass)
    for entity in er.async_entries_for_device(entities, old_device_id, include_disabled_entities=True):
        entities.async_update_entity(entity.entity_id, device_id=new.id)
    devices.async_remove_device(old_device_id)
    return new.id


def _gone(hass: HomeAssistant, device_id: str) -> None:
    """The Easee integration removed: neither its entities nor its device exist."""
    entities = er.async_get(hass)
    for entity in er.async_entries_for_device(entities, device_id, include_disabled_entities=True):
        entities.async_remove(entity.entity_id)
    dr.async_get(hass).async_remove_device(device_id)


async def test_a_readded_easee_gets_the_commands_at_its_new_device(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), current_control=CURRENT_CONTROL_EASEE)
    calls = _serve(hass)
    new_id = _readd(hass, ids["device_id"])
    assert new_id != ids["device_id"]
    hass.states.async_set("sensor.easee_status", "ready_to_charge")

    assert adapter.ready() and adapter.current_ready()
    assert await adapter.async_start() is True
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert await adapter.async_stop() is True

    assert calls and {device for device, _ in calls} == {new_id}, calls


async def test_the_stored_device_is_used_while_no_entity_names_another(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = _serve(hass)

    assert await adapter.async_stop() is True

    assert calls == [(ids["device_id"], "pause")]


async def test_an_easee_with_no_device_is_not_ready_and_is_sent_nothing(hass: HomeAssistant) -> None:
    ids = register_shape(hass, SHAPES["easee"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]), current_control=CURRENT_CONTROL_EASEE)
    calls = _serve(hass)
    _gone(hass, ids["device_id"])

    assert not adapter.ready() and not adapter.current_ready()
    assert await adapter.async_stop() is False
    assert await adapter.async_start() is False
    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_TARGET_UNAVAILABLE
    assert calls == []


async def test_an_automatic_stop_to_an_easee_with_no_device_is_owed(hass: HomeAssistant) -> None:
    """The controller's not-ready path: the decision is owed, not recorded as done."""
    ids = register_shape(hass, SHAPES["easee"])
    found = detect_charger(hass, ids["device_id"])
    controller = ChargingController(hass, "entry_easee", detected_config(found))
    await controller.async_initialize()
    calls = _serve(hass)
    _gone(hass, ids["device_id"])
    hass.states.async_set("sensor.easee_status", "charging")

    async with controller._lock:  # noqa: SLF001 - an automatic decision's stop, as the plan takes it
        went_out = await controller._automatic_stop_locked("the test's stop")  # noqa: SLF001

    assert went_out is False
    assert calls == []
    assert controller.commands_owed
    await controller.async_shutdown()
