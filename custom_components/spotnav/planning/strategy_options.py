"""Which strategies a charger may be set to right now.

Shared by the picker (`select.AutoStrategySelect`) and the dashboard's `strategy_options`, so
both always offer the same list.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_CHARGER_ENTRY_IDS,
    CONF_ENTRY_TYPE,
    CONF_MEASUREMENT_MODE,
    DOMAIN,
    ENTRY_TYPE_SITE,
    MEASUREMENT_MODE_DERIVED,
)
from .auto_settings import STRATEGY_CHEAPEST, STRATEGY_HYBRID, STRATEGY_SOLAR


def strategy_options_for(hass: HomeAssistant, charger_entry_id: str) -> list[str]:
    """`cheapest` alone, or `cheapest`, `solar` and `hybrid` when the charger's site is loaded with derived measurement."""
    if _site_has_derived_measurement(hass, charger_entry_id):
        return [STRATEGY_CHEAPEST, STRATEGY_SOLAR, STRATEGY_HYBRID]
    return [STRATEGY_CHEAPEST]


def _site_has_derived_measurement(hass: HomeAssistant, charger_entry_id: str) -> bool:
    """Whether the charger's site is loaded and has derived measurement (signed grid power, which solar needs).

    Membership is read from the site's stored `charger_entry_ids`, as `site_binding` does, without importing `dashboard_api`.
    """
    for site_entry in hass.config_entries.async_entries(DOMAIN):
        if site_entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE:
            continue
        if site_entry.state is not ConfigEntryState.LOADED:
            continue
        members = site_entry.data.get(CONF_CHARGER_ENTRY_IDS) or []
        if charger_entry_id not in members:
            continue
        return site_entry.data.get(CONF_MEASUREMENT_MODE) == MEASUREMENT_MODE_DERIVED
    return False
