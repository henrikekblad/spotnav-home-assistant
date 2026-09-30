"""Which chargers this instance has -- one definition, two consumers.

A charger is a config entry of this domain with `entry_type: charger` and a webhook; a site entry is
not one. The pairing payload and the dashboard's `chargers` list both walk it, and differ by exactly
one field, which is the security shape of the integration:

* the dashboard reports id and name: the instance's shape, which changes rarely and is discovered on
  every poll;
* the pairing payload adds each charger's webhook id: the authority to control it, which crosses
  once, when a human deliberately pairs.

Nothing here logs, and nothing formats a URL: the secret stays in the entry that owns it until a
caller that is allowed to see it asks.
"""

from __future__ import annotations

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError

from ..const import CONF_ENTRY_TYPE, CONF_WEBHOOK_ID, DOMAIN, ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE


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


def site_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """This instance's site entry, if it has one."""
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_SITE
        ),
        None,
    )


def charger_pairing_payload(hass: HomeAssistant) -> list[dict[str, str]]:
    """Every charger, in creation order, with its webhook secret.

    `id` is the config entry id (stable across renames, so it and not the name identifies a charger),
    `name` the entry title, `webhook` that charger's own secret.
    """
    return [
        {
            "id": entry.entry_id,
            "name": entry.title,
            "webhook": entry.data[CONF_WEBHOOK_ID],
        }
        for entry in charger_entries(hass)
    ]
