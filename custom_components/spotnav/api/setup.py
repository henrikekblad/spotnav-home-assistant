"""`spotnav/dismiss_setup_item {item}`: a "Get started" row answered "Not needed". Administrators only.

The row stays away for the whole installation (`setup_checklist.py`). Answers `{ok: true}`; a row that cannot
be dismissed (the car's charge level) or is unknown is refused with `spotnav_setup_item_not_dismissible`.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..setup_checklist import async_dismiss
from .common import ERROR_NOT_ADMIN, is_admin

ERROR_NOT_DISMISSIBLE: Final = "spotnav_setup_item_not_dismissible"


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


@callback
def async_setup_setup_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_dismiss_setup_item)
