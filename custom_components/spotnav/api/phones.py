"""`spotnav/disconnect_phones` (API v1, SpotNav 3.0): "Disconnect all phones", an administrator's one way to revoke
every paired phone at once.

Every charger and every site gets a new webhook id (`entry_migration.new_webhook_id`); the old ones stop answering at
once (Home Assistant's empty answer, which the app reads as "pair again"), including a site id a charger handed over
(`get_site_access`). Each charger's instant-notification registration is cleared, so no phone is woken any more, and
pending pairing requests are dropped (and their approval items in Devices & services withdrawn), so an approval given
just before cannot fetch the new ids. Nothing else
changes: no entry is reloaded and charging goes on. A phone is connected again by pairing it again.

`{type: "spotnav/disconnect_phones", api_version: 1}` answers `{api_version: 1, ok, error, chargers, sites,
notifications_not_cleared}`: `chargers` and `sites` count the webhooks replaced (always every one: the ids are
replaced first, all at once), `notifications_not_cleared` the chargers whose phone notifications could not be cleared
(logged; those phones may still be woken until it is done again). Administrators only: anyone else gets `ok: false`,
`error: "spotnav_not_admin"` and nothing changes. Another `api_version` is `spotnav_unsupported_api_version`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_ENTRY_TYPE, CONF_WEBHOOK_ID, DOMAIN, ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE
from ..entry_migration import new_webhook_id
from ..notifications.push import ChargerPush
from ..runtime import charger_data, domain_data
from .common import ERROR_NOT_ADMIN, is_admin, send_unsupported_version
from .pairing import PairingRegister
from .site_entry_webhook import async_ensure_site_webhook
from .webhook import async_move_charger_webhook

_LOGGER = logging.getLogger(__name__)

PHONES_API_VERSION: Final = 1


def _kind(entry: ConfigEntry) -> str | None:
    if not entry.data.get(CONF_WEBHOOK_ID):
        return None
    kind = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_CHARGER)
    return kind if kind in (ENTRY_TYPE_CHARGER, ENTRY_TYPE_SITE) else None


def _replace_webhook(hass: HomeAssistant, entry: ConfigEntry, kind: str) -> None:
    """Give one entry a new webhook id and answer at the new id only: a site whatever its load state, a charger
    wherever a webhook is registered for it now (also mid-setup); one set up later registers the new id itself."""
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_WEBHOOK_ID: new_webhook_id()})
    if kind == ENTRY_TYPE_SITE:
        async_ensure_site_webhook(hass, entry)
    else:
        async_move_charger_webhook(hass, entry)


async def async_disconnect_phones(hass: HomeAssistant) -> dict[str, int]:
    """Replace every charger's and site's webhook id, then drop the pending pairings and their approval cards, then
    forget every phone's notification registration. The ids come first and all at once (nothing there can fail or
    wait); each later step is on its own, so one that fails is logged and the rest still runs. Returns the counts
    `{chargers, sites, notifications_not_cleared}`."""
    counts = {"chargers": 0, "sites": 0, "notifications_not_cleared": 0}
    chargers: list[ConfigEntry] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        kind = _kind(entry)
        if kind is None:
            continue
        _replace_webhook(hass, entry, kind)
        if kind == ENTRY_TYPE_SITE:
            counts["sites"] += 1
        else:
            counts["chargers"] += 1
            chargers.append(entry)
    # An approval given a moment ago could otherwise fetch the new ids within its re-delivery minute.
    domain = domain_data(hass)
    if domain.pairing is not None:
        domain.pairing = PairingRegister()
    try:
        _abort_approval_cards(hass)
    except Exception:  # noqa: BLE001 - the ids are replaced already; a card left behind only expires when opened
        _LOGGER.warning("SpotNav: a waiting phone approval could not be withdrawn", exc_info=True)
    for entry in chargers:
        try:
            data = charger_data(hass, entry.entry_id)
            push = None if data is None else data.push
            if push is not None:
                await push.async_register(None)
            else:
                await ChargerPush.async_remove_stored(hass, entry.entry_id)
        except Exception:  # noqa: BLE001 - one charger's store must not stop the others
            counts["notifications_not_cleared"] += 1
            _LOGGER.warning(
                "SpotNav: the phone notifications of charger %s could not be cleared", entry.entry_id, exc_info=True
            )
    _LOGGER.info(
        "SpotNav: every phone was disconnected (%d charger and %d site webhooks replaced, %d chargers' notifications "
        "not cleared)",
        counts["chargers"],
        counts["sites"],
        counts["notifications_not_cleared"],
    )
    return counts


def _abort_approval_cards(hass: HomeAssistant) -> None:
    """Withdraw the "A phone wants to connect" items still waiting in Devices & services."""
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        if flow.get("context", {}).get("source") == SOURCE_INTEGRATION_DISCOVERY:
            hass.config_entries.flow.async_abort(flow["flow_id"])


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
            msg["id"],
            {
                "api_version": PHONES_API_VERSION,
                "ok": False,
                "error": ERROR_NOT_ADMIN,
                "chargers": 0,
                "sites": 0,
                "notifications_not_cleared": 0,
            },
        )
        return
    counts = await async_disconnect_phones(hass)
    connection.send_result(msg["id"], {"api_version": PHONES_API_VERSION, "ok": True, "error": None, **counts})


@callback
def async_setup_phones_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_disconnect_phones)
