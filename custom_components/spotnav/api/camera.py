"""The charger's camera for vehicle identification, over both transports: snapshot, frame, reference pictures.

Every command is an administrator's (WebSocket) or the paired app's (webhook, bound to its charger), at
`api_version` 1, and answers `{"api_version": 1, "ok", "error", ...}`. Pictures travel as
`{"content_type": "image/jpeg", "data": <base64>, "width", "height"}`; nothing is image-processed by a client.

* `spotnav/camera_snapshot` / webhook `camera_snapshot`: `{camera_entity_id?}` (default the chosen camera) ->
  `picture`, the camera's whole picture now, for drawing the frame on.
* `spotnav/save_camera_frame` / `save_camera_frame`: `{frame: {x, y, w, h} | null}` -> `identify_camera`, the
  settings' camera after the write (the rest of the settings untouched; `null` is the whole picture).
* `spotnav/take_reference_picture` / `take_reference_picture`: `{vehicle_id, kind: "day" | "night"}` ->
  `references`, that car's reference pictures after it (`[{kind, taken_at, colour}]`).
* `spotnav/delete_reference_picture` / `delete_reference_picture`: `{vehicle_id, kind: "day" | "night" | null}`
  (`null`: every one of that car's) -> `references`.
* `spotnav/reference_picture` / `reference_picture`: `{vehicle_id, kind}` -> `picture`, a thumbnail.

Refusals: `spotnav_no_camera` (no camera is chosen, or it is not there), `spotnav_no_picture` (the camera gave
none), `spotnav_invalid_value` (not one of this charger's cars, a kind or frame that is not one, no such
picture), `spotnav_not_admin`, `spotnav_unknown_charger`, `spotnav_unsupported_api_version`; over the webhook
HTTP 400 with the same code.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant

from ..planning.auto_controller import SettingsReconcileError
from ..planning.auto_settings import AutoSettings
from ..runtime import charger_data, ChargerConfigEntry, domain_data, preview_for
from ..vehicles.camera_identification import CameraIdentification, CameraUnavailable
from ..vehicles.camera_pictures import picture_size, valid_vehicle_id
from ..vehicles.camera_rule import PICTURE_KINDS
from ..vehicles.camera_settings import CameraSettingsError, Frame
from ..vehicles.vehicle_discovery import resolve_target_vehicle
from .common import ERROR_CHARGER_UNLOADED, ERROR_NOT_ADMIN, is_admin, lookup_charger, send_unsupported_version

CAMERA_API_VERSION: Final = 1
ERROR_NO_CAMERA: Final = "spotnav_no_camera"
ERROR_NO_PICTURE: Final = "spotnav_no_picture"
ERROR_INVALID_VALUE: Final = "spotnav_invalid_value"

type Command = Callable[[HomeAssistant, str, dict[str, Any]], Awaitable[dict[str, Any]]]


class CameraRefusal(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _envelope(**fields: Any) -> dict[str, Any]:
    return {"api_version": CAMERA_API_VERSION, "ok": True, "error": None, **fields}


def _failure(code: str) -> dict[str, Any]:
    return {"api_version": CAMERA_API_VERSION, "ok": False, "error": code}


def _picture(jpeg: bytes, width: int, height: int) -> dict[str, Any]:
    return {
        "content_type": "image/jpeg",
        "data": base64.b64encode(jpeg).decode("ascii"),
        "width": width,
        "height": height,
    }


def _camera(hass: HomeAssistant, entry_id: str) -> CameraIdentification:
    data = charger_data(hass, entry_id)
    camera = None if data is None else data.camera
    if camera is None:
        raise CameraRefusal(ERROR_CHARGER_UNLOADED)
    return camera


def _vehicle(hass: HomeAssistant, entry_id: str, value: Any) -> str:
    """One of this charger's cars (`vehicle_ids`), or a refusal."""
    store = domain_data(hass).auto_store
    if store is None or not valid_vehicle_id(value):
        raise CameraRefusal(ERROR_INVALID_VALUE)
    settings = store.settings(entry_id)
    _, choices = resolve_target_vehicle(hass, settings.target.vehicle_id, settings.vehicle_ids)
    if value not in {choice.id for choice in choices}:
        raise CameraRefusal(ERROR_INVALID_VALUE)
    return value


def _kind(value: Any, *, allow_all: bool = False) -> str | None:
    if allow_all and value is None:
        return None
    if value not in PICTURE_KINDS:
        raise CameraRefusal(ERROR_INVALID_VALUE)
    return value


def _references(camera: CameraIdentification, vehicle_id: str) -> list[dict[str, Any]]:
    settings = camera.settings()
    if settings is None:
        return []
    return [item.as_wire() for item in camera.references.references(vehicle_id, settings.camera_entity_id)]


