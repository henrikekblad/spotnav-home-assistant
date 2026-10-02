"""Follow entity renames: an entity id stored in a SpotNav entry is rewritten when the entity is renamed.

Config entries hold entity ids as plain strings (the charge control, the current limit, the status and
measurement entities of a charger or a site). Renaming an entity in Home Assistant changes its id, and
without this the stored id would point at nothing and the charger would silently stop working. Like
Home Assistant's own integrations, the entry is updated and reloaded.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def replace_entity_id(value: Any, old: str, new: str) -> tuple[Any, bool]:
    """`value` with every string equal to `old` replaced by `new`, and whether anything changed.

    Walks dicts and lists; only values are rewritten, never keys.
    """
    if isinstance(value, str):
        return (new, True) if value == old else (value, False)
    if isinstance(value, dict):
        changed = False
        result: dict[Any, Any] = {}
        for key, item in value.items():
            result[key], item_changed = replace_entity_id(item, old, new)
            changed = changed or item_changed
        return (result, True) if changed else (value, False)
    if isinstance(value, list):
        items = [replace_entity_id(item, old, new) for item in value]
        if any(changed for _, changed in items):
            return [item for item, _ in items], True
        return value, False
    return value, False


async def async_follow_rename(hass: HomeAssistant, old: str, new: str) -> list[str]:
    """Rewrite `old` to `new` in every SpotNav entry that stores it; reload those that changed.

    Returns the ids of the entries that changed.
    """
    changed_entries: list[ConfigEntry] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        data, data_changed = replace_entity_id(dict(entry.data), old, new)
        options, options_changed = replace_entity_id(dict(entry.options), old, new)
        if not data_changed and not options_changed:
            continue
        hass.config_entries.async_update_entry(entry, data=data, options=options)
        changed_entries.append(entry)
    for entry in changed_entries:
        _LOGGER.info("Followed a renamed entity in the entry %s", entry.title)
        await hass.config_entries.async_reload(entry.entry_id)
    return [entry.entry_id for entry in changed_entries]


@callback
def async_setup_entity_renames(hass: HomeAssistant) -> CALLBACK_TYPE:
    """Listen for entity renames for the life of Home Assistant; returns the unsubscribe."""

    @callback
    def renamed(event: Event) -> None:
        if event.data.get("action") != "update":
            return
        old = event.data.get("old_entity_id")
        new = event.data.get("entity_id")
        if not isinstance(old, str) or not isinstance(new, str) or old == new:
            return
        hass.async_create_task(async_follow_rename(hass, old, new), "spotnav follow entity rename")

    return hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, renamed)
