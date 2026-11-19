from homeassistant.components.number import NumberEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, _control


class Limit(NumberEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_min_value = 0.0
    _attr_native_max_value = 6.0
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = "kW"

    def __init__(self, key: str, name: str) -> None:
        self._key = "discharge_limit" if "discharging" in key else "charge_limit"
        self._attr_unique_id = f"sigen_0_{key}"
        self.entity_id = f"number.sigen_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "plant")})
        self._attr_native_value = 6.0

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        _control(self._key, value)
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([
        Limit("plant_ess_max_discharging_limit", "Battery maximum discharging limit"),
        Limit("plant_ess_max_charging_limit", "Battery maximum charging limit"),
    ])
