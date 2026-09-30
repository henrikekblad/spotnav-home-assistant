"""Switches for the Auto price path: the departure deadline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_SITE
from .entity import AutoSurface, SpotNavAutoEntity
from .execution.controller import ChargingController
from .planning.auto_settings import AutoSettings
from .runtime import ChargerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
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