async def async_camera_snapshot(hass: HomeAssistant, entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    camera = _camera(hass, entry_id)
    requested = payload.get("camera_entity_id")
    if requested is not None and not isinstance(requested, str):
        raise CameraRefusal(ERROR_INVALID_VALUE)
    try:
        snapshot = await camera.async_snapshot(requested)
    except CameraUnavailable as err:
        raise CameraRefusal(ERROR_NO_CAMERA if err.code == "no_camera" else ERROR_NO_PICTURE) from None
    return _envelope(picture=_picture(snapshot.jpeg, snapshot.width, snapshot.height))


async def async_save_camera_frame(hass: HomeAssistant, entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    camera = _camera(hass, entry_id)
    if "frame" not in payload:
        raise CameraRefusal(ERROR_INVALID_VALUE)
    try:
        frame = None if payload["frame"] is None else Frame.from_wire(payload["frame"])
    except CameraSettingsError:
        raise CameraRefusal(ERROR_INVALID_VALUE) from None
    if camera.settings() is None:
        raise CameraRefusal(ERROR_NO_CAMERA)
    controller = preview_for(hass, entry_id)
    if controller is None:
        raise CameraRefusal(ERROR_CHARGER_UNLOADED)

    def mutate(current: AutoSettings) -> AutoSettings:
        if current.identify_camera is None:
            # The camera was taken away meanwhile: nothing to frame.
            return current
        return replace(current, identify_camera=replace(current.identify_camera, frame=frame))

    try:
        await controller.async_apply_settings(mutate=mutate)
    except SettingsReconcileError:
        # Written; only the plan could not be updated (the settings answer says the same).
        pass
    settings = camera.settings()
    if settings is None:
        raise CameraRefusal(ERROR_NO_CAMERA)
    return _envelope(identify_camera=settings.as_dict())


async def async_take_reference_picture(hass: HomeAssistant, entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    camera = _camera(hass, entry_id)
    vehicle_id = _vehicle(hass, entry_id, payload.get("vehicle_id"))
    kind = _kind(payload.get("kind"))
    assert kind is not None
    try:
        await camera.async_take_reference(vehicle_id, kind)
    except CameraUnavailable as err:
        raise CameraRefusal(ERROR_NO_CAMERA if err.code == "no_camera" else ERROR_NO_PICTURE) from None
    return _envelope(vehicle_id=vehicle_id, references=_references(camera, vehicle_id))


async def async_delete_reference_picture(hass: HomeAssistant, entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    camera = _camera(hass, entry_id)
    vehicle_id = payload.get("vehicle_id")
    if not valid_vehicle_id(vehicle_id):
        raise CameraRefusal(ERROR_INVALID_VALUE)
    # A car no longer at this charger may still have pictures: they can always be removed.
    kind = _kind(payload.get("kind"), allow_all=True)
    await camera.references.async_delete(vehicle_id, kind)
    return _envelope(vehicle_id=vehicle_id, references=_references(camera, vehicle_id))


async def async_reference_picture(hass: HomeAssistant, entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    camera = _camera(hass, entry_id)
    vehicle_id = payload.get("vehicle_id")
    if not valid_vehicle_id(vehicle_id):
        raise CameraRefusal(ERROR_INVALID_VALUE)
    kind = _kind(payload.get("kind"))
    assert kind is not None
    thumbnail = await camera.async_reference_thumbnail(vehicle_id, kind)
    if thumbnail is None:
        raise CameraRefusal(ERROR_INVALID_VALUE)
    width, height = await hass.async_add_executor_job(picture_size, thumbnail)
    return _envelope(vehicle_id=vehicle_id, kind=kind, picture=_picture(thumbnail, width, height))


#: Command name (WebSocket `spotnav/<name>`, webhook action `<name>`) -> what it does, and its fields.
COMMANDS: Final[dict[str, tuple[Command, tuple[str, ...]]]] = {
    "camera_snapshot": (async_camera_snapshot, ("camera_entity_id",)),
    "save_camera_frame": (async_save_camera_frame, ("frame",)),
    "take_reference_picture": (async_take_reference_picture, ("vehicle_id", "kind")),
    "delete_reference_picture": (async_delete_reference_picture, ("vehicle_id", "kind")),
    "reference_picture": (async_reference_picture, ("vehicle_id", "kind")),
}


def _websocket_handler(name: str, command: Command, fields: tuple[str, ...]) -> Any:
    schema: dict[Any, Any] = {
        vol.Required("type"): f"spotnav/{name}",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
    }
    schema.update({vol.Optional(field): object for field in fields})

    @websocket_api.websocket_command(schema)
    @websocket_api.async_response
    async def handler(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        version = msg.get("api_version")
        if isinstance(version, bool) or version != CAMERA_API_VERSION:
            send_unsupported_version(connection, msg, CAMERA_API_VERSION)
            return
        if not is_admin(connection):
            connection.send_result(msg["id"], _failure(ERROR_NOT_ADMIN))
            return
        entry, failure = lookup_charger(hass, msg.get("charger_id"))
        if failure is not None or entry is None:
            connection.send_result(msg["id"], _failure(failure or ERROR_INVALID_VALUE))
            return
        try:
            answer = await command(hass, entry.entry_id, msg)
        except CameraRefusal as refusal:
            connection.send_result(msg["id"], _failure(refusal.code))
            return
        connection.send_result(msg["id"], answer)

    return handler


def _webhook_handler(name: str, command: Command) -> Callable[..., Awaitable[web.Response]]:
    async def handler(hass: HomeAssistant, entry: ChargerConfigEntry, payload: dict[str, Any]) -> web.Response:
        try:
            answer = await command(hass, entry.entry_id, payload)
        except CameraRefusal as refusal:
            return web.json_response({**_failure(refusal.code), "action": name}, status=400)
        return web.json_response({**answer, "action": name})

    return handler


#: The webhook actions, by name (`api/webhook.py`).
WEBHOOK_ACTIONS: Final = {name: _webhook_handler(name, command) for name, (command, _fields) in COMMANDS.items()}


@callback
def async_setup_camera_api(hass: HomeAssistant) -> None:
    for name, (command, fields) in COMMANDS.items():
        websocket_api.async_register_command(hass, _websocket_handler(name, command, fields))
