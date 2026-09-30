from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, VEHICLE_ID

# key, name, device class, unit, value, state class
SENSORS = (
    ("ev_battery_percentage", "EV battery level", SensorDeviceClass.BATTERY, "%", 64, SensorStateClass.MEASUREMENT),
    ("ev_battery_soh_percentage", "EV battery health", SensorDeviceClass.BATTERY, "%", 96, SensorStateClass.MEASUREMENT),
    ("car_battery_percentage", "12V battery level", None, "%", 88, None),
    ("ev_battery_capacity", "EV battery capacity", SensorDeviceClass.ENERGY_STORAGE, "kJ", 230400, SensorStateClass.MEASUREMENT),
    ("ev_battery_remain", "EV battery remaining", SensorDeviceClass.ENERGY_STORAGE, "kJ", 147456, SensorStateClass.MEASUREMENT),
    ("ev_driving_range", "EV range", SensorDeviceClass.DISTANCE, "km", 310, None),
)


class VehicleSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, key, name, device_class, unit, value, state_class) -> None:
        self._attr_unique_id = f"{VEHICLE_ID}-{key}"
        self._attr_translation_key = key
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, VEHICLE_ID)})
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_native_value = value
        self._attr_state_class = state_class
        self.entity_id = f"sensor.family_car_{key}"


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(VehicleSensor(*spec) for spec in SENSORS)
