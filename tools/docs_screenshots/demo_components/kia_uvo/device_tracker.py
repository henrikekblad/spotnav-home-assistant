"""Each car's tracker, at home."""

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.const import STATE_HOME
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, VEHICLES


class Location(TrackerEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "location"
    _attr_name = "Location"

    def __init__(self, vehicle_id, prefix) -> None:
        self._attr_unique_id = f"{vehicle_id}-location"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vehicle_id)})
        self.entity_id = f"device_tracker.{prefix}_location"

    @property
    def location_name(self) -> str:
        return STATE_HOME


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(Location(vehicle_id, prefix) for vehicle_id, prefix, *_ in VEHICLES)
