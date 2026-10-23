"""Adding and editing an electricity price: its area, then its three fees.

The fees mean what a charger's mean (`pricing/price_points.py`): nothing, the area's suggestion, or a
figure of the person's own, stored as `FiscalOverride`. A fee the area's published price already holds
is not asked for and is stored off. The area list is the relay catalogue the chargers use.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component

from ..const import CONF_ENTRY_TYPE, CONF_PRICE_AREA, DOMAIN, ENTRY_TYPE_PRICE
from ..planning.first_run import suggest_area
from ..pricing.price_points import (
    default_fee_mode,
    FEE_COMPONENTS,
    FEE_MANUAL,
    FEE_MODES,
    FEE_OFF,
    FEE_SUGGESTED,
    fee_mode,
    fee_override,
    included,
    overrides_from_data,
    suggestion,
)
from ..pricing.relay_contract import AreaCatalogue, AreaEntry
from ..runtime import domain_data


_LOGGER = logging.getLogger(__name__)

#: The largest fee a form accepts, in the area's minor unit per kWh (the charger's number allows the same).
MAX_FEE = 10000.0


def default_title(area_id: str) -> str:
    return f"Electricity price {area_id}"


async def async_price_catalogue(hass: HomeAssistant) -> AreaCatalogue | None:
    """The relay's area list, fetched once if it is not held yet, or `None` when there is none.

    An electricity price can be the installation's first SpotNav entry, before the integration's own
    setup has created the price manager: that setup runs first (once; it is what any entry would start).
    """
    if domain_data(hass).price_refresh is None:
        await async_setup_component(hass, DOMAIN, {})
    manager = domain_data(hass).price_refresh
    if manager is None:
        return None
    try:
        await manager.async_ensure_catalogue()
    except Exception as err:  # noqa: BLE001 - no list is a reason to stop the flow, not to fail it
        _LOGGER.debug("The price area list could not be read: %s", err)
    return manager.catalogue_snapshot().catalogue


def area_schema(catalogue: AreaCatalogue, default: str | None) -> vol.Schema:
    options = [
        selector.SelectOptionDict(value=area.id, label=f"{area.id} – {area.name}") for area in catalogue.areas
    ]
    field = vol.Required(CONF_PRICE_AREA, default=default) if default else vol.Required(CONF_PRICE_AREA)
    return vol.Schema(
        {
            field: selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
            )
        }
    )


def _unit(area: AreaEntry, component: str) -> str:
    return "%" if component == "vat" else f"{area.minor_unit}/kWh"


def fee_placeholders(area: AreaEntry) -> dict[str, str]:
    """The area and each fee's suggestion as text for the form's description ("–" where there is none)."""
    placeholders = {"area": f"{area.id} – {area.name}"}
    for component in FEE_COMPONENTS:
        value = suggestion(area, component)
        if included(area, component):
            text = "✓"
        elif value is None:
            text = "–"
        else:
            text = f"{value:g} {_unit(area, component)}"
        placeholders[f"{component}_suggestion"] = text
    return placeholders


def fee_defaults(area: AreaEntry, stored: dict[str, Any] | None) -> dict[str, tuple[str, float | None]]:
    """Each fee's mode and figure to show: what is stored for this area, else the area's suggestion."""
    if stored is not None and stored.get(CONF_PRICE_AREA) == area.id:
        overrides = overrides_from_data(area.id, stored)
        return {
            component: (fee_mode(getattr(overrides, component)), getattr(overrides, component).value)
            for component in FEE_COMPONENTS
        }
    return {component: (default_fee_mode(area, component), None) for component in FEE_COMPONENTS}


def fees_schema(area: AreaEntry, defaults: dict[str, tuple[str, float | None]]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    for component in FEE_COMPONENTS:
        if included(area, component):
            continue
        mode, value = defaults[component]
        fields[vol.Required(f"{component}_mode", default=mode)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(FEE_MODES), translation_key="fee_mode", mode=selector.SelectSelectorMode.DROPDOWN
            )
        )
        key = vol.Optional(f"{component}_value", description={"suggested_value": value}) if value is not None else (
            vol.Optional(f"{component}_value")
        )
        fields[key] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=100 if component == "vat" else MAX_FEE,
                step="any",
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement=_unit(area, component),
            )
        )
    return vol.Schema(fields)


def parse_fees(area: AreaEntry, user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The stored fees from one form, or the errors that keep it open."""
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for component in FEE_COMPONENTS:
        if included(area, component):
            data[component] = fee_override(FEE_OFF, None).as_dict()
            continue
        mode = user_input.get(f"{component}_mode", FEE_OFF)
        value = user_input.get(f"{component}_value")
        if mode == FEE_MANUAL and value is None:
            errors[f"{component}_value"] = "fee_value_required"
        elif mode == FEE_SUGGESTED and suggestion(area, component) is None:
            errors[f"{component}_mode"] = "fee_no_suggestion"
        data[component] = fee_override(mode, value).as_dict()
    return data, errors


