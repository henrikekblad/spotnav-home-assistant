from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, VEHICLES


def sensors(charge, capacity, range_km):
    """key, name, device class, unit, value, state class"""
    return (
        ("ev_battery_percentage", "EV battery level", SensorDeviceClass.BATTERY, "%", charge, SensorStateClass.MEASUREMENT),
        ("ev_battery_soh_percentage", "EV battery health", SensorDeviceClass.BATTERY, "%", 96, SensorStateClass.MEASUREMENT),
        ("car_battery_percentage", "12V battery level", None, "%", 88, None),
        ("ev_battery_capacity", "EV battery capacity", SensorDeviceClass.ENERGY_STORAGE, "kJ", capacity, SensorStateClass.MEASUREMENT),
        ("ev_battery_remain", "EV battery remaining", SensorDeviceClass.ENERGY_STORAGE, "kJ", round(capacity * charge / 100), SensorStateClass.MEASUREMENT),
        ("ev_driving_range", "EV range", SensorDeviceClass.DISTANCE, "km", range_km, None),
    )


class VehicleSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, vehicle_id, prefix, key, name, device_class, unit, value, state_class) -> None:
        self._attr_unique_id = f"{vehicle_id}-{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vehicle_id)})
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = value
        self._attr_state_class = state_class
        self.entity_id = f"sensor.{prefix}_{key}"


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        VehicleSensor(vehicle_id, prefix, *spec)
        for vehicle_id, prefix, _name, _model, charge, capacity, range_km in VEHICLES
        for spec in sensors(charge, capacity, range_km)
    )
