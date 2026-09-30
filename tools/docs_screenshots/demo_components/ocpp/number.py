from homeassistant.components.number import NumberEntity, NumberMode

from . import CHARGE_POINTS
from .entity import ConnectorEntity


class Current(ConnectorEntity, NumberEntity):
    _attr_native_unit_of_measurement = "A"
    _attr_native_min_value = 6
    _attr_native_max_value = 16
    _attr_native_step = 1
    _attr_native_value = 16
    _attr_mode = NumberMode.BOX

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    entities = []
    for cpid, name in CHARGE_POINTS:
        entities.append(Current("number", cpid, name, "session_current_limit", "Session current limit"))
        station = Current("number", cpid, name, "maximum_current", "Maximum current", connector=False)
        station._attr_native_min_value = 0
        entities.append(station)
    async_add_entities(entities)
