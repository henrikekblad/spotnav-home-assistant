"""`spotnav/get_blueprints` (admin only): how many automations use each of SpotNav's blueprints.

`{api_version, ok, error, blueprints: [{file, path, automations}]}`, one row per blueprint in the order the card lists
them: `file` is the name SpotNav installs it under, `path` the blueprint's path as an automation refers to it
(`spotnav/<file>`), and `automations` how many automations use it. The card asks when its Automations list opens. A
separate command rather than a dashboard field, so the app's dashboard is not handed a key it does not know. A
non-administrator gets `spotnav_not_admin` in the envelope and no rows.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..blueprint_install import automations_using, blueprint_path, BLUEPRINT_FILES
from .common import ERROR_NOT_ADMIN, is_admin, send_unsupported_version

#: This contract's own version.
BLUEPRINTS_API_VERSION: Final = 1


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_blueprints",
        vol.Optional("api_version"): object,
    }
)
@callback
def websocket_get_blueprints(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Each blueprint and how many automations use it (module docstring)."""
    version = msg.get("api_version")
    if type(version) is not int or version != BLUEPRINTS_API_VERSION:
        send_unsupported_version(connection, msg, BLUEPRINTS_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_result(
            msg["id"], {"api_version": BLUEPRINTS_API_VERSION, "ok": False, "error": ERROR_NOT_ADMIN, "blueprints": []}
        )
        return
    counts = automations_using(hass)
    connection.send_result(
        msg["id"],
        {
            "api_version": BLUEPRINTS_API_VERSION,
            "ok": True,
            "error": None,
            "blueprints": [
                {"file": name, "path": blueprint_path(name), "automations": counts[name]} for name in BLUEPRINT_FILES
            ],
        },
    )


@callback
def async_setup_blueprints_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_blueprints)
