"""The year's review for the cards (API v1, `year_review/`): two commands of their own, not keys in
`spotnav/get_dashboard` or `spotnav/get_house`, whose decoders take exactly their keys.

* `spotnav/get_year_review {api_version: 1, site_id | charger_id}`: the kept review and whether this person's cards
  offer it (`year_review/compute.review_answer`):
  `{api_version, site_id, year, state: "ready" | "computing" | "none", banner, row, review | null}`. `banner`: offered
  (10 December to 31 January, or while the service's showing lasts) and ready, and this person has neither opened
  it nor closed its banner; `row` (the Support row): offered and ready. Read from memory: nothing is worked out on a
  read.
* `spotnav/year_review_seen {api_version: 1, site_id | charger_id, year, action: "viewed" | "dismissed"}` -> `{ok: true}`: this
  person opened the review of `year` (the banner goes for good) or closed its banner. Kept per Home Assistant user
  (`connection.user.id`), so the banner does not come back in another browser or the companion app.

Any user who can see the cards may call both; the person is the connection's user, never a field. Refusals as
`spotnav/get_house_history` (`spotnav_unknown_site`, `spotnav_no_site`, the charger's codes), and
`spotnav_invalid_request` for a year or an action that is not one.

Their webhook twins, for the paired app (`api/site_webhook.py`: the charger webhook's site, or the site webhook's own;
a `site_id` or `charger_id` in the body is never read). The webhook has no Home Assistant user: the person is the
phone, by the app's secret `app_id` (as `phone_notifications`), whose seen state is kept apart from the users'
(`year_review/store.py`, `phones`) under the same rules:

* `{action: "get_year_review", api_version?: 1, app_id?}` -> `{ok: true, action, api_version, site_id, year, state,
  banner, row, review}`, the command's answer for this phone (without `app_id`: for a phone that has seen nothing).
* `{action: "year_review_seen", api_version?: 1, app_id, year, seen_action: "viewed" | "dismissed"}` ->
  `{ok: true, action}` (`action` routes the body, so the choice is `seen_action`).

Refusals are HTTP 400 `{ok: false, error, action}`: `spotnav_unsupported_api_version`, `spotnav_no_site` (a charger
in no site, a site not loaded), `spotnav_invalid_request` (an `app_id`, a year or a `seen_action` that is not one).
The `app_id` is a secret: never logged, kept hashed only, never answered.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..notifications.phones import PhoneError, app_key
from ..runtime import site_data
from ..year_review.compute import YEAR_REVIEW_API_VERSION
from ..year_review.store import ACTIONS
from .common import ERROR_UNSUPPORTED_VERSION, send_unsupported_version
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

ERROR_INVALID_REQUEST: Final = "spotnav_invalid_request"


def _review_of(hass: HomeAssistant, msg: dict[str, Any]) -> Any:
    """The year review of the site a request names, or a `PowerMonthRefusal`."""
    return _review_of_site(hass, _site_of_request(hass, msg))


def _review_of_site(hass: HomeAssistant, site: ConfigEntry | None) -> Any:
    """The year review of a loaded site, or a `PowerMonthRefusal`."""
    if site is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The charger is in no site")
    data = site_data(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or data is None or data.year_review is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    return data.year_review


def _version_ok(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> bool:
    version = msg.get("api_version")
    if type(version) is not int or version != YEAR_REVIEW_API_VERSION:
        send_unsupported_version(connection, msg, YEAR_REVIEW_API_VERSION)
        return False
    return True


def _user_id(connection: websocket_api.ActiveConnection) -> str | None:
    user = connection.user
    return None if user is None else user.id


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_year_review",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
    }
)
@callback
def websocket_get_year_review(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The kept review of one site, for this person."""
    if not _version_ok(connection, msg):
        return
    try:
        review = _review_of(hass, msg)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], review.answer(_user_id(connection)))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/year_review_seen",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("year"): object,
        vol.Optional("action"): object,
    }
)
@callback
def websocket_year_review_seen(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """This person opened the review of a year, or closed its banner."""
    if not _version_ok(connection, msg):
        return
    year, action = msg.get("year"), msg.get("action")
    user_id = _user_id(connection)
    if not _seen_ok(year, action) or user_id is None:
        connection.send_error(msg["id"], ERROR_INVALID_REQUEST, "year must be a year and action viewed or dismissed")
        return
    try:
        review = _review_of(hass, msg)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    review.store.mark(user_id, year, action)
    connection.send_result(msg["id"], {"ok": True})


def _seen_ok(year: Any, action: Any) -> bool:
    return type(year) is int and 2000 <= year <= 2200 and action in ACTIONS


# ---------------------------------------------------------------------------------------------- the webhook twins

WEBHOOK_GET: Final = "get_year_review"
WEBHOOK_SEEN: Final = "year_review_seen"


def _refused(action: str, code: str) -> web.Response:
    return web.json_response({"ok": False, "error": code, "action": action}, status=400)


def _webhook_version_ok(payload: dict[str, Any]) -> bool:
    version = payload.get("api_version", YEAR_REVIEW_API_VERSION)
    return type(version) is int and version == YEAR_REVIEW_API_VERSION


async def webhook_get_year_review(hass: HomeAssistant, site: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
    """`get_year_review` for the paired phone (see the module)."""
    if not _webhook_version_ok(payload):
        return _refused(WEBHOOK_GET, ERROR_UNSUPPORTED_VERSION)
    try:
        review = _review_of_site(hass, site)
    except PowerMonthRefusal as refusal:
        return _refused(WEBHOOK_GET, refusal.code)
    phone: str | None = None
    if payload.get("app_id") is not None:
        try:
            phone = app_key(payload["app_id"])
        except PhoneError:
            return _refused(WEBHOOK_GET, ERROR_INVALID_REQUEST)
    return web.json_response({"ok": True, "action": WEBHOOK_GET, **review.answer(phone, phone=True)})


async def webhook_year_review_seen(hass: HomeAssistant, site: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
    """`year_review_seen` for the paired phone: it opened the review of `year`, or closed its banner."""
    if not _webhook_version_ok(payload):
        return _refused(WEBHOOK_SEEN, ERROR_UNSUPPORTED_VERSION)
    try:
        review = _review_of_site(hass, site)
    except PowerMonthRefusal as refusal:
        return _refused(WEBHOOK_SEEN, refusal.code)
    year, action = payload.get("year"), payload.get("seen_action")
    try:
        phone = app_key(payload.get("app_id"))
    except PhoneError:
        return _refused(WEBHOOK_SEEN, ERROR_INVALID_REQUEST)
    if not _seen_ok(year, action):
        return _refused(WEBHOOK_SEEN, ERROR_INVALID_REQUEST)
    review.store.mark(phone, year, action, phone=True)
    return web.json_response({"ok": True, "action": WEBHOOK_SEEN})


@callback
def async_setup_year_review_api(hass: HomeAssistant) -> None:
    """Register the commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_year_review)
    websocket_api.async_register_command(hass, websocket_year_review_seen)
