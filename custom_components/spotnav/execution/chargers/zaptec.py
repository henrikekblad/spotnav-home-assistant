"""Zaptec: its limit number sits on the Installation device and caps every charger under it."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .registry import register_installation_check

def single_charger_installation(hass: HomeAssistant, number_entity_id: str) -> bool:
    """Whether the installation a limit number belongs to has exactly one charger.

    Zaptec's number sits on the Installation device and caps every charger under it. The chargers are
    the devices that name it as `via_device`; an installation with none that can be found, or several,
    is not single, so the write is refused rather than lowering someone else's charger.
    """
    entity = er.async_get(hass).async_get(number_entity_id)
    if entity is None or entity.device_id is None:
        return False
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    children = [
        device
        for device in devices.devices
        if device.via_device_id == entity.device_id
        and any(
            candidate.platform == entity.platform
            for candidate in er.async_entries_for_device(registry, device.id)
        )
    ]
    return len(children) == 1


register_installation_check(single_charger_installation)
