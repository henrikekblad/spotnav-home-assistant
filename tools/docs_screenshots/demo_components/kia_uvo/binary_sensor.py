"""Each car's "plugged in" sensor, off until `kia_uvo.docs_set_plug` turns it on."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import DOMAIN, SIGNAL_PLUG, VEHICLES


class PlugSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_translation_key = "ev_battery_plug"
    _attr_name = "EV battery plug"
    _attr_is_on = False

    def __init__(self, vehicle_id, prefix) -> None:
        self._prefix = prefix
        self._attr_unique_id = f"{vehicle_id}-ev_battery_plug"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vehicle_id)})
        self.entity_id = f"binary_sensor.{prefix}_ev_battery_plug"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_PLUG, self._moved))

    @callback
    def _moved(self, vehicle: str, plugged: bool) -> None:
        if vehicle == self._prefix:
            self._attr_is_on = plugged
            self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(PlugSensor(vehicle_id, prefix) for vehicle_id, prefix, *_ in VEHICLES)
