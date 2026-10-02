"""The Auto price path's departure date: the local day a departure falls on, or none for a daily one."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_SITE
from .entity import AutoSurface, SpotNavAutoEntity
from .execution.controller import ChargingController
from .runtime import ChargerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        return
    controller = entry.runtime_data.controller
    auto = AutoSurface.resolve(hass, entry.entry_id)
    async_add_entities([AutoDepartureDate(entry, controller, auto)])


class AutoDepartureDate(SpotNavAutoEntity, DateEntity):
    """The day the departure time falls on, in the market's own zone.

    Unknown (no date) is the ordinary daily departure: the next occurrence of the departure time. A date
    that has gone by reads as unknown, because planning ignores it. The controller refuses a date in the past
    or more than 7 days ahead at the write, and the card's "clear" is the button beside this entity (a date
    entity cannot be set to nothing).
    """

    _attr_entity_category = EntityCategory.CONFIG

    _attr_translation_key = "departure_date"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="departure_date")

    @property
    def native_value(self) -> date | None:
        settings = self.settings
        return None if settings is None else settings.departure_date

    async def async_set_value(self, value: date) -> None:
        await self.async_write_settings(lambda settings: replace(settings, departure_date=value))
