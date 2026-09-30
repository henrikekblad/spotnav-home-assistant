"""The Auto price path's departure time: an area-local wall time, with no timezone."""

from __future__ import annotations

from dataclasses import replace
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
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
    async_add_entities([AutoDepartureTime(entry, controller, auto)])


class AutoDepartureTime(SpotNavAutoEntity, TimeEntity):
    """When the charge should be finished, as a naive wall-clock time.

    "07:30 where the charger is", resolved by the planner against the selected area's timezone; an
    offset stored here would break across a clock change.
    """

    _attr_entity_category = EntityCategory.CONFIG

    _attr_translation_key = "departure_time"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="departure_time")

    @property
    def native_value(self) -> time | None:
        settings = self.settings
        return None if settings is None else settings.departure

    async def async_set_value(self, value: time) -> None:
        """Store a whole-minute, area-local wall time, or refuse; never round.

        A value with an offset, seconds or microseconds cannot be stored as `HH:MM` without lying about
        what was picked, so it is refused by a stable name.
        """
        if value.tzinfo is not None:
            raise ServiceValidationError(
                "The departure time is a wall clock time in the market's own timezone, "
                "without an offset.",
                translation_domain=DOMAIN,
                translation_key="invalid_departure",
            )
        if value.second or value.microsecond:
            raise ServiceValidationError(
                "The departure time is stored to the minute, so seconds cannot be kept.",
                translation_domain=DOMAIN,
                translation_key="invalid_departure",
            )
        await self.async_write_settings(lambda settings: replace(settings, departure=value))
