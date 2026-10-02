"""Zaptec: the installation limit is written only for a single-charger installation.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.spotnav.execution.chargers.base import (
    ASSIGN_ASSIGNED,
    ASSIGN_INSTALLATION_SHARED,
    WRITE_SESSION_START,
)
from custom_components.spotnav.flows.charger_detection import detect_charger

from ..charger_helpers import adapter_for
from ..charger_shapes import register_shape, SHAPES


async def test_zaptec_writes_the_installation_limit_only_for_a_single_charger_installation(
    hass: HomeAssistant,
) -> None:
    ids = register_shape(hass, SHAPES["zaptec"])
    adapter = adapter_for(hass, detect_charger(hass, ids["device_id"]))
    calls = async_mock_service(hass, "number", "set_value")

    assert await adapter.async_set_current(10, reason=WRITE_SESSION_START) == ASSIGN_ASSIGNED
    assert [dict(call.data) for call in calls] == [{"entity_id": "number.zaptec_available_current", "value": 10}]
    assert adapter.policy.installation_wide and adapter.policy.min_interval_s == 900.0

    # A second charger under the same installation: the limit would lower it too.
    from homeassistant.helpers import device_registry as dr

    config_entry = hass.config_entries.async_get_entry("zaptec")
    devices = dr.async_get(hass)
    second = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("zaptec", "ZAP2")},
        name="zaptec second",
        via_device_id=ids["installation_device_id"],
    )
    er.async_get(hass).async_get_or_create(
        "switch", "zaptec", "ZAP2_charger_operation_mode", config_entry=config_entry, device_id=second.id
    )
    calls.clear()

    assert await adapter.async_set_current(8, reason=WRITE_SESSION_START) == ASSIGN_INSTALLATION_SHARED
    assert calls == []
    assert adapter.capabilities.set_current is False and adapter.capabilities.regulated_current is False
