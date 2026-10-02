"""The voltage between two phases a charger's power is figured from.

A site holds it for its chargers (`CONF_VOLTAGE_BETWEEN_PHASES_V` on the site entry); a charger with no
site holds its own on its entry. 400 V is the TN network default and 230 V an IT network (much of
Norway), where three-phase power is `sqrt(3) x 230 V x I`, not `sqrt(3) x 400 V x I`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant

from ..const import (
    CONF_VOLTAGE_BETWEEN_PHASES_V,
    DEFAULT_VOLTAGE_BETWEEN_PHASES_V,
    DOMAIN,
    VOLTAGE_BETWEEN_PHASES_CHOICES,
    VOLTAGE_BETWEEN_PHASES_IT_V,
)
from .first_run import site_for_charger


def stored_voltage_between_phases_v(data: Mapping[str, Any] | None) -> float:
    """The stored choice of an entry's data; anything but a known choice is the default."""
    value = None if data is None else data.get(CONF_VOLTAGE_BETWEEN_PHASES_V)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V)
    return float(value) if value in VOLTAGE_BETWEEN_PHASES_CHOICES else float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V)


def voltage_between_phases_v(hass: HomeAssistant, charger_entry_id: str) -> float:
    """This charger's voltage between phases: its site's, else its own entry's, else 400 V."""
    site = site_for_charger(hass, charger_entry_id)
    if site is not None:
        return stored_voltage_between_phases_v(site.data)
    entry = hass.config_entries.async_get_entry(charger_entry_id)
    if entry is None or entry.domain != DOMAIN:
        return float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V)
    return stored_voltage_between_phases_v(entry.data)


#: Countries whose homes are often on an IT network (230 V between phases): the form suggests 230 V.
IT_NETWORK_COUNTRIES = frozenset({"NO"})


def default_voltage_between_phases_v(hass: HomeAssistant) -> float:
    """What a new site or charger suggests: 230 V in Norway, else the TN default of 400 V."""
    country = hass.config.country
    if isinstance(country, str) and country.upper() in IT_NETWORK_COUNTRIES:
        return float(VOLTAGE_BETWEEN_PHASES_IT_V)
    return float(DEFAULT_VOLTAGE_BETWEEN_PHASES_V)
