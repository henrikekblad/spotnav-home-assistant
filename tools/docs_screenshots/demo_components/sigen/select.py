from homeassistant.components.select import SelectEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, _control


class RemoteEms(SelectEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_options = ["Standby", "Maximum Self Consumption", "Command Charging (Grid First)", "Command Discharging (PV First)"]

    def __init__(self) -> None:
        self._attr_unique_id = "sigen_0_plant_remote_ems_control_mode"
        self.entity_id = "select.sigen_plant_remote_ems_control_mode"
        self._attr_name = "Remote EMS control mode"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "plant")})
        self._attr_current_option = "Maximum Self Consumption"

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        _control("mode", option)
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([RemoteEms()])
