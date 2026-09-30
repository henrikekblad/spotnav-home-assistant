"""Adding a newly created charger to the installation's one site, once the charger entry exists."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_PHASE_WIRING, DOMAIN

_LOGGER = logging.getLogger(__name__)

_KEY = f"{DOMAIN}_pending_site_joins"


def queue_site_join(
    hass: HomeAssistant, charger_unique_id: str, site_entry_id: str, wiring: dict[str, Any]
) -> None:
    """Remember that the charger about to be created under this unique id joins that site.

    The charger's entry id does not exist until the flow has finished, so the join is applied from
    the charger's own setup (`async_apply_site_join`), in memory only: it is consumed by the setup
    that follows creation at once, and never persisted into the charger's data.
    """
    hass.data.setdefault(_KEY, {})[charger_unique_id] = (site_entry_id, wiring)


async def async_apply_site_join(hass: HomeAssistant, charger: ConfigEntry) -> None:
    """Append a just-created charger and its wiring to the site that was chosen for it, then reload
    that site. Changes nothing else on the site (active control, enabled state, measurement)."""
    pending = hass.data.get(_KEY, {}).pop(charger.unique_id, None) if charger.unique_id else None
    if pending is None:
        return
    site_entry_id, wiring = pending
    site = hass.config_entries.async_get_entry(site_entry_id)
    if site is None or charger.entry_id in (site.data.get(CONF_CHARGER_ENTRY_IDS) or []):
        return
    hass.config_entries.async_update_entry(
        site,
        data={
            **site.data,
            CONF_CHARGER_ENTRY_IDS: [*(site.data.get(CONF_CHARGER_ENTRY_IDS) or []), charger.entry_id],
            CONF_PHASE_WIRING: {
                **(site.data.get(CONF_PHASE_WIRING) or {}),
                charger.entry_id: wiring,
            },
        },
    )
    hass.async_create_task(hass.config_entries.async_reload(site.entry_id))
