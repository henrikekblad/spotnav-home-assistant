"""The pool's circulation pump: a relay on a device of its own, named for the pool."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN


class CirculationPump(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_icon = "mdi:pump"

    def __init__(self, pool) -> None:
        self.pool = pool
        self._attr_unique_id = "demo_pool_pump"
        self.entity_id = "switch.pool_pump"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "pool_pump")}, name="Pool pump", model="Relay")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.pool.listen(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self.pool.circulation_on

    async def async_turn_on(self, **kwargs) -> None:
        self.pool.advance()
        self.pool.circulation_on = True
        self.pool.changed()

    async def async_turn_off(self, **kwargs) -> None:
        self.pool.advance()
        self.pool.circulation_on = False
        self.pool.changed()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([CirculationPump(entry.runtime_data)])
