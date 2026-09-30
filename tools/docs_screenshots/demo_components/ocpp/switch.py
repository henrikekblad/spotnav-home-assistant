from homeassistant.components.switch import SwitchEntity

from . import CHARGE_POINTS
from .entity import ConnectorEntity


class ChargeControl(ConnectorEntity, SwitchEntity):
    _attr_is_on = False

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        ChargeControl("switch", cpid, name, "charge_control", "Charge control") for cpid, name in CHARGE_POINTS
    )
