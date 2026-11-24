"""The pool heat pump: `heat` or `off`, the water coming in as its current temperature, 15-40 °C."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

#: Home Assistant 2026.11 renamed the climate's temperatures to `native_*`: set whichever this version reads.
_NATIVE = hasattr(ClimateEntity, "_attr_native_current_temperature")
CURRENT_ATTR = "_attr_native_current_temperature" if _NATIVE else "_attr_current_temperature"
TARGET_ATTR = "_attr_native_target_temperature" if _NATIVE else "_attr_target_temperature"
UNIT_ATTR = "_attr_native_temperature_unit" if _NATIVE else "_attr_temperature_unit"


class PoolHeatPump(ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_translation_key = "pool_heatpump"
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
    )
    _attr_precision = PRECISION_TENTHS
    _attr_target_temperature_step = 1.0
    _attr_min_temp = 15.0
    _attr_max_temp = 40.0

    def __init__(self, pool) -> None:
        self.pool = pool
        self._attr_unique_id = "demo_pool_heat_pump"
        self.entity_id = "climate.pool_heat_pump"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "pool_heat_pump")}, name="Pool heat pump", manufacturer="Tuya", model="Pool heat pump",
        )
        setattr(self, UNIT_ATTR, UnitOfTemperature.CELSIUS)
        self._set_temperatures()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.pool.listen(self.async_write_ha_state))

    def _set_temperatures(self) -> None:
        setattr(self, CURRENT_ATTR, round(self.pool.water_c, 1))
        setattr(self, TARGET_ATTR, self.pool.target_c)

    @callback
    def async_write_ha_state(self) -> None:
        self._set_temperatures()
        super().async_write_ha_state()

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.HEAT if self.pool.heat_on else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        if not self.pool.heat_on:
            return HVACAction.OFF
        return HVACAction.HEATING if self.pool.heating() else HVACAction.IDLE

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self.pool.advance()
        self.pool.heat_on = hvac_mode == HVACMode.HEAT
        self.pool.changed()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs) -> None:
        self.pool.advance()
        self.pool.target_c = float(min(self._attr_max_temp, max(self._attr_min_temp, round(float(kwargs[ATTR_TEMPERATURE])))))
        self.pool.changed()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([PoolHeatPump(entry.runtime_data)])
