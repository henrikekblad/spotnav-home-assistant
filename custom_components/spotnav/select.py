"""Selectors for the Auto price path: market, strategy and the fiscal policies.

Every entity writes through the preview's `async_apply_settings` (compare-and-set against the
revision it displayed), never the store or a charger directly. The state is the stored machine
value (`SE4`, `auto_price`, `manual`); labels live in the translations.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import callback, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTRY_TYPE, DOMAIN, ENTRY_TYPE_SITE
from .entity import (
    AutoSurface,
    FISCAL_COMPONENTS,
    FISCAL_OPTIONS,
    fiscal_policy,
    SpotNavAutoEntity,
    with_fiscal_policy,
)
from .execution.controller import ChargingController
from .planning.auto_controller import component_included
from .planning.auto_settings import AutoSettingsError
from .planning.strategy_options import strategy_options_for
from .pricing.price_repository import CatalogueSnapshot
from .runtime import ChargerConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: ChargerConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
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
    entities: list[Any] = [
        AutoAreaSelect(entry, controller, auto),
        AutoStrategySelect(entry, controller, auto),
    ]
    entities.extend(
        AutoFiscalPolicySelect(entry, controller, auto, component)
        for component in FISCAL_COMPONENTS
    )
    async_add_entities(entities)


class AutoAreaSelect(SpotNavAutoEntity, SelectEntity):
    """The market whose prices Auto uses, as the relay's own catalogue states it."""

    _attr_entity_category = EntityCategory.CONFIG

    _attr_translation_key = "price_area"

    def __init__(
        self, entry: ConfigEntry, controller: ChargingController, auto: AutoSurface
    ) -> None:
        super().__init__(entry, controller, auto, key="price_area")

    @property
    def options(self) -> list[str]:
        """The catalogue's area ids, plus the configured one when the catalogue cannot say.

        The list is exactly what the relay published; only the already-configured area stays when the
        catalogue is unavailable, so a configured choice is never unrepresentable.
        """
        catalogue = self._auto.catalogue()
        ids = (
            []
            if catalogue is None or catalogue.catalogue is None
            else list(catalogue.catalogue.area_ids)
        )
        settings = self.settings
        chosen = None if settings is None else settings.area_id
        if chosen is not None and chosen not in ids:
            ids.append(chosen)
        return ids

    @property
    def current_option(self) -> str | None:
        settings = self.settings
        return None if settings is None else settings.area_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The catalogue names for the options, and the chosen area's money facts.

        The state stays the machine id; friendly names live in attributes.
        """
        catalogue = self._auto.catalogue()
        names: dict[str, str] = {}
        if catalogue is not None and catalogue.catalogue is not None:
            names = {entry.id: entry.name for entry in catalogue.catalogue.areas}
        chosen = self.area
        return {
            "area_names": names,
            "catalogue_state": None if catalogue is None else catalogue.state,
            "currency": None if chosen is None else chosen.currency,
            "major_unit": None if chosen is None else chosen.major_unit,
            "minor_unit": None if chosen is None else chosen.minor_unit,
            "selected_name": None if chosen is None else chosen.name,
        }

    async def async_added_to_hass(self) -> None:
        """Listen to the shared catalogue, once per entity, so the options stay current."""
        await super().async_added_to_hass()
        manager = self._auto.manager
        if manager is not None:
            self.async_on_remove(manager.add_catalogue_listener(self._on_catalogue))

    @callback
    def _on_catalogue(self, snapshot: CatalogueSnapshot) -> None:
        """The relay's area list changed: rewrite state, on the event loop."""
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Change market: this area's own stored overrides are what the fiscal entities show."""
        await self.async_write_settings(lambda settings: replace(settings, area_id=option))

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


class AutoFiscalPolicySelect(SpotNavAutoEntity, SelectEntity):
    """One fiscal component's three-state policy, for the selected market.

    Per component and market: figures are in the area's own currency, so a policy belongs to that
    area and is re-read on a market switch.
    """

    _attr_entity_category = EntityCategory.CONFIG

    _attr_options = list(FISCAL_OPTIONS)

    def __init__(
        self,
        entry: ConfigEntry,
        controller: ChargingController,
        auto: AutoSurface,
        component: str,
    ) -> None:
        super().__init__(entry, controller, auto, key=f"fiscal_{component}_policy")
        # One translation key per component keeps the three selects distinct entities.
        self._attr_translation_key = f"fiscal_{component}_policy"
        self._component = component

    @property
    def available(self) -> bool:
        """Unavailable without a market (a fiscal policy belongs to one), and for a component the market's
        published price already includes: there is nothing to choose, it is locked as included."""
        return (
            super().available
            and self.override is not None
            and not component_included(self.area, self._component)
        )

    @property
    def current_option(self) -> str | None:
        overrides = self.override
        if overrides is None:
            return None
        return fiscal_policy(getattr(overrides, self._component))

    async def async_select_option(self, option: str) -> None:
        overrides = self.override
        if overrides is None:
            raise ServiceValidationError(
                "Select a market first: a fiscal policy belongs to one.",
                translation_domain=DOMAIN,
                translation_key="invalid_area",
            )
        try:
            replaced = with_fiscal_policy(getattr(overrides, self._component), option)
        except AutoSettingsError as err:
            raise ServiceValidationError(
                str(err), translation_domain=DOMAIN, translation_key=err.code
            ) from err
        await self.async_write_settings(
            lambda settings: settings.with_override(
                replace(overrides, **{self._component: replaced})
            )
        )
