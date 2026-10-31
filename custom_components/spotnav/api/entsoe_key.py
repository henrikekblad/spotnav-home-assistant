"""`spotnav/set_entsoe_key {key, area_id?}`: the installation's ENTSO-E key, from the card. Administrators only.

`key` is the security token, or `null` (or blank) to clear it. Saving checks it with one request
(`pricing/entsoe_key.py`) and answers `{ok, error, state}`:

* `{ok: true, error: null, state: "ok"}`: stored and accepted (or `state: null` after clearing);
* `{ok: false, error: "entsoe_key_rejected", state}`: the platform rejected it; nothing was stored and `state` is
  the previous key's (or `null`);
* `{ok: false, error: "entsoe_unreachable", state: "unchecked"}`: the platform could not be asked; the key is
  stored and used, and the first day it asks for proves or rejects it.

`area_id` names the area to check against (default: the first electricity price entry's, DE-LU when it has no
route). The key is never answered back.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..pricing.entsoe_key import async_save_entsoe_key
from .common import ERROR_NOT_ADMIN, is_admin


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/set_entsoe_key",
        vol.Required("key"): vol.Any(str, None),
        vol.Optional("area_id"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def websocket_set_entsoe_key(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Check and store (or clear) the installation's ENTSO-E key."""
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only administrators can change the ENTSO-E key")
        return
    saved = await async_save_entsoe_key(hass, msg["key"], area_id=msg.get("area_id"))
    connection.send_result(msg["id"], saved.as_dict())


@callback
def async_setup_entsoe_key_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_set_entsoe_key)
