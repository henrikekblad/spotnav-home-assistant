"""The debug bundle's one command: `spotnav/get_debug_bundle`, admin only.

Returns the same redacted installation-wide bundle as the site entry's diagnostics
(`debug_bundle.async_build_debug_bundle`); the card saves it as `spotnav-debug-<date>.json`. A
non-administrator gets the stable `spotnav_not_admin` code in the success envelope, like the other
write contracts, and no bundle.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..debug_bundle import async_build_debug_bundle
from .common import ERROR_NOT_ADMIN, is_admin, send_unsupported_version

#: This contract's own version.
DEBUG_API_VERSION: Final = 1


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_debug_bundle",
        vol.Optional("api_version"): object,
    }
)
@websocket_api.async_response
async def websocket_get_debug_bundle(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The installation's redacted debug bundle, for administrators."""
    version = msg.get("api_version")
    if type(version) is not int or version != DEBUG_API_VERSION:
        send_unsupported_version(connection, msg, DEBUG_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_result(
            msg["id"], {"api_version": DEBUG_API_VERSION, "ok": False, "error": ERROR_NOT_ADMIN, "bundle": None}
        )
        return
    bundle = await async_build_debug_bundle(hass)
    connection.send_result(
        msg["id"], {"api_version": DEBUG_API_VERSION, "ok": True, "error": None, "bundle": bundle}
    )


@callback
def async_setup_debug_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_debug_bundle)
