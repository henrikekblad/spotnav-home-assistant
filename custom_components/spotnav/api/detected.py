"""`spotnav/get_detected` (API v1): what SpotNav detected for one site, read-only.

`{api_version: 1, charger_id | site_id}`. Any user who can see the card may read, as the dashboard. The answer is
`site/detected.py:answer` (its docstring has the shape): per kind of source the entity chosen, why, and the
near-misses with why they were not, as stable codes. Built by `site/detected_hass.py` from what the site's parts
already chose; nothing is written. For the card's settings to show later; no card reads it in this release.

Refusals (WebSocket errors): `spotnav_unsupported_api_version`, `spotnav_charger_required` (neither id), the charger
lookup's codes, `spotnav_unknown_site` and `spotnav_no_site` (the charger is in no site, or the site is not loaded).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..site.detected import DETECTED_API_VERSION
from ..site.detected_hass import async_detected
from .common import send_unsupported_version
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_detected",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
    }
)
@websocket_api.async_response
async def websocket_get_detected(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """What SpotNav detected for one site."""
    version = msg.get("api_version")
    if type(version) is not int or version != DETECTED_API_VERSION:
        send_unsupported_version(connection, msg, DETECTED_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
        answer = None if site.state is not ConfigEntryState.LOADED else await async_detected(hass, site)
        if answer is None:
            raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_detected_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_detected)
