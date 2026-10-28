"""Selectors: a charger's strategy and charge periods, and an electricity price device's area and fee policies.

A charger's entities write through the preview's `async_apply_settings` (compare-and-set against the revision it
displayed), never the store or a charger directly. The area and fee policies are the price device's
(`price_entities.py`). The state is the stored machine value (`cheapest`, `auto`); labels live in the
translations.
"""

from __future__ import annotations

from dataclasses import replace

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .entity import AutoSurface, SpotNavAutoEntity
from .execution.controller import ChargingController
from .planning.auto_settings import MAX_PERIODS, MIN_PERIODS
from .planning.strategy_options import strategy_options_for
from .price_entities import price_selects, remove_charger_price_entities
from .runtime import ChargerConfigEntry, price_data


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if (price := price_data(hass, entry.entry_id)) is not None:
        # An electricity price entry's device: its area and fee policies (`price_entities.py`).
        async_add_entities(price_selects(entry, price.device))
        return
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE:
        # Second guard: a platform forwarded for the wrong entry type creates nothing.
        return
    controller = entry.runtime_data.controller
    auto = AutoSurface.resolve(hass, entry.entry_id)
    # The phases are no longer a setting (the charger's wiring and the vehicle's onboard charger decide them):
    # the select an older release created is removed.
    registry = er.async_get(hass)
    leftover = registry.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_charging_phases")
    if leftover is not None:
        registry.async_remove(leftover)
    # The area and fee policies moved to the electricity price device: the charger's former ones are removed.
    remove_charger_price_entities(hass, "select", entry.entry_id)
    async_add_entities(
        [
            AutoStrategySelect(entry, controller, auto),
            AutoPeriodsSelect(entry, controller, auto),
        ]
    )


class AutoStrategySelect(SpotNavAutoEntity, SelectEntity):
    """Which strategy Auto runs: `cheapest` always, `solar`/`hybrid` when the site can measure the
    signal both need.

    `strategy_options_for` (which folds in `_site_is_solar_capable`) is also what the
    dashboard reports as `strategy_options`, so the two cannot disagree. Writes through
    `SpotNavAutoEntity.async_write_settings`.
    """

    _attr_entity_category = EntityCategory.CONFIG

    _attr_translation_key = "auto_strategy"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="auto_strategy")

    @property
    def options(self) -> list[str]:
        """`cheapest`, plus `solar` and `hybrid` when the site supports the signed grid power they need."""
        return strategy_options_for(self.hass, self._entry.entry_id)

    @property
    def current_option(self) -> str | None:
        settings = self.settings
        return None if settings is None else settings.strategy

    async def async_select_option(self, option: str) -> None:
        await self.async_write_settings(lambda settings: replace(settings, strategy=option))


#: The charge-periods select's options: automatic, or a hard cap of 1 to 8.
PERIODS_AUTO_OPTION = "auto"
PERIODS_OPTIONS = [PERIODS_AUTO_OPTION, *(str(count) for count in range(MIN_PERIODS, MAX_PERIODS + 1))]


class AutoPeriodsSelect(SpotNavAutoEntity, SelectEntity):
    """How the plan splits into charge periods: `auto` (the planner weighs a start cost per period), or at most
    that many periods."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "charge_periods"
    _attr_options = PERIODS_OPTIONS

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="charge_periods")

    @property
    def current_option(self) -> str | None:
        settings = self.settings
        if settings is None:
            return None
        return PERIODS_AUTO_OPTION if settings.max_periods is None else str(settings.max_periods)

    async def async_select_option(self, option: str) -> None:
        count = None if option == PERIODS_AUTO_OPTION else int(option)
        await self.async_write_settings(lambda settings: replace(settings, max_periods=count))
