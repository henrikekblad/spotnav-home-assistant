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


def _live():
    """The demo house's simulation (`demo_house/model.py`), when that integration is there."""
    try:
        from custom_components.demo_house import model
    except ImportError:  # pragma: no cover - the tool always has it
        return None
    return model.live()


SHARE = {"a": 0.40, "b": 0.27, "c": 0.33}


class DemoSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, *, unique_id, object_id, name, device, device_class, unit, base, spread, seed, live=None, state_class=SensorStateClass.MEASUREMENT) -> None:
        self._live = live
        self._attr_state_class = state_class
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
        if self._live is not None:
            values = _live()
            if values is not None:
                self._attr_native_value = round(self._live(values), 3)
                self.async_write_ha_state()
                return
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
            live=lambda v, share=SHARE[phase]: v["grid"] * share,
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
        device="plant", device_class=SensorDeviceClass.POWER, unit="kW", base=0.0, spread=0.02, seed=9, live=lambda v: v["batt"],
    ))
    for key, name, unit, device_class, base, live, state_class in (
        ("plant_ess_soc", "Battery state of charge", "%", SensorDeviceClass.BATTERY, 55.0, lambda v: v["soc"], SensorStateClass.MEASUREMENT),
        ("plant_ess_rated_energy_capacity", "Battery rated capacity", "kWh", SensorDeviceClass.ENERGY_STORAGE, 15.0, None, SensorStateClass.MEASUREMENT),
        ("plant_ess_rated_charging_power", "Battery rated charging power", "kW", SensorDeviceClass.POWER, 6.0, None, SensorStateClass.MEASUREMENT),
        ("plant_ess_rated_discharging_power", "Battery rated discharging power", "kW", SensorDeviceClass.POWER, 6.0, None, SensorStateClass.MEASUREMENT),
        ("plant_ess_discharge_cut_off_soc", "Battery discharge cut-off", "%", SensorDeviceClass.BATTERY, 10.0, None, SensorStateClass.MEASUREMENT),
        ("plant_max_active_power", "Plant maximum active power", "kW", SensorDeviceClass.POWER, 10.0, None, SensorStateClass.MEASUREMENT),
        ("plant_ess_average_cell_temperature", "Battery cell temperature", "°C", SensorDeviceClass.TEMPERATURE, 17.5, None, SensorStateClass.MEASUREMENT),
    ):
        entities.append(DemoSensor(
            unique_id=f"sigen_0_{key}", object_id=f"sigen_{key}", name=name, device="plant", device_class=device_class,
            unit=unit, base=base, spread=0.0, seed=1, live=live, state_class=state_class,
        ))
    for key, name, value in (
        ("plant_ems_work_mode", "EMS work mode", "max_self_consumption"),
        ("plant_on_off_grid_status", "On/off grid status", "on_grid"),
    ):
        entities.append(TextSensor(f"sigen_0_{key}", f"sigen_{key}", name, "plant", value))
    entities.append(TextSensor("sigen_1_inverter_machine_firmware_version", "sigen_inverter_machine_firmware_version", "Firmware version", "inverter", "V100R001C00SPC117"))
    async_add_entities(entities)


class TextSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, unique_id, object_id, name, device, value) -> None:
        self._attr_unique_id = unique_id
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device)})
        self._attr_native_value = value
