"""Answering "which car is plugged in?" and choosing a car's identification sources, over both transports.

* `spotnav/identify_vehicle` (WebSocket, any signed-in user) and the webhook action `identify_vehicle`:
  `{vehicle_id}` answers the charger's open question (`vehicles/identification.py`). The answer is
  `{"api_version": 1, "ok", "error", "identification"}`, `identification` being the dashboard block after the
  answer. Refusals: `spotnav_not_identifying` (no plug-in is being identified), `spotnav_invalid_value` (not one
  of the candidates), `spotnav_unknown_charger`, `spotnav_unsupported_api_version`.
* `spotnav/choose_vehicle_identification` (WebSocket, administrators) and the webhook action of the same name:
  `{vehicle_id, source: "plug" | "location", entity_id}` with `entity_id` one of the source's candidates,
  `"none"` (the car has no such source) or `null` (back to automatic). The answer carries `identification`, the
  vehicle row's block after the write. Refusals: `spotnav_invalid_value`, `spotnav_not_admin` and the entry-level
  ones above.

A webhook refusal is HTTP 400 with the same code.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..runtime import charger_data, ChargerConfigEntry
from ..vehicles.identification_sources import async_choose_source, sources_block
from .common import ERROR_NOT_ADMIN, is_admin, lookup_charger, send_unsupported_version

IDENTIFICATION_API_VERSION: Final = 1
ERROR_NOT_IDENTIFYING: Final = "spotnav_not_identifying"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"


class IdentificationRefusal(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _envelope(**fields: Any) -> dict[str, Any]:
    return {"api_version": IDENTIFICATION_API_VERSION, "ok": True, "error": None, **fields}


def _failure(code: str) -> dict[str, Any]:
    return {"api_version": IDENTIFICATION_API_VERSION, "ok": False, "error": code}


async def async_identify_vehicle(hass: HomeAssistant, entry_id: str, vehicle_id: Any) -> dict[str, Any]:
    """A person's answer to the charger's question: the dashboard block after it, or a refusal by code."""
    data = charger_data(hass, entry_id)
    identifier = None if data is None else data.identifier
    if identifier is None or identifier.dashboard() is None:
        raise IdentificationRefusal(ERROR_NOT_IDENTIFYING)
    if not await identifier.async_answer(vehicle_id):
        raise IdentificationRefusal(ERROR_INVALID_VALUE)
    return _envelope(identification=identifier.dashboard())


async def async_choose_identification(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Record a person's choice of a car's plug or location source: the car's sources after it."""
    vehicle_id = payload.get("vehicle_id")
    try:
        await async_choose_source(hass, vehicle_id, payload.get("source"), payload.get("entity_id"))
    except ValueError:
        raise IdentificationRefusal(ERROR_INVALID_VALUE) from None
    return _envelope(identification=sources_block(hass, vehicle_id))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/identify_vehicle",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("vehicle_id"): object,
    }
)
@websocket_api.async_response
async def websocket_identify_vehicle(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Answer which car is plugged in. Any signed-in user may: the question goes to the household's phones."""

    async def write(entry: Any) -> dict[str, Any]:
        return await async_identify_vehicle(hass, entry.entry_id, msg.get("vehicle_id"))

    await _ws_write_for(hass, connection, msg, write, admin_only=False)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/choose_vehicle_identification",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("vehicle_id"): object,
        vol.Optional("source"): object,
        vol.Optional("entity_id"): object,
    }
)
@websocket_api.async_response
async def websocket_choose_vehicle_identification(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Choose a car's plug or location source, or none, or automatic. Administrators only."""

    async def write(_entry: Any) -> dict[str, Any]:
        return await async_choose_identification(hass, msg)

    await _ws_write_for(hass, connection, msg, write)


async def _ws_write_for(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    write: Any,
    *,
    admin_only: bool = True,
) -> None:
    version = msg.get("api_version")
    if isinstance(version, bool) or version != IDENTIFICATION_API_VERSION:
        send_unsupported_version(connection, msg, IDENTIFICATION_API_VERSION)
        return
    if admin_only and not is_admin(connection):
        connection.send_result(msg["id"], _failure(ERROR_NOT_ADMIN))
        return
    entry, failure = lookup_charger(hass, msg.get("charger_id"))
    if failure is not None or entry is None:
        connection.send_result(msg["id"], _failure(failure or ERROR_INVALID_VALUE))
        return
    try:
        answer = await write(entry)
    except IdentificationRefusal as refusal:
        connection.send_result(msg["id"], _failure(refusal.code))
        return
    connection.send_result(msg["id"], answer)


async def webhook_identify_vehicle(
    hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]
) -> web.Response:
    """The app's answer to the charger's question (the webhook is bound to its charger)."""
    action = "identify_vehicle"
    try:
        answer = await async_identify_vehicle(hass, entry.entry_id, payload.get("vehicle_id"))
    except IdentificationRefusal as refusal:
        return web.json_response({**_failure(refusal.code), "action": action}, status=400)
    return web.json_response({**answer, "action": action})


async def webhook_choose_vehicle_identification(
    hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]
) -> web.Response:
    """The app's choice of a car's plug or location source."""
    action = "choose_vehicle_identification"
    try:
        answer = await async_choose_identification(hass, payload)
    except IdentificationRefusal as refusal:
        return web.json_response({**_failure(refusal.code), "action": action}, status=400)
    return web.json_response({**answer, "action": action})


@callback
def async_setup_identification_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_identify_vehicle)
    websocket_api.async_register_command(hass, websocket_choose_vehicle_identification)
