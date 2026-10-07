from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, VEHICLES

# key, name, value, min, max
NUMBERS = (
    ("ev_charge_limits_ac", "AC charge limit", 80, 50, 100),
    ("ev_charge_limits_dc", "DC charge limit", 100, 50, 100),
)


class ChargeLimit(NumberEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "%"
    _attr_native_step = 10
    _attr_mode = NumberMode.SLIDER

    def __init__(self, vehicle_id, prefix, key, name, value, low, high) -> None:
        self._attr_unique_id = f"{vehicle_id}_{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vehicle_id)})
        self._attr_native_value = value
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self.entity_id = f"number.{prefix}_{key}"

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        ChargeLimit(vehicle_id, prefix, *spec) for vehicle_id, prefix, *_ in VEHICLES for spec in NUMBERS
    )
