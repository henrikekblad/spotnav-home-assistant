"""The fake sauna's climate: `heat` starts a session, `off` ends it (Saunum's shape)."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from .entity import FakeSaunaEntity

#: Home Assistant 2026.11 renamed the climate's temperatures to `native_*` (a subclass still overriding the old
#: properties has their value cached): the fake sets whichever attributes this version reads.
_NATIVE = hasattr(ClimateEntity, "_attr_native_current_temperature")
CURRENT_ATTR = "_attr_native_current_temperature" if _NATIVE else "_attr_current_temperature"
TARGET_ATTR = "_attr_native_target_temperature" if _NATIVE else "_attr_target_temperature"
UNIT_ATTR = "_attr_native_temperature_unit" if _NATIVE else "_attr_temperature_unit"


class FakeSaunaClimate(FakeSaunaEntity, ClimateEntity):
    _attr_name = None
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_precision = PRECISION_WHOLE
    _attr_target_temperature_step = 1.0
    _attr_min_temp = 40.0
    _attr_max_temp = 100.0

    def __init__(self, entry, sauna) -> None:
        super().__init__(entry, sauna, None)
        setattr(self, UNIT_ATTR, UnitOfTemperature.CELSIUS)
        self._set_temperatures()

    def _set_temperatures(self) -> None:
        setattr(self, CURRENT_ATTR, round(self.sauna.air_c, 1))
        setattr(self, TARGET_ATTR, self.sauna.target_c)

    @callback
    def async_write_ha_state(self) -> None:
        """Every redraw (the model's listener) carries the model's temperatures."""
        self._set_temperatures()
        super().async_write_ha_state()

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.HEAT if self.sauna.session else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        if not self.sauna.session:
            return HVACAction.OFF
        return HVACAction.HEATING if self.sauna.heating() else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"fake_device": "yes"}

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        sauna = self.sauna
        sauna.advance()
        if hvac_mode == HVACMode.HEAT:
            if sauna.door_open:
                raise ServiceValidationError("The (fake) sauna door is open")
            if not sauna.session:
                sauna.session = True
                sauna.session_started = sauna.last
        else:
            sauna.session = False
        sauna.changed()

    async def async_set_temperature(self, **kwargs) -> None:
        self.sauna.advance()
        self.sauna.target_c = float(min(self._attr_max_temp, max(self._attr_min_temp, round(float(kwargs[ATTR_TEMPERATURE])))))
        self.sauna.changed()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([FakeSaunaClimate(entry, entry.runtime_data)])
