from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from . import DOMAIN, model

#: object id, name, device, key in the simulation, device class, unit, state class
SPECS = (
    ("demo_solar_power", "Solar power", "solar", "sun", SensorDeviceClass.POWER, "kW", SensorStateClass.MEASUREMENT),
    ("demo_solar_energy", "Solar energy", "solar", "e_solar", SensorDeviceClass.ENERGY, "kWh", SensorStateClass.TOTAL_INCREASING),
    ("demo_grid_import_energy", "Grid import", "solar", "e_grid_in", SensorDeviceClass.ENERGY, "kWh", SensorStateClass.TOTAL_INCREASING),
    ("demo_grid_export_energy", "Grid export", "solar", "e_grid_out", SensorDeviceClass.ENERGY, "kWh", SensorStateClass.TOTAL_INCREASING),
    ("demo_battery_charge_energy", "Battery charged", "solar", "e_batt_in", SensorDeviceClass.ENERGY, "kWh", SensorStateClass.TOTAL_INCREASING),
    ("demo_battery_discharge_energy", "Battery discharged", "solar", "e_batt_out", SensorDeviceClass.ENERGY, "kWh", SensorStateClass.TOTAL_INCREASING),
    ("demo_heat_pump_power", "Heat pump power", "heat", "heat", SensorDeviceClass.POWER, "kW", SensorStateClass.MEASUREMENT),
    ("demo_heat_pump_energy", "Heat pump energy", "heat", "e_heat", SensorDeviceClass.ENERGY, "kWh", SensorStateClass.TOTAL_INCREASING),
    ("living_room_temperature", "Living room", "rooms", "living", SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
    ("bedroom_temperature", "Bedroom", "rooms", "bedroom", SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
    ("office_temperature", "Office", "rooms", "office", SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
    ("outdoor_temperature", "Outdoor temperature", "rooms", "t_out", SensorDeviceClass.TEMPERATURE, "°C", SensorStateClass.MEASUREMENT),
)


class DemoHouseSensor(SensorEntity):
    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, spec) -> None:
        object_id, name, device, key, device_class, unit, state_class = spec
        self.entity_id = f"sensor.{object_id}"
        self._attr_unique_id = f"demo_house_{object_id}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device)})
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_state_class = state_class
        self._key = key
        self._attr_native_value = self._read()

    def _read(self) -> float:
        return round(model.live()[self._key], 1 if self._attr_native_unit_of_measurement == "°C" else 3)

    def refresh(self) -> None:
        self._attr_native_value = self._read()
        self.async_write_ha_state()

    async def _tick(self, now) -> None:
        self.refresh()

    async def async_added_to_hass(self) -> None:
        self.hass.data[DOMAIN]["entities"].append(self)
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, timedelta(seconds=10)))


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(DemoHouseSensor(spec) for spec in SPECS)
