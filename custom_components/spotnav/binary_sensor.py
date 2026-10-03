"""Binary sensors for SpotNav charging control."""

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CHARGER_ENTRY_IDS, CONF_ENTRY_TYPE, ENTRY_TYPE_SITE
from .entity import SpotNavChargingEntity, SpotNavSiteEntity
from .execution.controller import ChargingController
from .execution.grid_charge import grid_charge
from .runtime import controller_for
from .site.site_capacity_controller import SiteCapacityController


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        async_add_entities([SiteGridChargingEntity(hass, entry, entry.runtime_data.controller)])
        return

    controller = entry.runtime_data.controller
    async_add_entities(
        [ScheduleActiveEntity(entry, controller), GridChargingEntity(hass, entry, controller)]
    )


class ScheduleActiveEntity(SpotNavChargingEntity, BinarySensorEntity):
    """Whether a SpotNav charging schedule is active."""

    _attr_translation_key = "schedule_active"

    def __init__(self, entry: ConfigEntry, controller: ChargingController) -> None:
        super().__init__(entry, controller)
        self._attr_unique_id = f"{entry.entry_id}_schedule_active"

    @property
    def is_on(self) -> bool:
        return self.controller.plan is not None


class GridChargingEntity(SpotNavChargingEntity, BinarySensorEntity):
    """On while this charger charges with energy meant to come from the grid."""

    _attr_translation_key = "grid_charging"

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, controller: ChargingController
    ) -> None:
        super().__init__(entry, controller)
        self.hass = hass
        self._attr_unique_id = f"{entry.entry_id}_grid_charging"

    @property
    def is_on(self) -> bool:
        return grid_charge(self.hass, self._entry.entry_id, self.controller).on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return grid_charge(self.hass, self._entry.entry_id, self.controller).attributes()


class SiteGridChargingEntity(SpotNavSiteEntity, BinarySensorEntity):
    """On while any charger of this site charges from the grid: the one a battery planner points at."""

    _attr_translation_key = "grid_charging_site"

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, controller: SiteCapacityController
    ) -> None:
        super().__init__(entry, controller)
        self.hass = hass
        self._attr_unique_id = f"{entry.entry_id}_grid_charging"

    def _active(self) -> list[str]:
        active: list[str] = []
        for charger_entry_id in self.controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
            charger = controller_for(self.hass, charger_entry_id)
            if charger is not None and grid_charge(self.hass, charger_entry_id, charger).on:
                active.append(charger_entry_id)
        return active

    @property
    def is_on(self) -> bool:
        return bool(self._active())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"charger_ids": self._active()}
