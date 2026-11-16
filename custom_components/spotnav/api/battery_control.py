"""The home battery control's switch and status for the card (none shows them yet): `spotnav/battery_control`.

`{api_version: 1, site_id}` (or `charger_id`, which names the charger's site) answers the site's battery control status (`BatteryControl.status`): its options, the
state and the one line under the switch as stable codes, the verification report for the plant's firmware, the
actions it allows, the routine's progress, what the person still has to do (`needs`) and the notes. Any user may read.

`{api_version: 1, site_id, changes: {battery_control_*: bool, battery_mode: str}}` writes some options first (administrators only):
`battery_control_enabled` (the switch), `battery_control_ai_takeover_ack` (the explanation was read),
`battery_mode` (`self_consumption`, `balanced` or `max_profit`: the only one that may sell; the status's `options`
carry it, and the legacy `battery_control_allow_sell`, derived from it, for one release); the owner's `battery_control_shadow_only` and `battery_control_import_limit_acts` too.
Written to the site entry without a reload; turning off hands the plant back before the answer. Refusals are stable
codes in `error`: `spotnav_no_site`, `spotnav_not_admin`, `spotnav_invalid_value`.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..battery import gate
from ..runtime import site_controller_for
from .common import (
    ERROR_NOT_ADMIN,
    is_admin,
    lookup_charger,
    lookup_site,
    send_unsupported_version,
    site_of_charger,
)

_LOGGER = logging.getLogger(__name__)

BATTERY_CONTROL_API_VERSION: Final = 1
ERROR_NO_SITE: Final = "spotnav_no_site"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"


def _answer(status: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
    return {"api_version": BATTERY_CONTROL_API_VERSION, "ok": error is None, "error": error, "status": status}


def _site_of(hass: HomeAssistant, msg: dict[str, Any]) -> ConfigEntry | None:
    """The site the request names: `site_id`, else the site of `charger_id`."""
    if "site_id" in msg:
        entry, _code = lookup_site(hass, msg.get("site_id"), require_loaded=False)
        return entry
    charger, _code = lookup_charger(hass, msg.get("charger_id"), require_loaded=False)
    return None if charger is None else site_of_charger(hass, charger.entry_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/battery_control",
        vol.Optional("api_version"): object,
        vol.Optional("site_id"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("changes"): object,
    }
)
@websocket_api.async_response
async def websocket_battery_control(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The site's battery control status, after writing `changes` when given."""
    version = msg.get("api_version")
    if isinstance(version, bool) or version != BATTERY_CONTROL_API_VERSION:
        send_unsupported_version(connection, msg, BATTERY_CONTROL_API_VERSION)
        return
    entry = _site_of(hass, msg)
    controller = None if entry is None else site_controller_for(hass, entry.entry_id)
    if controller is None:
        connection.send_result(msg["id"], _answer(error=ERROR_NO_SITE))
        return
    control = controller.battery_control
    changes = msg.get("changes")
    if changes is not None:
        if not is_admin(connection):
            connection.send_result(msg["id"], _answer(error=ERROR_NOT_ADMIN))
            return
        if not isinstance(changes, dict) or not changes or any(
            not gate.valid_change(key, value) for key, value in changes.items()
        ):
            connection.send_result(msg["id"], _answer(error=ERROR_INVALID_VALUE))
            return
        connection.send_result(msg["id"], _answer(await control.async_update_options(dict(changes))))
        return
    connection.send_result(msg["id"], _answer(control.status()))


@callback
def async_setup_battery_control_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_battery_control)
