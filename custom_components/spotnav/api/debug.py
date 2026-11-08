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

`spotnav/send_debug_info` (admin only, `require_admin`) sends that same bundle, with the administrator's message
and optional e-mail address, to SpotNav's developer (`support_report.py`) and answers
`{api_version, ok, error, ref, retry_after_s}`: `ref` is the reference (`SN-XXXX`) the person quotes, `error` one
of the `spotnav_report_*` codes, `retry_after_s` the wait when rate limited. Only this command sends anything.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.messages import message_to_json_bytes, result_message
from homeassistant.core import callback, HomeAssistant

from ..card_asset import async_manifest_version, read_bundle_digest
from ..debug_bundle import async_build_debug_bundle
from ..runtime import domain_data
from ..support_report import async_send_report, clean_client, clean_email, clean_message, ERROR_INVALID_EMAIL, ERROR_INVALID_MESSAGE
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
    # The same message `send_result` makes, encoded in the executor: the bundle is about a megabyte.
    message = await hass.async_add_executor_job(
        message_to_json_bytes,
        result_message(msg["id"], {"api_version": DEBUG_API_VERSION, "ok": True, "error": None, "bundle": bundle}),
    )
    connection.send_message(message)


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


def _report_answer(
    *, ref: str | None = None, error: str | None = None, retry_after_s: int | None = None
) -> dict[str, Any]:
    return {
        "api_version": DEBUG_API_VERSION,
        "ok": ref is not None,
        "error": error,
        "ref": ref,
        "retry_after_s": retry_after_s,
    }


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/send_debug_info",
        vol.Optional("api_version"): object,
        vol.Required("message"): object,
        vol.Optional("email"): object,
        vol.Optional("client"): object,
    }
)
@websocket_api.async_response
async def websocket_send_debug_info(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the redacted bundle with the administrator's message to SpotNav's developer (module docstring)."""
    version = msg.get("api_version")
    if type(version) is not int or version != DEBUG_API_VERSION:
        send_unsupported_version(connection, msg, DEBUG_API_VERSION)
        return
    message = clean_message(msg.get("message"))
    if message is None:
        connection.send_result(msg["id"], _report_answer(error=ERROR_INVALID_MESSAGE))
        return
    valid, email = clean_email(msg.get("email"))
    if not valid:
        connection.send_result(msg["id"], _report_answer(error=ERROR_INVALID_EMAIL))
        return
    result = await async_send_report(hass, message, email, clean_client(msg.get("client")))
    connection.send_result(
        msg["id"], _report_answer(ref=result.ref, error=result.error, retry_after_s=result.retry_after_s)
    )


@callback
def async_setup_debug_api(hass: HomeAssistant) -> None:
    """Register the commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_debug_bundle)
    websocket_api.async_register_command(hass, websocket_get_card_info)
    websocket_api.async_register_command(hass, websocket_send_debug_info)
