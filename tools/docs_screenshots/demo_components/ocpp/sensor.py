from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from . import CHARGE_POINTS
from .entity import ConnectorEntity


class Register(ConnectorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "kWh"
    _attr_native_value = 1284.6


class Current(ConnectorEntity, SensorEntity):
    """Current.Import: the total as the state, each phase as an attribute, the way the integration exposes it."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "A"
    _attr_native_value = 0.0
    _attr_extra_state_attributes = {"L1": 0.0, "L2": 0.0, "L3": 0.0}


class Voltage(ConnectorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "V"
    _attr_native_value = 230.0
    _attr_extra_state_attributes = {"L1": 230.0, "L2": 231.0, "L3": 229.0}


class Status(ConnectorEntity, SensorEntity):
    _attr_native_value = "Preparing"


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    entities = []
    for cpid, name in CHARGE_POINTS:
        entities.append(
            Register("sensor", cpid, name, "energy_active_import_register", "Energy active import register")
        )
        entities.append(Current("sensor", cpid, name, "current_import", "Current import"))
        entities.append(Voltage("sensor", cpid, name, "voltage", "Voltage"))
        entities.append(Status("sensor", cpid, name, "status_connector", "Status connector"))
    async_add_entities(entities)
