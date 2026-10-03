"""Adding a newly created charger to the installation's one site, once the charger entry exists."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_MEASURED_CURRENT_SOURCE, CONF_PHASE_WIRING, DOMAIN
from ..planning.grid_voltage import stored_voltage_between_phases_v
from ..vehicles.discovery import profile_attribute_source
from .measurement_source import source_to_dict

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


def _without(site: ConfigEntry, charger_entry_ids: set[str]) -> dict[str, Any] | None:
    """The site's data without these chargers and their wiring, or `None` when it has none of them."""
    members = list(site.data.get(CONF_CHARGER_ENTRY_IDS) or [])
    if not charger_entry_ids.intersection(members):
        return None
    wiring = {key: value for key, value in (site.data.get(CONF_PHASE_WIRING) or {}).items() if key not in charger_entry_ids}
    return {
        **site.data,
        CONF_CHARGER_ENTRY_IDS: [member for member in members if member not in charger_entry_ids],
        CONF_PHASE_WIRING: wiring,
    }


def async_leave_sites(hass: HomeAssistant, charger_entry_id: str) -> None:
    """Take a charger entry that was deleted for good out of every site that lists it, with its wiring,
    and reload those sites. A member left behind would be shown by its id, keep its phase wiring and
    hold back the regulator of the chargers that share its phases.
    """
    for site in hass.config_entries.async_entries(DOMAIN):
        data = _without(site, {charger_entry_id})
        if data is None:
            continue
        hass.config_entries.async_update_entry(site, data=data)
        hass.async_create_task(hass.config_entries.async_reload(site.entry_id))


def prune_missing_members(hass: HomeAssistant, site: ConfigEntry) -> bool:
    """Drop members whose charger entry no longer exists (deleted by a version that left them behind).
    `True` when the site's data changed; the caller reloads it. Called once Home Assistant has
    started, when every config entry is known.
    """
    missing = {
        member
        for member in site.data.get(CONF_CHARGER_ENTRY_IDS) or []
        if hass.config_entries.async_get_entry(member) is None
    }
    data = _without(site, missing) if missing else None
    if data is None:
        return False
    _LOGGER.info("Site %s: removing chargers that no longer exist: %s", site.entry_id, sorted(missing))
    hass.config_entries.async_update_entry(site, data=data)
    return True


#: Set on a charger's wiring when a person removed its measured-current source, so it is not filled in again.
MEASURED_SOURCE_DECLINED = "measured_source_declined"


def backfill_profile_measured_sources(hass: HomeAssistant, site: ConfigEntry) -> dict[str, Any] | None:
    """The site's data with a measured-current source filled in for every member charger whose wiring
    has none and whose platform profile can name one (Easee: the `current` sensor's terminal
    attributes), or `None` when nothing changes. A source a person set, or removed on purpose, is left
    as it is. Logs once per charger it fills in (a later start finds the source and does nothing).
    """
    # Imported here: the flows import this module's neighbours, and they import the controller.
    from ..flows.measured_source import charger_device_id

    wiring_by_charger = site.data.get(CONF_PHASE_WIRING) or {}
    voltage = stored_voltage_between_phases_v(site.data)
    updated: dict[str, Any] = {}
    for charger_entry_id in site.data.get(CONF_CHARGER_ENTRY_IDS) or []:
        wiring = wiring_by_charger.get(charger_entry_id)
        if not isinstance(wiring, dict) or wiring.get(CONF_MEASURED_CURRENT_SOURCE) or wiring.get(MEASURED_SOURCE_DECLINED):
            continue
        device_id = charger_device_id(hass, charger_entry_id)
        if device_id is None:
            continue
        phases = wiring.get("phases")
        source = profile_attribute_source(
            hass,
            device_id,
            voltage_between_phases_v=voltage,
            phases=1 if phases == 1 else 3,
            phase=wiring.get("phase"),
            require_state=False,
        )
        if source is None:
            continue
        updated[charger_entry_id] = {**wiring, CONF_MEASURED_CURRENT_SOURCE: source_to_dict(source)}
        _LOGGER.info(
            "Site %s: charger %s now reads its measured current from the attributes of %s",
            site.entry_id,
            charger_entry_id,
            source.entity_id,
        )
    if not updated:
        return None
    return {**site.data, CONF_PHASE_WIRING: {**wiring_by_charger, **updated}}
