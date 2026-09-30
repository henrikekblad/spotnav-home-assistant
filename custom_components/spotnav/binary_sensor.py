"""Binary sensors for SpotNav charging control."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_SITE
from .entity import SpotNavChargingEntity
from .execution.controller import ChargingController


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return

    controller = entry.runtime_data.controller
    async_add_entities([ScheduleActiveEntity(entry, controller)])


class ScheduleActiveEntity(SpotNavChargingEntity, BinarySensorEntity):
    """Whether a SpotNav charging schedule is active."""

    _attr_translation_key = "schedule_active"

    def __init__(self, entry: ConfigEntry, controller: ChargingController) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_schedule_active"

    @property
    def is_on(self) -> bool:
        return self.controller.plan is not None
