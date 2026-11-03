"""`spotnav/get_power_month` (API v1): one month of the site's measured import, for the history dialog's power view.

`{api_version: 1, charger_id | site_id, month?: "YYYY-MM"}` (the month defaults to the running one, on the area's
clock; at most `ARCHIVE_MONTHS` back and never ahead). Any user who can see the card may read, as the dashboard.
The answer (`site/power_history.py:power_month`):

    {api_version, site_id, month, available_months, unit: "kW", import_target_kw, components, days, hours, bars}

* `components`: the demand-charge parts the month was measured against (as `grid_tariff.demand.components`): the
  ones set now for a month whose hours are kept (the running month and the one before), else those set when its
  last hour completed. `bars[i]` is part `i`'s figure that month (`null` while nothing counted).
* `days`: one per day with a measured hour, oldest first, `{date, peak_kw, peak_start, house_kw, counted, held,
  paused}`: the day's highest hourly mean of net import and the hour it started (local time on the area's clock),
  the house's part of that hour (`null` when the chargers could not be read), the parts whose counted peaks fall
  on that day (`counted`, indexes into `components`), and whether the import target held a charger's current down
  or paused a charger in any of its hours.
* `hours`: every measured hour `{start, kw, house_kw, incomplete, held, paused, counted}` for a month whose hours
  are kept, `null` for an older month (its days only).
* `import_target_kw`: the site's import target now (`null` for none).

Refusals (WebSocket errors; the webhook answers 400 with `error`): `spotnav_unsupported_api_version`,
`spotnav_charger_required` (neither id), the charger lookup's codes, `spotnav_unknown_site`, `spotnav_no_site`
(the charger is in no site, or the site is not loaded), `spotnav_needs_total_grid_power` (the site cannot measure
its import: no total grid power on a direct site) and `spotnav_invalid_range` (the month). Nothing here writes.

The charger webhook's `get_power_month` (`webhook_get_power_month`) answers the same for the paired app's charger
(its site), with the routing `action` beside it.
"""

from __future__ import annotations

from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..const import CONF_ENTRY_TYPE, CONF_GRID_POWER_SOURCE, CONF_MEASUREMENT_MODE, DOMAIN, ENTRY_TYPE_SITE
from ..planning.first_run import site_for_charger
from ..pricing.price_source import site_demand_components
from ..runtime import site_controller_for
from ..site.power_history import ARCHIVE_MONTHS, month_from, month_of_now, months_before
from ..site.solar_capability import solar_capability
from .common import ERROR_CHARGER_REQUIRED, ERROR_UNSUPPORTED_VERSION, lookup_charger, send_unsupported_version
from .entity_fields import stored_import_target_kw

POWER_MONTH_API_VERSION: Final = 1
#: The charger webhook's action for the same.
WEBHOOK_ACTION: Final = "get_power_month"

ERROR_UNKNOWN_SITE: Final = "spotnav_unknown_site"
ERROR_NO_SITE: Final = "spotnav_no_site"
ERROR_NEEDS_TOTAL_GRID_POWER: Final = "spotnav_needs_total_grid_power"
ERROR_INVALID_RANGE: Final = "spotnav_invalid_range"

#: Every key of the answer, in order.
POWER_MONTH_KEYS: Final = (
    "api_version",
    "site_id",
    "month",
    "available_months",
    "unit",
    "import_target_kw",
    "components",
    "days",
    "hours",
    "bars",
)


class PowerMonthRefusal(Exception):
    """A request the answer cannot honour; `code` is its stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _site_of_request(hass: HomeAssistant, request: dict[str, Any]) -> ConfigEntry:
    """The site a request names (`site_id`, else the site of `charger_id`), or a refusal."""
    site_id = request.get("site_id")
    if site_id is not None:
        entry = hass.config_entries.async_get_entry(site_id) if isinstance(site_id, str) and site_id else None
        if entry is None or entry.domain != DOMAIN or entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_SITE:
            raise PowerMonthRefusal(ERROR_UNKNOWN_SITE, "No such site")
        return entry
    charger_id = request.get("charger_id")
    if charger_id is None:
        raise PowerMonthRefusal(ERROR_CHARGER_REQUIRED, "A charger_id or a site_id is required")
    charger, code = lookup_charger(hass, charger_id)
    if charger is None:
        raise PowerMonthRefusal(code or ERROR_CHARGER_REQUIRED, "The charger cannot be read")
    return _site_of_charger(hass, charger)


def _site_of_charger(hass: HomeAssistant, charger: ConfigEntry) -> ConfigEntry:
    site = site_for_charger(hass, charger.entry_id)
    if site is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The charger is in no site")
    return site


def power_month_answer(hass: HomeAssistant, site: ConfigEntry, month_raw: Any) -> dict[str, Any]:
    """The answer for one site and month (WebSocket and webhook alike), or a `PowerMonthRefusal`."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    capability = solar_capability(site.data.get(CONF_MEASUREMENT_MODE), site.data.get(CONF_GRID_POWER_SOURCE))
    if not capability.capable:
        raise PowerMonthRefusal(ERROR_NEEDS_TOTAL_GRID_POWER, "The site cannot measure its import")
    demand = controller.demand
    zone = controller.demand_zone()
    current = month_of_now(demand.now(), zone)
    month = current if month_raw is None else month_from(month_raw)
    if month is None or not months_before(current, ARCHIVE_MONTHS) <= month <= current:
        raise PowerMonthRefusal(ERROR_INVALID_RANGE, f"month must be YYYY-MM, at most {ARCHIVE_MONTHS} months back")
    view = demand.power_month(
        month,
        site_demand_components(hass, site.entry_id),
        import_target_kw=stored_import_target_kw(site.data),
        tz=zone,
    )
    return {"api_version": POWER_MONTH_API_VERSION, "site_id": site.entry_id, **view}


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_power_month",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("month"): object,
    }
)
@callback
def websocket_get_power_month(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """One month of the site's measured import."""
    version = msg.get("api_version")
    if type(version) is not int or version != POWER_MONTH_API_VERSION:
        send_unsupported_version(connection, msg, POWER_MONTH_API_VERSION)
        return
    try:
        answer = power_month_answer(hass, _site_of_request(hass, msg), msg.get("month"))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


async def webhook_get_power_month(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    """`get_power_month` over the charger webhook (the paired app): this charger's site, an optional `month` and
    `api_version` (1). HTTP 200 with the answer, 400 with the refusal's code."""
    version = payload.get("api_version", POWER_MONTH_API_VERSION)
    if type(version) is not int or version != POWER_MONTH_API_VERSION:
        return web.json_response({"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": WEBHOOK_ACTION}, status=400)
    try:
        answer = power_month_answer(hass, _site_of_charger(hass, entry), payload.get("month"))
    except PowerMonthRefusal as refusal:
        return web.json_response({"ok": False, "error": refusal.code, "action": WEBHOOK_ACTION}, status=400)
    return web.json_response({"ok": True, "action": WEBHOOK_ACTION, **answer})


@callback
def async_setup_power_month_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_power_month)
