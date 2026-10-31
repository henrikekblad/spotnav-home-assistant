"""`spotnav/fix_own_mode`: the card's "Set to …" and "Turn on" beside a charger's own setting that works
against SpotNav (the status lines `charger_own_mode` and `charger_disabled`).

`{api_version: 1, charger_id, entity_id, value}` writes `value` to `entity_id` only when, read again now, that
entity is still one of the charger's conflicts (`execution/charger_entities.charger_conflicts`: an entity of the
charger's own device and integration that a profile rule names, which reads as on, or its own enable switch that
reads off) and `value` is the one value that conflict's fix writes. Nothing the client names is written
otherwise: an entity of another device, one no rule names, a conflict that has already ended or a value other than
the fix's are all `spotnav_no_conflict`. A select gets `select.select_option`, a switch `switch.turn_off` (or
`turn_on` for the enable switch). Administrators only. The answer is `{"api_version": 1, "ok", "error"}`; refusals:
`spotnav_not_admin`, `spotnav_no_conflict`, `spotnav_own_mode_failed` (the charger's integration refused the
write), and the charger lookup's codes. Each write is logged at INFO.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..execution.charger_entities import charger_conflicts, FixableConflict
from ..runtime import controller_for
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger, send_unsupported_version

_LOGGER = logging.getLogger(__name__)

OWN_MODE_API_VERSION: Final = 1
ERROR_NO_CONFLICT: Final = "spotnav_no_conflict"
ERROR_FAILED: Final = "spotnav_own_mode_failed"


def _answer(error: str | None = None) -> dict[str, Any]:
    return {"api_version": OWN_MODE_API_VERSION, "ok": error is None, "error": error}


def _service_call(conflict: FixableConflict) -> tuple[str, str, dict[str, Any]] | None:
    """The one service call that writes the conflict's fix, or `None` where it has none."""
    domain = conflict.entity_id.split(".", 1)[0]
    if conflict.value is None:
        return None
    if domain == "select":
        return "select", "select_option", {"entity_id": conflict.entity_id, "option": conflict.value}
    if domain == "switch" and conflict.value in ("on", "off"):
        return "switch", f"turn_{conflict.value}", {"entity_id": conflict.entity_id}
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/fix_own_mode",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("entity_id"): object,
        vol.Optional("value"): object,
    }
)
@websocket_api.async_response
async def websocket_fix_own_mode(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Set one of the charger's own settings back to the value SpotNav works with. `ok` means Home Assistant
    has written it; the status line clears itself once the entity reads the new value.
    """
    version = msg.get("api_version")
    if isinstance(version, bool) or version != OWN_MODE_API_VERSION:
        send_unsupported_version(connection, msg, OWN_MODE_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_result(msg["id"], _answer(ERROR_NOT_ADMIN))
        return
    entry, failure = lookup_charger(hass, msg.get("charger_id"))
    if failure is not None or entry is None:
        connection.send_result(msg["id"], _answer(failure))
        return
    controller = controller_for(hass, entry.entry_id)
    entity_id, value = msg.get("entity_id"), msg.get("value")
    conflict = (
        None
        if controller is None or not isinstance(entity_id, str) or not isinstance(value, str)
        else next(
            (
                found
                for found in charger_conflicts(hass, controller.charge_control, controller.adapter.platform)
                if found.entity_id == entity_id and found.value == value
            ),
            None,
        )
    )
    call = None if conflict is None else _service_call(conflict)
    if conflict is None or call is None:
        connection.send_result(msg["id"], _answer(ERROR_NO_CONFLICT))
        return
    domain, service, data = call
    _LOGGER.info(
        "Setting %s from %s to %s for charger %s, as a person asked from the card",
        conflict.entity_id,
        conflict.state,
        conflict.value,
        entry.title,
    )
    try:
        await hass.services.async_call(domain, service, data, blocking=True)
    except Exception as err:  # noqa: BLE001 - logged by type, answered by a stable code
        _LOGGER.warning("Setting %s to %s failed (%s)", conflict.entity_id, conflict.value, type(err).__name__)
        connection.send_result(msg["id"], _answer(ERROR_FAILED))
        return
    connection.send_result(msg["id"], _answer())


@callback
def async_setup_own_mode_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_fix_own_mode)
