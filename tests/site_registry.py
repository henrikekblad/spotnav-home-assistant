"""Materialize a recorded `Registry` (`tests/test_site_detection.py`) into Home Assistant's real
entity and device registries, so detection, the entity-config API and the config flow can be driven
through Home Assistant itself."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .test_site_detection import Registry


def materialize(hass: HomeAssistant, registry: Registry, *, states: dict[str, str] | None = None) -> dict[str, str]:
    """Create every device and entity; give each enabled entity a state (default `"0"`).

    Returns `{object_id: entity_id}`. A disabled entity has a registry entry and no state, as in
    Home Assistant.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    config_entries: dict[str, MockConfigEntry] = {}

    def entry_for(platform: str, entry_id: str) -> MockConfigEntry:
        key = f"{platform}:{entry_id}"
        if key not in config_entries:
            config_entries[key] = MockConfigEntry(domain=platform, entry_id=f"{platform}_{entry_id}", title=platform)
            config_entries[key].add_to_hass(hass)
        return config_entries[key]

    device_ids: dict[str, str] = {}
    platform_of_entry = {entity.config_entry_id: entity.platform for entity in registry.entities}
    for device in registry.devices:
        entry_key = device.config_entry_ids[0] if device.config_entry_ids else "entry"
        platform = platform_of_entry.get(entry_key, "test")
        created = devices.async_get_or_create(
            config_entry_id=entry_for(platform, entry_key).entry_id,
            identifiers={(platform, device.id)},
            manufacturer=device.manufacturer,
            model=device.model,
            name=device.name,
        )
        device_ids[device.id] = created.id

    made: dict[str, str] = {}
    for entity in registry.entities:
        disabled = {"integration": RegistryEntryDisabler.INTEGRATION, "user": RegistryEntryDisabler.USER}.get(
            entity.disabled_by or ""
        )
        entry = entities.async_get_or_create(
            "sensor",
            entity.platform,
            entity.unique_id,
            suggested_object_id=entity.object_id,
            config_entry=entry_for(entity.platform, entity.config_entry_id or "entry"),
            device_id=device_ids.get(entity.device_id or ""),
            disabled_by=disabled,
            original_device_class=entity.device_class,
            unit_of_measurement=entity.unit,
            translation_key=entity.translation_key,
            original_name=entity.original_name,
        )
        made[entity.object_id] = entry.entity_id
        if disabled is None:
            attrs = {}
            if entity.unit:
                attrs["unit_of_measurement"] = entity.unit
            if entity.device_class:
                attrs["device_class"] = entity.device_class
            hass.states.async_set(entry.entity_id, (states or {}).get(entity.object_id, "0"), attrs)
    return made
