"""`spotnav/release_import_target`: "Release the import target tonight" beside the import target's lines (held, paused,
`import_target_at_risk`) and the notification's button.

`{api_version: 1, charger_id}` sets the site's import target aside for that charger's charge until its plan's
departure, or until the car is unplugged where no departure is ahead (a person's Start, or a plan without one)
(`SiteCapacityController.async_release_import_target`): kept across a restart, ended by the departure or an unplug.
Any user may ask, as any user may start a charge. The answer is `{"api_version": 1, "ok", "error"}`; refusals:
`spotnav_no_site` (the charger has no site), `spotnav_no_import_target` (the site has none set), and the charger
lookup's codes. Logged at INFO.

The charger webhook's `release_import_target` (`webhook_release_import_target`) is the same for the paired app.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback, HomeAssistant

from .common import ERROR_UNSUPPORTED_VERSION, lookup_charger, send_unsupported_version

RELEASE_API_VERSION: Final = 1
ERROR_NO_SITE: Final = "spotnav_no_site"
#: The charger webhook's action for the same.
WEBHOOK_ACTION: Final = "release_import_target"


def _answer(error: str | None = None) -> dict[str, Any]:
    return {"api_version": RELEASE_API_VERSION, "ok": error is None, "error": error}


def release_import_target(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Set the import target aside for this charger until its departure; the refusal's code, else `None`."""
    from ..execution.solar_execution import site_controller_for_charger  # noqa: PLC0415 - it imports the site

    site = site_controller_for_charger(hass, entry.entry_id)
    if site is None:
        return ERROR_NO_SITE
    return site.async_release_import_target(entry.entry_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/release_import_target",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
    }
)
@callback
def websocket_release_import_target(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Set the site's import target aside for this charger's charge until its departure."""
    version = msg.get("api_version")
    if isinstance(version, bool) or version != RELEASE_API_VERSION:
        send_unsupported_version(connection, msg, RELEASE_API_VERSION)
        return
    entry, failure = lookup_charger(hass, msg.get("charger_id"))
    if failure is not None or entry is None:
        connection.send_result(msg["id"], _answer(failure))
        return
    connection.send_result(msg["id"], _answer(release_import_target(hass, entry)))


async def webhook_release_import_target(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> web.Response:
    """`release_import_target` over the charger webhook (the paired app), with an optional `api_version` (1): HTTP 200
    once set aside, 400 with the refusal's code."""
    version = payload.get("api_version", RELEASE_API_VERSION)
    if isinstance(version, bool) or version != RELEASE_API_VERSION:
        error: str | None = ERROR_UNSUPPORTED_VERSION
    else:
        error = release_import_target(hass, entry)
    return web.json_response({**_answer(error), "action": WEBHOOK_ACTION}, status=200 if error is None else 400)


@callback
def async_setup_import_target_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_release_import_target)
