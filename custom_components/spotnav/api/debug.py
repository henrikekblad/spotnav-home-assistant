"""The debug bundle's command, `spotnav/get_debug_bundle` (admin only), and `spotnav/get_card_info`.

`spotnav/get_debug_bundle` returns the same redacted installation-wide bundle as the site entry's diagnostics
(`debug_bundle.async_build_debug_bundle`); the card saves it as `spotnav-debug-<date>.json`. A
non-administrator gets the stable `spotnav_not_admin` code in the success envelope, like the other
write contracts, and no bundle.

`spotnav/get_card_info` (any signed-in user) answers which card the integration serves:
`{api_version, ok, error, spotnav_version, card_bundle_hash}`, the hash being the one in the card URL Home
Assistant hands the browsers (the bundle file's own hash before the card is served). The card compares it
with the hash in the URL it was itself loaded from and, when they differ, says the browser or the
Companion app runs an older card. A separate command, not a dashboard field, so a card that predates it
is never handed a key it does not know.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..card_asset import async_manifest_version, read_bundle_digest
from ..debug_bundle import async_build_debug_bundle
from ..runtime import domain_data
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


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_card_info",
        vol.Optional("api_version"): object,
    }
)
@websocket_api.async_response
async def websocket_get_card_info(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The version and the bundle hash of the card the integration serves (module docstring)."""
    version = msg.get("api_version")
    if type(version) is not int or version != DEBUG_API_VERSION:
        send_unsupported_version(connection, msg, DEBUG_API_VERSION)
        return
    manifest_version = await async_manifest_version(hass)
    digest = domain_data(hass).card_served_digest
    if digest is None:
        digest = await hass.async_add_executor_job(read_bundle_digest)
    connection.send_result(
        msg["id"],
        {
            "api_version": DEBUG_API_VERSION,
            "ok": True,
            "error": None,
            "spotnav_version": None if manifest_version is None else str(manifest_version),
            "card_bundle_hash": digest,
        },
    )


@callback
def async_setup_debug_api(hass: HomeAssistant) -> None:
    """Register the commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_debug_bundle)
    websocket_api.async_register_command(hass, websocket_get_card_info)