class PriceConfigSteps:
    """The config flow's two steps for a new electricity price (mixed into the integration's config flow)."""

    hass: HomeAssistant
    _price_catalogue: AreaCatalogue | None = None
    _price_area: AreaEntry | None = None

    async def async_step_price(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which price area."""
        catalogue = self._price_catalogue = await async_price_catalogue(self.hass)
        if catalogue is None or not catalogue.areas:
            return self.async_abort(reason="no_price_areas")  # type: ignore[attr-defined]
        if user_input is not None:
            self._price_area = catalogue.area(user_input[CONF_PRICE_AREA])
            if self._price_area is not None:
                return await self.async_step_price_fees()
        config = self.hass.config
        default = suggest_area(catalogue, config.country, config.latitude, config.longitude)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="price", data_schema=area_schema(catalogue, default)
        )

    async def async_step_price_fees(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The three fees for that area."""
        area = self._price_area
        if area is None:
            return await self.async_step_price()
        errors: dict[str, str] = {}
        if user_input is not None:
            fees, errors = parse_fees(area, user_input)
            if not errors:
                return self.async_create_entry(  # type: ignore[attr-defined]
                    title=default_title(area.id),
                    data={CONF_ENTRY_TYPE: ENTRY_TYPE_PRICE, CONF_PRICE_AREA: area.id, **fees},
                )
        schema = fees_schema(area, fee_defaults(area, None))
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)  # type: ignore[attr-defined]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="price_fees",
            data_schema=schema,
            errors=errors,
            description_placeholders=fee_placeholders(area),
        )


class PriceOptionsFlow(config_entries.OptionsFlow):
    """Change an electricity price's area and fees. Saving reloads the entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._area: AreaEntry | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Options steps share one namespace of texts with the charger's (`init`): this flow's are `price_*`."""
        return await self.async_step_price_init(user_input)

    async def async_step_price_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        catalogue = await async_price_catalogue(self.hass)
        if catalogue is None or not catalogue.areas:
            return self.async_abort(reason="no_price_areas")
        if user_input is not None:
            self._area = catalogue.area(user_input[CONF_PRICE_AREA])
            if self._area is not None:
                return await self.async_step_price_fees()
        return self.async_show_form(
            step_id="price_init", data_schema=area_schema(catalogue, self._entry.data.get(CONF_PRICE_AREA))
        )

    async def async_step_price_fees(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        area = self._area
        if area is None:
            return await self.async_step_price_init()
        errors: dict[str, str] = {}
        if user_input is not None:
            fees, errors = parse_fees(area, user_input)
            if not errors:
                previous = self._entry.data.get(CONF_PRICE_AREA)
                title = self._entry.title
                if previous and title == default_title(previous):
                    # A name SpotNav gave follows the area; a name the person gave stays.
                    title = default_title(area.id)
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, CONF_PRICE_AREA: area.id, **fees},
                    title=title,
                )
                await self.hass.config_entries.async_reload(self._entry.entry_id)
                return self.async_create_entry(title="", data={})
        schema = fees_schema(area, fee_defaults(area, dict(self._entry.data)))
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="price_fees", data_schema=schema, errors=errors, description_placeholders=fee_placeholders(area)
        )
