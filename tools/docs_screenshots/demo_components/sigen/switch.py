from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, _control


class RemoteEmsSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self) -> None:
        self._attr_unique_id = "sigen_0_plant_remote_ems_enable"
        self.entity_id = "switch.sigen_plant_remote_ems_enable"
        self._attr_name = "Remote EMS"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "plant")})
        self._attr_is_on = False

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        _control("remote_ems", True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        _control("remote_ems", False)
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([RemoteEmsSwitch()])
