from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN


class DemoSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, object_id: str, name: str) -> None:
        self.entity_id = f"switch.{object_id}"
        self._attr_unique_id = f"demo_house_{object_id}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "heat")})
        self._attr_is_on = False

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([DemoSwitch("heat_pump_curve_relay", "Curve relay"), DemoSwitch("hot_water_boost", "Hot water boost")])
