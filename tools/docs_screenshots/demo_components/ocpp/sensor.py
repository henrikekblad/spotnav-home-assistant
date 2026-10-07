from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import CHARGE_POINTS, SIGNAL_STATUS
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

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATUS, self._moved))

    @callback
    def _moved(self, charge_point: str, status: str) -> None:
        if self._object_id.startswith(f"{charge_point}_"):
            self._attr_native_value = status
            self.async_write_ha_state()


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
