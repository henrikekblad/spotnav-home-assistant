"""The fake sauna's door: `on` is open (Saunum's `door_open`, device class door)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import FakeSaunaEntity


class FakeSaunaDoor(FakeSaunaEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_name = "Door"

    def __init__(self, entry, sauna) -> None:
        super().__init__(entry, sauna, "door_open")

    @property
    def is_on(self) -> bool:
        return self.sauna.door_open


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([FakeSaunaDoor(entry, entry.runtime_data)])
