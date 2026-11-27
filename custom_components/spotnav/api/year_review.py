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
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..runtime import site_data
from ..year_review.compute import YEAR_REVIEW_API_VERSION
from ..year_review.store import ACTIONS
from .common import send_unsupported_version
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

ERROR_INVALID_REQUEST: Final = "spotnav_invalid_request"


def _review_of(hass: HomeAssistant, msg: dict[str, Any]) -> Any:
    """The year review of the site a request names, or a `PowerMonthRefusal`."""
    site = _site_of_request(hass, msg)
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
    if type(year) is not int or not 2000 <= year <= 2200 or action not in ACTIONS or user_id is None:
        connection.send_error(msg["id"], ERROR_INVALID_REQUEST, "year must be a year and action viewed or dismissed")
        return
    try:
        review = _review_of(hass, msg)
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    review.store.mark(user_id, year, action)
    connection.send_result(msg["id"], {"ok": True})


@callback
def async_setup_year_review_api(hass: HomeAssistant) -> None:
    """Register the commands once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_year_review)
    websocket_api.async_register_command(hass, websocket_year_review_seen)
