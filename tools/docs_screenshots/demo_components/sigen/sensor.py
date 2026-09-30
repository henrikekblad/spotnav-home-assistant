from __future__ import annotations

import math
import time
from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from . import DOMAIN

#: phase letter -> (grid power kW, inverter voltage V)
PHASES = {"a": (1.12, 229.6), "b": (0.74, 231.2), "c": (0.96, 230.4)}


class DemoSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, *, unique_id, object_id, name, device, device_class, unit, base, spread, seed) -> None:
        self._attr_unique_id = unique_id
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device)})
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._base, self._spread, self._seed = base, spread, seed
        self._attr_native_value = round(base, 3)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, timedelta(seconds=5)))

    async def _tick(self, now) -> None:
        wobble = math.sin(time.time() / 7.0 + self._seed) * self._spread
        self._attr_native_value = round(self._base + wobble, 3)
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    entities = []
    for index, (phase, (power, volts)) in enumerate(PHASES.items()):
        entities.append(DemoSensor(
            unique_id=f"sigen_0_plant_grid_sensor_phase_{phase}_active_power",
            object_id=f"sigen_plant_grid_phase_{phase}_active", name=f"Phase {phase.upper()} active power",
            device="plant", device_class=SensorDeviceClass.POWER, unit="kW", base=power, spread=0.04, seed=index,
        ))
        entities.append(DemoSensor(
            unique_id=f"sigen_0_plant_grid_sensor_phase_{phase}_reactive_power",
            object_id=f"sigen_plant_grid_phase_{phase}_reactive", name=f"Phase {phase.upper()} reactive power",
            device="plant", device_class=SensorDeviceClass.REACTIVE_POWER, unit="kvar", base=0.05, spread=0.01,
            seed=index,
        ))
        entities.append(DemoSensor(
            unique_id=f"sigen_1_inverter_phase_{phase}_voltage",
            object_id=f"sigen_inverter_phase_{phase}_voltage", name=f"Phase {phase.upper()} voltage",
            device="inverter", device_class=SensorDeviceClass.VOLTAGE, unit="V", base=volts, spread=0.6, seed=index,
        ))
    entities.append(DemoSensor(
        unique_id="sigen_0_plant_ess_power", object_id="sigen_plant_ess_power", name="Battery power",
        device="plant", device_class=SensorDeviceClass.POWER, unit="kW", base=0.0, spread=0.02, seed=9,
    ))
    async_add_entities(entities)
