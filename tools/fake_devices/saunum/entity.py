"""The fake sauna's base entity: on its device, redrawn whenever the shared state changes."""

from __future__ import annotations

from homeassistant.helpers.entity import Entity

from . import FakeSauna, device_info


class FakeSaunaEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, sauna: FakeSauna, key: str | None) -> None:
        self.sauna = sauna
        self._attr_unique_id = entry.entry_id if key is None else f"{entry.entry_id}-{key}"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.sauna.listen(self.async_write_ha_state))
