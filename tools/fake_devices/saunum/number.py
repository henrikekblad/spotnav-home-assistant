"""The fake sauna's session length (minutes, Saunum's `sauna_duration`): a session ends by itself after it."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import UnitOfTime

from .entity import FakeSaunaEntity


class FakeSaunaDuration(FakeSaunaEntity, NumberEntity):
    _attr_translation_key = "sauna_duration"
    _attr_name = "Sauna duration"
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 1
    _attr_native_max_value = 720
    _attr_native_step = 1

    def __init__(self, entry, sauna) -> None:
        super().__init__(entry, sauna, "sauna_duration")

    @property
    def native_value(self) -> float:
        return self.sauna.duration_min

    async def async_set_native_value(self, value: float) -> None:
        self.sauna.duration_min = float(value)
        self.sauna.changed()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([FakeSaunaDuration(entry, entry.runtime_data)])
