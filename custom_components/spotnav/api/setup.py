"""`spotnav/dismiss_setup_item {item}`: a "Get started" row answered "Not needed". Administrators only.

The row stays away for the whole installation (`setup_checklist.py`). Answers `{ok: true}`; a row that cannot
be dismissed (the car's charge level) or is unknown is refused with `spotnav_setup_item_not_dismissible`.

The charger webhook's `dismiss_setup_item {item}` (`webhook_dismiss_setup_item`) is the same for the paired app, which
counts as an administrator: `{"ok": true, "action"}`, or HTTP 400 with `{"ok": false, "error", "action"}`.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from ..setup_checklist import async_dismiss
from .common import ERROR_NOT_ADMIN, is_admin

ERROR_NOT_DISMISSIBLE: Final = "spotnav_setup_item_not_dismissible"
#: The charger webhook's action for the same answer.
WEBHOOK_ACTION: Final = "dismiss_setup_item"


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/dismiss_setup_item",
        vol.Required("item"): str,
    }
)
@websocket_api.async_response
async def websocket_dismiss_setup_item(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Keep one optional setup row away for the installation."""
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only administrators can change the setup list")
        return
    if not await async_dismiss(hass, msg["item"]):
        connection.send_error(msg["id"], ERROR_NOT_DISMISSIBLE, "This setup item cannot be dismissed")
        return
    connection.send_result(msg["id"], {"ok": True})


async def webhook_dismiss_setup_item(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    """`dismiss_setup_item` over the charger webhook: the command's own core; an `item` that is not a text is
    refused as an unknown row."""
    item = payload.get("item")
    if not isinstance(item, str) or not await async_dismiss(hass, item):
        return web.json_response({"ok": False, "error": ERROR_NOT_DISMISSIBLE, "action": WEBHOOK_ACTION}, status=400)
    return web.json_response({"ok": True, "action": WEBHOOK_ACTION})


@callback
def async_setup_setup_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_dismiss_setup_item)
