"""Which chargers and sites this instance has -- one definition, two consumers.

A charger is a config entry of this domain with `entry_type: charger` and a webhook; a site entry is
not one. The pairing payload and the dashboard's `chargers` list both walk it, and differ by exactly
one field, which is the security shape of the integration:

* the dashboard reports id and name: the instance's shape, which changes rarely and is discovered on
  every poll;
* the pairing payload adds each charger's webhook id: the authority to control it, which crosses
  once, when a human deliberately pairs.

A site (SpotNav 3.0) is handed over the same way: its webhook secret crosses at pairing (`site_pairing_payload`)
and to a charger of it (`get_site_access`), never in the dashboard.

Nothing here logs, and nothing formats a URL: the secret stays in the entry that owns it until a
caller that is allowed to see it asks.
"""

from __future__ import annotations

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError

from ..const import CONF_CHARGER_ENTRY_IDS, CONF_ENTRY_TYPE, CONF_WEBHOOK_ID, DOMAIN, ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE


def webhook_base_url(hass: HomeAssistant, webhook_id: str) -> tuple[str, str]:
    """The instance's base URL and one webhook's full URL, as HA sees them.

    Any registered webhook gives the same base URL (it is the instance's), so callers pass whichever id
    they hold. Returns `("", path)` when Home Assistant has no URL to offer: an empty string is an
    honest "unknown" rather than a guessed address.
    """
    try:
        webhook_url = webhook.async_generate_url(
            hass, webhook_id, allow_internal=True, prefer_external=True
        )
        return webhook_url.rsplit("/api/webhook/", 1)[0], webhook_url
    except NoURLAvailableError:
        return "", webhook.async_generate_path(webhook_id)


def charger_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every charger config entry, in creation order.

    Creation order is what a person expects to see and is deterministic per instance. Site entries and
    entries with no webhook are skipped: they cannot be reached.
    """
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_CHARGER
        and entry.data.get(CONF_WEBHOOK_ID)
    ]


def site_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every site config entry with a webhook (SpotNav 3.0: every site), in creation order."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE and entry.data.get(CONF_WEBHOOK_ID)
    ]


def _site_ids_by_charger(hass: HomeAssistant) -> dict[str, str]:
    """Each charger's site, by entry id (the first site that lists it, as `api/common.py:site_of_charger`)."""
    sites: dict[str, str] = {}
    for site in hass.config_entries.async_entries(DOMAIN):
        if site.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE:
            continue
        for charger_id in site.data.get(CONF_CHARGER_ENTRY_IDS) or []:
            sites.setdefault(charger_id, site.entry_id)
    return sites


def charger_pairing_payload(hass: HomeAssistant) -> list[dict[str, str]]:
    """Every charger, in creation order, with its webhook secret.

    `id` is the config entry id (stable across renames, so it and not the name identifies a charger),
    `name` the entry title, `webhook` that charger's own secret, and `site_id` (SpotNav 3.0, absent for a charger in
    no site) the site that lists it.
    """
    sites = _site_ids_by_charger(hass)
    payload = []
    for entry in charger_entries(hass):
        charger = {"id": entry.entry_id, "name": entry.title, "webhook": entry.data[CONF_WEBHOOK_ID]}
        if entry.entry_id in sites:
            charger["site_id"] = sites[entry.entry_id]
        payload.append(charger)
    return payload


def site_pairing_entry(hass: HomeAssistant, site: ConfigEntry) -> dict[str, object]:
    """One site as the app is handed it (at pairing, and by a charger of it: `get_site_access`): `id` the site entry's
    id, `name` its title, `webhook` the site webhook's secret, and `chargers` its chargers (`{id, name}`, in the site's
    order), each addressed on the site webhook by its `id` as `charger_id`."""
    by_id = {entry.entry_id: entry for entry in charger_entries(hass)}
    return {
        "id": site.entry_id,
        "name": site.title,
        "webhook": site.data[CONF_WEBHOOK_ID],
        "chargers": [
            {"id": charger_id, "name": by_id[charger_id].title}
            for charger_id in site.data.get(CONF_CHARGER_ENTRY_IDS) or []
            if charger_id in by_id
        ],
    }


def site_pairing_payload(hass: HomeAssistant) -> list[dict[str, object]]:
    """Every site, with and without chargers, in creation order (`site_pairing_entry`)."""
    return [site_pairing_entry(hass, site) for site in site_entries(hass)]
