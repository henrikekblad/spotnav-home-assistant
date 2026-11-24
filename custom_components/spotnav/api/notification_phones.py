"""The phones and what each hears (spec `plans/v3_notifications_per_phone.md` section 7): the card's two WebSocket
commands (administrators only) and the app's `phone_notifications` webhook action (its own phone only).

`{type: "spotnav/notification_phones", api_version: 1}` answers the listing:

    {api_version: 1, ok, error,
     phones: [{id, name, on, events, chargers: null | [charger ids],
               companion: {service, name, found} | null, app: {instant} | null, route: "app" | "companion" | "none"}],
     chargers: [{id, name}], loads: {chargers, pool, spa, sauna}, events: [every phone event, in order]}

`{type: "spotnav/update_notification_phone", api_version: 1, phone, changes}` changes one phone (`{on?, events?,
chargers?}`, or one of `link: <id>`, `unlink: true`, `forget: true`) and answers the listing again, or `ok: false` with
`spotnav_not_admin`, `spotnav_unknown_phone` or `spotnav_invalid_value` (nothing written). `route` is how a notice the
app can show reaches the phone now; an untouched Companion phone has the id `notify:<service>`.

The webhook action, on a charger's webhook and on a site's:

    {action: "phone_notifications", api_version?: 1, app_id, name?, push_ref?, shows?, set?: {on?, events?, chargers?}}

answers `{ok, action, api_version: 1, phone: {id, name, on, events, chargers, companion: {name} | null, instant},
chargers, loads, events}`. It reads and writes the caller's own phone only, the one whose app sent this `app_id` (made
on the first call, see `PhoneStore.async_app_call`); 400 `invalid_phone_notifications` for a body that cannot be
stored, nothing written. The `app_id` is a secret: never logged, kept hashed only, never answered.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..notifications.phone_store import PhoneRefusal, PhoneStore
from ..notifications.phones import PHONE_EVENTS, Phone, PhoneError, app_key, route_of
from ..notifications.settings import NOTIFY_DOMAIN
from ..runtime import domain_data
from .common import ERROR_NOT_ADMIN, ERROR_UNSUPPORTED_VERSION, is_admin, send_unsupported_version

_LOGGER = logging.getLogger(__name__)

PHONES_NOTIFY_API_VERSION: Final = 1
WEBHOOK_ACTION: Final = "phone_notifications"
ERROR_INVALID_PHONE_NOTIFICATIONS: Final = "invalid_phone_notifications"
ERROR_UNAVAILABLE: Final = "spotnav_phones_unavailable"


def _chargers(hass: HomeAssistant) -> list[dict[str, str]]:
    from ..vehicles.charger_inventory import charger_entries  # noqa: PLC0415 - the inventory imports the API layer

    return [{"id": entry.entry_id, "name": entry.title} for entry in charger_entries(hass)]


def loads(hass: HomeAssistant) -> dict[str, Any]:
    """What the installation has that a phone can hear about: how many chargers, and a pool, a spa, a sauna."""
    from ..vehicles.charger_inventory import site_entries  # noqa: PLC0415
    from .heat import stored_config  # noqa: PLC0415 - the heat API imports this module's neighbours

    configs = [stored_config(site) for site in site_entries(hass)]
    return {
        "chargers": len(_chargers(hass)),
        "pool": any(config.pool is not None for config in configs),
        "spa": any(config.spa is not None for config in configs),
        "sauna": any(config.sauna is not None for config in configs),
    }


def _has_service(hass: HomeAssistant) -> Any:
    return lambda service: hass.services.has_service(NOTIFY_DOMAIN, service)


def encode_phone(hass: HomeAssistant, phone: Phone, names: dict[str, str]) -> dict[str, Any]:
    has = _has_service(hass)
    companion = None
    if phone.notify is not None:
        companion = {"service": phone.notify, "name": names.get(phone.notify, phone.name), "found": has(phone.notify)}
    return {
        "id": phone.id,
        "name": phone.name,
        "on": phone.on,
        "events": list(phone.events),
        "chargers": None if phone.chargers is None else list(phone.chargers),
        "companion": companion,
        "app": None if phone.app is None else {"instant": phone.instant},
        "route": route_of(phone, (), has),
    }


def listing_answer(hass: HomeAssistant, store: PhoneStore) -> dict[str, Any]:
    names = store.phone_names()
    return {
        "api_version": PHONES_NOTIFY_API_VERSION,
        "ok": True,
        "error": None,
        "phones": [encode_phone(hass, phone, names) for phone in store.listing()],
        "chargers": _chargers(hass),
        "loads": loads(hass),
        "events": list(PHONE_EVENTS),
    }


def _refusal(code: str) -> dict[str, Any]:
    return {
        "api_version": PHONES_NOTIFY_API_VERSION,
        "ok": False,
        "error": code,
        "phones": [],
        "chargers": [],
        "loads": {"chargers": 0, "pool": False, "spa": False, "sauna": False},
        "events": list(PHONE_EVENTS),
    }


def _speaks(msg: dict[str, Any]) -> bool:
    version = msg.get("api_version")
    return type(version) is int and version == PHONES_NOTIFY_API_VERSION


@websocket_api.websocket_command(
    {vol.Required("type"): "spotnav/notification_phones", vol.Optional("api_version"): object}
)
@websocket_api.async_response
async def websocket_notification_phones(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if not _speaks(msg):
        send_unsupported_version(connection, msg, PHONES_NOTIFY_API_VERSION)
        return
    store = domain_data(hass).phones
    if not is_admin(connection):
        connection.send_result(msg["id"], _refusal(ERROR_NOT_ADMIN))
        return
    if store is None:
        connection.send_result(msg["id"], _refusal(ERROR_UNAVAILABLE))
        return
    connection.send_result(msg["id"], listing_answer(hass, store))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/update_notification_phone",
        vol.Optional("api_version"): object,
        vol.Required("phone"): object,
        vol.Required("changes"): object,
    }
)
@websocket_api.async_response
async def websocket_update_notification_phone(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if not _speaks(msg):
        send_unsupported_version(connection, msg, PHONES_NOTIFY_API_VERSION)
        return
    store = domain_data(hass).phones
    if not is_admin(connection):
        connection.send_result(msg["id"], _refusal(ERROR_NOT_ADMIN))
        return
    if store is None:
        connection.send_result(msg["id"], _refusal(ERROR_UNAVAILABLE))
        return
    try:
        await store.async_update(msg["phone"], msg["changes"])
    except PhoneRefusal as refusal:
        connection.send_result(msg["id"], {**listing_answer(hass, store), "ok": False, "error": refusal.code})
        return
    connection.send_result(msg["id"], listing_answer(hass, store))


def _own_phone(hass: HomeAssistant, store: PhoneStore, phone: Phone) -> dict[str, Any]:
    names = store.phone_names()
    return {
        "id": phone.id,
        "name": phone.name,
        "on": phone.on,
        "events": list(phone.events),
        "chargers": None if phone.chargers is None else list(phone.chargers),
        "companion": None if phone.notify is None else {"name": names.get(phone.notify, phone.name)},
        "instant": phone.instant,
    }


async def async_phone_notifications(hass: HomeAssistant, payload: dict[str, Any]) -> web.Response:
    """The app's own phone: read, and written where the body says (see the module)."""
    action = WEBHOOK_ACTION
    asked = payload.get("api_version", PHONES_NOTIFY_API_VERSION)
    if type(asked) is not int or asked != PHONES_NOTIFY_API_VERSION:
        return web.json_response({"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": action}, status=400)
    store = domain_data(hass).phones
    if store is None:
        return web.json_response({"ok": False, "error": ERROR_UNAVAILABLE, "action": action}, status=503)
    try:
        key = app_key(payload.get("app_id"))
        phone = await store.async_app_call(
            key,
            name=payload.get("name"),
            push_ref=payload["push_ref"] if "push_ref" in payload else ...,
            shows=payload["shows"] if "shows" in payload else ...,
            changes=payload.get("set"),
        )
    except (PhoneError, PhoneRefusal) as refusal:
        # Never the body: it carries the app's secret id and maybe its wake-up reference.
        _LOGGER.debug("SpotNav: a phone's notification settings were refused: %s", refusal)
        return web.json_response({"ok": False, "error": ERROR_INVALID_PHONE_NOTIFICATIONS, "action": action}, status=400)
    return web.json_response(
        {
            "ok": True,
            "action": action,
            "api_version": PHONES_NOTIFY_API_VERSION,
            "phone": _own_phone(hass, store, phone),
            "chargers": _chargers(hass),
            "loads": loads(hass),
            "events": list(PHONE_EVENTS),
        }
    )


async def webhook_phone_notifications(hass: HomeAssistant, _entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    """The webhook handler (a charger's or a site's: the phones are the installation's)."""
    return await async_phone_notifications(hass, payload)


@callback
def async_setup_notification_phones_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_notification_phones)
    websocket_api.async_register_command(hass, websocket_update_notification_phone)
