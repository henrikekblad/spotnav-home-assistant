"""Config entry migrations (`async_migrate_entry`): what a newer minor version adds to an entry an older one wrote.

Minor 2 (SpotNav 3.0) gives every site entry its own webhook id, the site webhook's secret (`api/site_entry_webhook.py`).
A charger keeps the id it has; an electricity price has none. The migration runs before the entry is set up and
writes no other key, so a downgrade to a release that knows minor 1 loads the entry as it was (Home Assistant refuses
only a higher major version) and an upgrade again keeps the id.
"""

from __future__ import annotations

import logging
import secrets

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ENTRY_TYPE,
    CONF_WEBHOOK_ID,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    ENTRY_TYPE_SITE,
    WEBHOOK_ID_BYTES,
)

_LOGGER = logging.getLogger(__name__)


def new_webhook_id() -> str:
    """A fresh webhook secret, for a charger or a site."""
    return secrets.token_urlsafe(WEBHOOK_ID_BYTES)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry of an older minor version up to `CONFIG_ENTRY_MINOR_VERSION`. A higher major version never
    reaches here (Home Assistant refuses it); a higher minor version is left as it is."""
    if entry.version != CONFIG_ENTRY_VERSION or entry.minor_version >= CONFIG_ENTRY_MINOR_VERSION:
        return True
    data = dict(entry.data)
    if data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE and not data.get(CONF_WEBHOOK_ID):
        # Never logged: the id is the site webhook's secret.
        data[CONF_WEBHOOK_ID] = new_webhook_id()
        _LOGGER.debug("SpotNav site %s got its own webhook", entry.entry_id)
    hass.config_entries.async_update_entry(entry, data=data, minor_version=CONFIG_ENTRY_MINOR_VERSION)
    return True
