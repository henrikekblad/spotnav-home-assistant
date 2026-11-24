"""Not on the real unit: a switch that opens and closes the fake door, to try SpotNav's lock. Opening the door ends
a running session, as the real unit's door alarm does."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .entity import FakeSaunaEntity


class FakeDoorControl(FakeSaunaEntity, SwitchEntity):
    _attr_name = "Door open (fake control)"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:door"

    def __init__(self, entry, sauna) -> None:
        super().__init__(entry, sauna, "fake_door_control")

    @property
    def is_on(self) -> bool:
        return self.sauna.door_open

    async def async_turn_on(self, **kwargs) -> None:
        self.sauna.advance()
        self.sauna.door_open = True
        self.sauna.session = False
        self.sauna.changed()

    async def async_turn_off(self, **kwargs) -> None:
        self.sauna.door_open = False
        self.sauna.changed()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([FakeDoorControl(entry, entry.runtime_data)])
