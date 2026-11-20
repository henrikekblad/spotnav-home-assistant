"""Switches: the Auto price path's departure deadline (a charger), and the site's heat Away (`SiteHeatAwaySwitch`)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_ENTRY_TYPE, CONF_HEAT, DOMAIN, ENTRY_TYPE_SITE
from .entity import AutoSurface, SpotNavAutoEntity, SpotNavSiteEntity
from .heat.config import AWAY_MAX_DAYS, F_AWAY_UNTIL, F_MODE, MODE_AWAY, MODE_HOME, read_config
from .execution.controller import ChargingController
from .planning.auto_settings import AutoSettings
from .runtime import ChargerConfigEntry

_LOGGER = logging.getLogger(__name__)


def heat_changed_signal(entry_id: str) -> str:
    """Sent when a site's heat settings are written (`api/heat.write_heat`)."""
    return f"{DOMAIN}_heat_changed_{entry_id}"


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        async_add_entities([SiteHeatAwaySwitch(hass, entry, entry.runtime_data.controller)])
        return
    controller = entry.runtime_data.controller
    auto = AutoSurface.resolve(hass, entry.entry_id)
    async_add_entities([AutoDepartureSwitch(entry, controller, auto)])


class AutoSettingSwitch(SpotNavAutoEntity, SwitchEntity):
    """Base for a boolean stored setting, written through the reviewed settings path."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_turn_on(self, **kwargs: object) -> None:
        await self.async_write_settings(self.store_value(True))

    async def async_turn_off(self, **kwargs: object) -> None:
        await self.async_write_settings(self.store_value(False))

    @property
    def is_on(self) -> bool | None:
        """The stored value, or `unknown` when there is nothing stored to read."""
        settings = self.settings
        return None if settings is None else self.is_on_setting(settings)

    def is_on_setting(self, settings: AutoSettings) -> bool:
        """This switch's own field. Subclasses implement exactly this."""
        raise NotImplementedError

    def store_value(self, value: bool) -> Callable[[AutoSettings], AutoSettings]:
        """The mutator for this switch's own field. Subclasses implement exactly this."""
        raise NotImplementedError


class AutoDepartureSwitch(AutoSettingSwitch):
    """Whether the departure time constrains the plan at all.

    Off means "charge the requested energy as cheaply as possible", with no deadline. There is no
    inverted *pause* switch: pause/resume are their own buttons.
    """

    _attr_translation_key = "departure_enabled"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="departure_enabled")

    def is_on_setting(self, settings: AutoSettings) -> bool:
        return settings.departure_enabled

    def store_value(self, value: bool) -> Callable[[AutoSettings], AutoSettings]:
        return lambda settings: replace(settings, departure_enabled=value)


class SiteHeatAwaySwitch(SpotNavSiteEntity, SwitchEntity):
    """The house's Away as a switch (the owner's decision D5 of 2026-11-20), for an alarm or presence automation: on is
    away until it is turned off (the heat settings' away with its return time as far ahead as it may be, 90 days),
    off is home again, so the heat comes back to normal at once. Away set by date in the card reads as on too, and
    turning it off ends it. The same settings write as the heat card's (`api/heat.write_heat`)."""

    _attr_translation_key = "heat_away"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, controller: object) -> None:
        super().__init__(entry, controller)  # type: ignore[arg-type]
        self.hass = hass
        self._attr_unique_id = f"{entry.entry_id}_heat_away"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, heat_changed_signal(self._entry.entry_id), self.async_write_ha_state)
        )

    @property
    def is_on(self) -> bool:
        entry = self.hass.config_entries.async_get_entry(self._entry.entry_id)
        config = read_config(None if entry is None else entry.data.get(CONF_HEAT))
        end = config.away_end()
        return config.mode == MODE_AWAY and end is not None and end > dt_util.utcnow()

    async def async_turn_on(self, **kwargs: object) -> None:
        if self.is_on:
            return
        until = dt_util.utcnow() + timedelta(days=AWAY_MAX_DAYS) - timedelta(minutes=1)
        self._write({F_MODE: MODE_AWAY, F_AWAY_UNTIL: until.isoformat()})

    async def async_turn_off(self, **kwargs: object) -> None:
        self._write({F_MODE: MODE_HOME})

    def _write(self, changes: dict[str, object]) -> None:
        from .api.heat import HeatWriteRefusal, write_heat  # noqa: PLC0415 - the API imports the platforms' peers

        entry = self.hass.config_entries.async_get_entry(self._entry.entry_id)
        if entry is None:
            raise HomeAssistantError("The SpotNav site is not loaded")
        try:
            write_heat(self.hass, entry, changes, {}, admin=True)
        except HeatWriteRefusal as refusal:
            _LOGGER.warning("SpotNav heat away: the write was refused: %s %s", refusal.code, refusal.field_errors)
            raise HomeAssistantError(f"SpotNav could not change Away: {refusal.code}") from refusal
        self.async_write_ha_state()
