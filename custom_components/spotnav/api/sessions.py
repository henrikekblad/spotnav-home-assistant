"""The charge-session history contract: `spotnav/get_sessions` (API v1).

One read-only command per charger, answering from memory (the session store), for the card's History
view and for any other client:

* default (`format` absent or `"json"`): the bounded summaries (every month with a session, the last
  `DAY_LIMIT` days), this and last month, the open session if any, and the last `limit` sessions;
* `format: "csv"`: the closed sessions that started between `from` and `to` (local dates, either
  optional), as one CSV text with the file name to save it under;
* `month` (`"YYYY-MM"`, default the current month, at most `MONTH_LIMIT` months back): the additive
  `month`, `month_summary`, `month_days` (one row for every day of the month, zero rows included),
  `month_sessions` (that month's sessions, newest first) and `available_months` (the months with data,
  newest first). With `format: "csv"` it is the whole month's CSV; it is refused together with `from`/`to`.

The WebSocket command and the webhook action `sessions` share `sessions_answer`, so both answer alike.

Every authenticated user may read, like the dashboard. Money is in the major unit (as the plan's
estimated cost), prices in the minor unit per kWh, and the savings are an estimate (the same energy at
the day's average price) and say so. Nothing here writes.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

import re

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant
from homeassistant.util import dt as dt_util

from ..runtime import domain_data
from ..sessions.inputs import local_zone
from ..sessions.model import ChargeSession
from ..sessions.store import RETENTION_DAYS
from ..sessions.summary import (
    available_months,
    month_bounds,
    month_days,
    month_key,
    month_sessions,
    month_summary,
    previous_month,
    sessions_csv,
    summarize,
)
from .common import send_unsupported_version
from .dashboard import DashboardFailure, resolve_charger_request

SESSIONS_API_VERSION: Final = 1

#: The last sessions and days one answer carries; the CSV is the way to the rest.
DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 100
DAY_LIMIT: Final = 62
#: How many months back `month` may name (the store keeps two years).
MONTH_LIMIT: Final = 24

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
    "month",
    "month_summary",
    "month_days",
    "month_sessions",
    "available_months",
)


def sessions_payload(
    sessions: tuple[ChargeSession, ...],
    open_session: ChargeSession | None,
    *,
    charger_id: str,
    zone,
    now,
    limit: int,
    month: str | None = None,
) -> dict[str, Any]:
    """The JSON answer, built from values only. `month` (validated) defaults to the current one."""
    today = now.astimezone(zone).date()
    chosen = month if month is not None else month_key(today)
    in_window = [m for m in available_months(sessions, zone) if m >= months_back(today, MONTH_LIMIT)]
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
        "month": chosen,
        "month_summary": month_summary(sessions, zone, chosen),
        "month_days": month_days(sessions, zone, chosen),
        "month_sessions": [item.public(zone) for item in month_sessions(sessions, zone, chosen)],
        "available_months": in_window,
    }


def months_back(today: date, count: int) -> str:
    """The month `count` months before `today`'s, as `YYYY-MM`."""
    index = today.year * 12 + today.month - 1 - count
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def valid_month(value: Any, today: date) -> bool:
    """A `YYYY-MM` that is not in the future and at most `MONTH_LIMIT` months back."""
    return (
        isinstance(value, str)
        and _MONTH.match(value) is not None
        and months_back(today, MONTH_LIMIT) <= value <= month_key(today)
    )


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


class SessionsRefusal(Exception):
    """A request the answer cannot honour; `code` is its stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sessions_answer(hass: HomeAssistant, entry_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """The answer to one sessions request for one charger (WebSocket and webhook alike).

    Reads `format`, `limit`, `from`, `to` and `month` from `request`; raises `SessionsRefusal` for a
    request that is not valid.
    """
    form = request.get("format", "json")
    limit = request.get("limit", DEFAULT_LIMIT)
    first, last = _date(request.get("from")), _date(request.get("to"))
    month = request.get("month")
    zone = local_zone(hass)
    now = dt_util.utcnow()
    today = now.astimezone(zone).date()
    if (
        form not in FORMATS
        or type(limit) is not int
        or not 1 <= limit <= MAX_LIMIT
        or first is False
        or last is False
        or (isinstance(first, date) and isinstance(last, date) and first > last)
        or (month is not None and (not valid_month(month, today) or first is not None or last is not None))
    ):
        raise SessionsRefusal(ERROR_INVALID_RANGE, "The request's format, limit, month or dates are not valid")
    store = domain_data(hass).session_store
    sessions = () if store is None else store.closed(entry_id)
    if form == "csv":
        if month is not None:
            first, last = month_bounds(month)
        return {
            "api_version": SESSIONS_API_VERSION,
            "format": "csv",
            "filename": csv_filename(entry_id, first, last),  # type: ignore[arg-type]
            "csv": sessions_csv(sessions, zone, first=first, last=last),  # type: ignore[arg-type]
        }
    return sessions_payload(
        sessions,
        None if store is None else store.open_session(entry_id),
        charger_id=entry_id,
        zone=zone,
        now=now,
        limit=limit,
        month=month,
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_sessions",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("limit"): object,
        vol.Optional("format"): object,
        vol.Optional("from"): object,
        vol.Optional("to"): object,
        vol.Optional("month"): object,
    }
)
@websocket_api.async_response
async def websocket_get_sessions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One charger's charge sessions: summaries and the latest sessions, a chosen month, or a CSV."""
    version = msg.get("api_version")
    if type(version) is not int or version != SESSIONS_API_VERSION:
        send_unsupported_version(connection, msg, SESSIONS_API_VERSION)
        return
    charger = resolve_charger_request(hass, msg)
    if isinstance(charger, DashboardFailure):
        connection.send_error(msg["id"], charger.code, charger.message)
        return
    try:
        answer = sessions_answer(hass, charger.entry_id, msg)
    except SessionsRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_sessions_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_get_sessions)
