"""The site's priority list as Home Assistant holds it (`site/priority.py` is the pure part): read with the chargers'
old `charger_priority` while no list is stored, the rows a site has now, the block the card and the apps read, and
the one write.

**The write** (`store_order`) stores the whole list in the site entry's data and in the running site's config (no
reload: every reader reads the config when it decides), drops the old `solar_priority` from the site and each member
charger's `charger_priority`, and asks the site to work its chargers out again at once. An older app's write of the
old values becomes a change of the list (`site/priority.with_solar_priority`, `with_charger_priority`).

**The rows shown** (`present_ids`): each member charger, and each other row whose load the site has: house heat (a
heat pump path or a floor-heating zone that heats), house cooling (a zone that can cool), the water heater, the home
battery, the pool's heat pump and circulation, the spa (both rows) and the sauna. Rows of loads the site does not
have stay in the stored list at their places.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import CONF_CHARGER_PRIORITY, CONF_PRIORITY, CONF_SOLAR_PRIORITY, DEFAULT_CHARGER_PRIORITY
from .priority import (
    charger_entry_of,
    charger_id,
    derived_charger_priorities,
    effective_order,
    ID_BATTERY,
    ID_COOLING,
    ID_HEAT,
    ID_POOL_CIRCULATION,
    ID_POOL_HEAT,
    ID_SAUNA,
    ID_SPA,
    ID_SPA_IDLE,
    ID_WATER_HEATER,
    members,
    solar_priority,
    stored,
)

_LOGGER = logging.getLogger(__name__)


def charger_ranks(hass: HomeAssistant, config: Mapping[str, Any]) -> dict[str, str]:
    """Each member charger's stored `charger_priority` (read while no list is stored)."""
    out: dict[str, str] = {}
    for entry_id in members(config):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.data.get(CONF_CHARGER_PRIORITY):
            out[entry_id] = str(entry.data[CONF_CHARGER_PRIORITY])
    return out


def _config(hass: HomeAssistant, site: ConfigEntry) -> Mapping[str, Any]:
    """The running site's config where it runs (it is what the readers read), else the entry's data."""
    from ..runtime import site_controller_for  # noqa: PLC0415 - the runtime imports the site

    controller = site_controller_for(hass, site.entry_id)
    return site.data if controller is None else controller.config


def site_order(hass: HomeAssistant, site: ConfigEntry) -> list[str]:
    """The site's whole list now (`priority.effective_order`)."""
    config = _config(hass, site)
    return effective_order(config, charger_ranks(hass, config))


def site_solar_priority(hass: HomeAssistant, site: ConfigEntry) -> str:
    """The old `solar_priority`, derived from the list (an older app reads it)."""
    return solar_priority(_config(hass, site))


def charger_priority_value(hass: HomeAssistant, site: ConfigEntry, charger_entry_id: str) -> str:
    """The old `charger_priority` of a charger on this site: its stored value while no list is stored, else derived
    from the list (`priority.derived_charger_priorities`)."""
    config = _config(hass, site)
    if stored(config) is None:
        entry = hass.config_entries.async_get_entry(charger_entry_id)
        return str((None if entry is None else entry.data.get(CONF_CHARGER_PRIORITY)) or DEFAULT_CHARGER_PRIORITY)
    return derived_charger_priorities(effective_order(config)).get(charger_entry_id, DEFAULT_CHARGER_PRIORITY)


def present_ids(hass: HomeAssistant, site: ConfigEntry) -> set[str]:
    """The rows whose load the site has now (see the module). A part that cannot be read is left out."""
    from ..runtime import site_controller_for  # noqa: PLC0415

    found = {charger_id(entry_id) for entry_id in members(site.data)}
    controller = site_controller_for(hass, site.entry_id)
    if controller is None:
        return found
    try:
        if controller.battery_shadow.facts.battery_entity_id:
            found.add(ID_BATTERY)
    except Exception:  # noqa: BLE001 - no battery known
        _LOGGER.debug("priority: the battery could not be read", exc_info=True)
    try:
        config = controller.heat_shadow.config()
    except Exception:  # noqa: BLE001 - no heat settings known
        _LOGGER.debug("priority: the heat settings could not be read", exc_info=True)
        return found
    found |= _heat_rows(hass, config)
    if getattr(config, "pool", None) is not None:
        found |= {ID_POOL_CIRCULATION, ID_POOL_HEAT}
    if getattr(config, "spa", None) is not None:
        found |= {ID_SPA, ID_SPA_IDLE}
    if getattr(config, "sauna", None) is not None:
        found.add(ID_SAUNA)
    return found


def _heat_rows(hass: HomeAssistant, config: Any) -> set[str]:
    from ..heat.flexible import heat_pump_load  # noqa: PLC0415 - it imports the site

    found: set[str] = set()
    if getattr(config, "water_heater", None) is not None:
        found.add(ID_WATER_HEATER)
    try:
        if heat_pump_load("", config, power_tariff=False) is not None:
            found.add(ID_HEAT)
    except Exception:  # noqa: BLE001 - read as absent
        _LOGGER.debug("priority: the heat loads could not be described", exc_info=True)
    for zone in getattr(config, "zones", ()) or ():
        state = hass.states.get(zone.entity_id)
        modes = () if state is None else (state.attributes.get("hvac_modes") or ())
        current = None if state is None else state.state
        if current == "cool" or "cool" in modes or "heat_cool" in modes:
            found.add(ID_COOLING)
        if current != "cool" or "heat" in modes:
            found.add(ID_HEAT)
    return found


def shown_order(hass: HomeAssistant, site: ConfigEntry) -> list[str]:
    """The rows a person is shown and writes: the list without the loads the site does not have."""
    present = present_ids(hass, site)
    return [item for item in site_order(hass, site) if item in present]


def priority_block(hass: HomeAssistant, site: ConfigEntry, *, writable: bool) -> dict[str, Any]:
    """`priority`: `{order: [{id, name}], writable}`, the rows the site has, top first; `name` is a charger's title
    (`null` for the other rows, which the card names itself)."""
    rows = []
    for item in shown_order(hass, site):
        entry_id = charger_entry_of(item)
        entry = None if entry_id is None else hass.config_entries.async_get_entry(entry_id)
        rows.append({"id": item, "name": None if entry is None else entry.title})
    return {"order": rows, "writable": bool(writable)}


def store_order(hass: HomeAssistant, site: ConfigEntry, order: Sequence[str]) -> None:
    """Store the whole list (see the module): the site entry, the running site's config, the old keys dropped."""
    from ..runtime import site_controller_for  # noqa: PLC0415

    order = list(order)
    data = dict(site.data)
    data[CONF_PRIORITY] = order
    data.pop(CONF_SOLAR_PRIORITY, None)
    hass.config_entries.async_update_entry(site, data=data)
    for entry_id in members(site.data):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and CONF_CHARGER_PRIORITY in entry.data:
            charger_data = dict(entry.data)
            charger_data.pop(CONF_CHARGER_PRIORITY, None)
            hass.config_entries.async_update_entry(entry, data=charger_data)
    controller = site_controller_for(hass, site.entry_id)
    if controller is not None:
        controller.config[CONF_PRIORITY] = order
        controller.config.pop(CONF_SOLAR_PRIORITY, None)
        controller._schedule_recompute()
