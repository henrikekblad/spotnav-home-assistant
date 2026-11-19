"""`spotnav/disconnect_phones` (API v1, SpotNav 3.0): "Disconnect all phones", an administrator's one way to revoke
every paired phone at once.

Every charger and every site gets a new webhook id (`entry_migration.new_webhook_id`); the old ones stop answering at
once (Home Assistant's empty answer, which the app reads as "pair again"), including a site id a charger handed over
(`get_site_access`). Each charger's instant-notification registration is cleared, so no phone is woken any more, and
pending pairing requests are dropped, so an approval given just before cannot fetch the new ids. Nothing else
changes: no entry is reloaded and charging goes on. A phone is connected again by pairing it again.

`{type: "spotnav/disconnect_phones", api_version: 1}` answers `{api_version: 1, ok, error, chargers, sites}`:
`chargers` and `sites` count the webhooks replaced. Administrators only: anyone else gets `ok: false`,
`error: "spotnav_not_admin"` and nothing changes. Another `api_version` is `spotnav_unsupported_api_version`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import webhook, websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_ENTRY_TYPE, CONF_WEBHOOK_ID, DOMAIN, ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE
from ..entry_migration import new_webhook_id
from ..notifications.push import ChargerPush
from ..runtime import charger_data, domain_data
from .common import ERROR_NOT_ADMIN, is_admin, send_unsupported_version
from .pairing import PairingRegister
from .site_entry_webhook import SITE_WEBHOOK_NAME, site_webhook_handler
from .webhook import CHARGER_WEBHOOK_NAME, charger_webhook_handler

_LOGGER = logging.getLogger(__name__)

PHONES_API_VERSION: Final = 1


def _kind(entry: ConfigEntry) -> str | None:
    if not entry.data.get(CONF_WEBHOOK_ID):
        return None
    kind = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_CHARGER)
    return kind if kind in (ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE) else None


def _replace_webhook(hass: HomeAssistant, entry: ConfigEntry, kind: str) -> None:
    """Give one entry a new webhook id and, while it is loaded, answer at the new id only."""
    old = entry.data[CONF_WEBHOOK_ID]
    new = new_webhook_id()
    # The entry's unload unregisters the id its data holds then (`api/webhook.py:async_register_entry_webhook`).
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_WEBHOOK_ID: new})
    webhook.async_unregister(hass, old)
    if entry.state is not ConfigEntryState.LOADED:
        return
    if kind == ENTRY_TYPE_SITE:
        name, handler = SITE_WEBHOOK_NAME, site_webhook_handler(entry)
    else:
        name, handler = CHARGER_WEBHOOK_NAME, charger_webhook_handler(entry)
    webhook.async_register(hass, DOMAIN, name, new, handler, local_only=False, allowed_methods=("POST",))


async def async_disconnect_phones(hass: HomeAssistant) -> dict[str, int]:
    """Replace every charger's and site's webhook id and forget every phone's notification registration. Returns the
    counts `{chargers, sites}`."""
    counts = {"chargers": 0, "sites": 0}
    for entry in hass.config_entries.async_entries(DOMAIN):
        kind = _kind(entry)
        if kind is None:
            continue
        _replace_webhook(hass, entry, kind)
        if kind == ENTRY_TYPE_SITE:
            counts["sites"] += 1
            continue
        counts["chargers"] += 1
        data = charger_data(hass, entry.entry_id)
        push = None if data is None else data.push
        if push is not None:
            await push.async_register(None)
        else:
            await ChargerPush.async_remove_stored(hass, entry.entry_id)
    # An approval given a moment ago could otherwise fetch the new ids within its re-delivery minute.
    domain = domain_data(hass)
    if domain.pairing is not None:
        domain.pairing = PairingRegister()
    _LOGGER.info(
        "SpotNav: every phone was disconnected (%d charger and %d site webhooks replaced)",
        counts["chargers"],
        counts["sites"],
    )
    return counts


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/disconnect_phones",
        vol.Optional("api_version"): object,
    }
)
@websocket_api.async_response
async def websocket_disconnect_phones(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """"Disconnect all phones" (see the module). Administrators only."""
    version = msg.get("api_version")
    if type(version) is not int or version != PHONES_API_VERSION:
        send_unsupported_version(connection, msg, PHONES_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_result(
            msg["id"], {"api_version": PHONES_API_VERSION, "ok": False, "error": ERROR_NOT_ADMIN, "chargers": 0, "sites": 0}
        )
        return
    counts = await async_disconnect_phones(hass)
    connection.send_result(msg["id"], {"api_version": PHONES_API_VERSION, "ok": True, "error": None, **counts})


@callback
def async_setup_phones_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_disconnect_phones)
