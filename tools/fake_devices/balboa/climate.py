"""The fake spa's climate and its water model: Ready heats the water at `HEAT_C_H` up to the bath temperature, Rest
lets it fall at `LOSS_C_H`. Writes apply at once and read back on the next state."""

from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from . import DOMAIN

MODEL = "BP501G1"
MAC = "00:00:00:FA:CE:01"
HEAT_C_H = 2.0
LOSS_C_H = 0.3
AMBIENT_C = 10.0
START_C = 30.0
TICK = timedelta(seconds=30)
PRESETS = ["ready", "rest", "ready_in_rest"]
HVAC_OF_PRESET = {"ready": HVACMode.HEAT, "rest": HVACMode.OFF, "ready_in_rest": HVACMode.AUTO}
PRESET_OF_HVAC = {HVACMode.HEAT: "ready", HVACMode.OFF: "rest", HVACMode.AUTO: "ready_in_rest"}


class FakeSpaClimate(ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = PRESETS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_HALVES
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 26.5
    _attr_max_temp = 40.0
    _attr_icon = "mdi:hot-tub"

    def __init__(self) -> None:
        self._attr_unique_id = f"{MODEL}-Climate-{MAC.replace(':', '')[-6:]}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, MAC)},
            name="Balboa spa (fake)",
            manufacturer="Balboa Water Group",
            model=MODEL,
            sw_version="fake",
        )
        self._preset = "ready"
        self._water = START_C
        self._attr_target_temperature = 37.5
        self._last = time.monotonic()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, TICK))

    def _heats(self) -> bool:
        return self._preset in ("ready", "ready_in_rest") and self._water < (self._attr_target_temperature or 0.0)

    def _advance(self) -> None:
        now = time.monotonic()
        hours = (now - self._last) / 3600.0
        self._last = now
        if self._heats():
            self._water = min(self._attr_target_temperature, self._water + HEAT_C_H * hours)
        elif self._water > AMBIENT_C:
            self._water = max(AMBIENT_C, self._water - LOSS_C_H * hours)

    def _tick(self, _now) -> None:
        self._advance()
        self.async_write_ha_state()

    @property
    def current_temperature(self) -> float:
        return round(self._water, 1)

    @property
    def hvac_mode(self) -> HVACMode:
        return HVAC_OF_PRESET[self._preset]

    @property
    def hvac_action(self) -> HVACAction:
        return HVACAction.HEATING if self._heats() else HVACAction.IDLE

    @property
    def preset_mode(self) -> str:
        return self._preset

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"fake_device": "yes"}

    async def async_set_temperature(self, **kwargs) -> None:
        self._advance()
        value = float(kwargs[ATTR_TEMPERATURE])
        self._attr_target_temperature = min(self._attr_max_temp, max(self._attr_min_temp, round(value * 2) / 2))
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._advance()
        self._preset = preset_mode
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._advance()
        self._preset = PRESET_OF_HVAC[HVACMode(hvac_mode)]
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([FakeSpaClimate()])
