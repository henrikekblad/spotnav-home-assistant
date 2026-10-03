"""The charge-session history contract: `spotnav/get_sessions` (API v1).

One read-only command per charger, answering from memory (the session store), for the card's History
view and for any other client:

* default (`format` absent or `"json"`): the bounded summaries (every month with a session, the last
  `DAY_LIMIT` days), this and last month, the open session if any, and the last `limit` sessions;
* `format: "csv"`: the closed sessions that started between `from` and `to` (local dates, either
  optional), as one CSV text with the file name to save it under.

Every authenticated user may read, like the dashboard. Money is in the major unit (as the plan's
estimated cost), prices in the minor unit per kWh, and the savings are an estimate (the same energy at
the day's average price) and say so. Nothing here writes.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant
from homeassistant.util import dt as dt_util

from ..runtime import domain_data
from ..sessions.inputs import local_zone
from ..sessions.model import ChargeSession
from ..sessions.store import RETENTION_DAYS
from ..sessions.summary import month_key, previous_month, sessions_csv, summarize, month_summary
from .common import send_unsupported_version
from .dashboard import DashboardFailure, resolve_charger_request

SESSIONS_API_VERSION: Final = 1

#: The last sessions and days one answer carries; the CSV is the way to the rest.
DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 100
DAY_LIMIT: Final = 62

FORMATS: Final = ("json", "csv")

ERROR_INVALID_RANGE: Final = "spotnav_invalid_range"

#: Every key of the JSON answer, in order.
SESSIONS_RESPONSE_KEYS: Final = (
    "api_version",
    "charger_id",
    "retention_days",
    "this_month",
    "last_month",
    "months",
    "days",
    "open",
    "sessions",
)


def sessions_payload(
    sessions: tuple[ChargeSession, ...],
    open_session: ChargeSession | None,
    *,
    charger_id: str,
    zone,
    now,
    limit: int,
) -> dict[str, Any]:
    """The JSON answer, built from values only."""
    today = now.astimezone(zone).date()
    return {
        "api_version": SESSIONS_API_VERSION,
        "charger_id": charger_id,
        "retention_days": RETENTION_DAYS,
        "this_month": month_summary(sessions, zone, month_key(today)),
        "last_month": month_summary(sessions, zone, previous_month(today)),
        "months": summarize(sessions, zone, by="month"),
        "days": summarize(sessions, zone, by="day")[:DAY_LIMIT],
        "open": None if open_session is None else open_session.public(zone),
        "sessions": [item.public(zone) for item in sorted(sessions, key=lambda s: s.start, reverse=True)[:limit]],
    }


def csv_filename(charger_id: str, first: date | None, last: date | None) -> str:
    """A file name made of the dates only: never the charger's name or id, which a person may not want
    in a file they send somewhere."""
    del charger_id
    span = "-".join(part.isoformat() for part in (first, last) if part is not None)
    return f"spotnav-sessions{'-' + span if span else ''}.csv"


def _date(value: Any) -> date | None | bool:
    """A local date, `None` when absent, `False` when it is not one."""
    if value is None:
        return None
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value)
    except ValueError:
        return False


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_sessions",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("limit"): object,
        vol.Optional("format"): object,
        vol.Optional("from"): object,
        vol.Optional("to"): object,
    }
)
@websocket_api.async_response
async def websocket_get_sessions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One charger's charge sessions: summaries and the latest sessions, or a CSV for a date range."""
    version = msg.get("api_version")
    if type(version) is not int or version != SESSIONS_API_VERSION:
        send_unsupported_version(connection, msg, SESSIONS_API_VERSION)
        return
    charger = resolve_charger_request(hass, msg)
    if isinstance(charger, DashboardFailure):
        connection.send_error(msg["id"], charger.code, charger.message)
        return
    form = msg.get("format", "json")
    limit = msg.get("limit", DEFAULT_LIMIT)
    first, last = _date(msg.get("from")), _date(msg.get("to"))
    if (
        form not in FORMATS
        or type(limit) is not int
        or not 1 <= limit <= MAX_LIMIT
        or first is False
        or last is False
        or (isinstance(first, date) and isinstance(last, date) and first > last)
    ):
        connection.send_error(msg["id"], ERROR_INVALID_RANGE, "The request's format, limit or dates are not valid")
        return
    store = domain_data(hass).session_store
    sessions = () if store is None else store.closed(charger.entry_id)
    zone = local_zone(hass)
    if form == "csv":
        connection.send_result(
            msg["id"],
            {
                "api_version": SESSIONS_API_VERSION,
                "format": "csv",
                "filename": csv_filename(charger.entry_id, first, last),  # type: ignore[arg-type]
                "csv": sessions_csv(sessions, zone, first=first, last=last),  # type: ignore[arg-type]
            },
        )
        return
    connection.send_result(
        msg["id"],
        sessions_payload(
            sessions,
            None if store is None else store.open_session(charger.entry_id),
            charger_id=charger.entry_id,
            zone=zone,
            now=dt_util.utcnow(),
            limit=limit,
        ),
    )


@callback
def async_setup_sessions_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_get_sessions)
