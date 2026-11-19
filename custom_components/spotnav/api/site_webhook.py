"""The charger webhook's site actions (SpotNav 3.0): the house, heat, battery and site settings reads the paired app
needs, and its everyday writes. Each is the twin of a WebSocket command, bound to the webhook's own charger's site
(a `site_id` or `charger_id` in the body is never read) and answered by that command's own builder, so both carriers
answer alike: the WebSocket answer with `ok` and the routing `action` beside it.

Reads (HTTP 200, or 400 with `{"ok": false, "error": <the command's stable code>, "action"}`):

| action | twin | request fields |
| --- | --- | --- |
| `get_house` | `spotnav/get_house` | `date?` |
| `get_house_now` | `spotnav/get_house_now` | |
| `get_house_history` | `spotnav/get_house_history` | `period`, `date?` |
| `get_house_loads` | `spotnav/get_house_loads` | `period?`, `date?`, `kind?` |
| `get_heat` | `spotnav/get_heat` (as someone who is no administrator: `writable` false, no `choices`) | |
| `get_heat_history` | `spotnav/get_heat_history` | `period`, `date?` |
| `get_site_settings_view` | `spotnav/get_site_settings_view` (`writable` true, `site.active_control.writable` false, as the webhook's dashboard) | |
| `battery_control` | `spotnav/battery_control` | `changes?` |

Writes, the everyday choices only (the owner's decision of 2026-11-18, `plans/app_3_0_plan.md`):

* `update_heat` (`spotnav/update_heat`: `changes`, `expected`): the dial (`target_c`), home/away (`mode`,
  `away_until`), the cooler night (`night`), each stored zone's offset (`zones`, the same entities in the same
  order) and each stored indoor sensor's offset (`indoor_offsets`), judged by the command's own rules (`heat/config.py`) under its compare-and-set.
* `battery_control` with `changes`: `battery_mode` only (`self_consumption`, `balanced`, `max_profit`).
* `heat_boost` (`spotnav/heat_boost`): the Boost button, `boost_action` "start" or "stop" (`action` routes the
  body); HTTP 200, 400 for an unknown `boost_action`, 429 for `spotnav_rate_limited`, 409 for the other refusals.

The control opt-ins stay with a signed-in administrator in Home Assistant: `control_enabled` ("Styr värmen") and
the control path, the sensors and which entities are zones, `battery_control_enabled` ("Styr batteriet") and the other battery options are
`spotnav_not_permitted_over_webhook` (HTTP 403) and nothing is written, as load balancing is
(`api/site_settings.py`). Both writes answer their command's envelope (`ok`, `error` inside it): HTTP 200, 400 for a
value refused, 409 for `spotnav_conflict`, 403 as above.

`api_version` is optional in every body (1, each command's own); another is `spotnav_unsupported_api_version`. A
charger in no site, or in one that is not loaded, is `spotnav_no_site`. The dashboard's `charger.capabilities` says
what a charger's webhook answers: `get_house`, `get_heat`, `battery_control`, `get_site_settings_view`
(`api/dashboard.py`).

The handlers take the site itself (`site_handlers`); `SITE_ACTIONS` binds them to the charger webhook's charger's
site, and the site webhook (`api/site_entry_webhook.py`) to its own site.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiohttp import web
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from ..heat.history import HEAT_HISTORY_API_VERSION
from ..heat.view import HEAT_API_VERSION
from ..planning.hybrid_forecast import async_forecast_capable_domains
from ..site.house_now import HOUSE_NOW_API_VERSION
from ..site.house_period import HOUSE_HISTORY_API_VERSION
from ..site.house_loads import HOUSE_LOADS_API_VERSION
from ..site.house_view import HOUSE_API_VERSION
from . import heat_history, house_history
from .battery_control import BATTERY_CONTROL_API_VERSION, async_battery_control_answer
from .common import (
    ERROR_NO_SITE,
    ERROR_NOT_PERMITTED_OVER_WEBHOOK,
    ERROR_UNSUPPORTED_VERSION,
    site_of_charger,
)
from .heat import (
    ERROR_CONFLICT,
    ERROR_INVALID_VALUE,
    HeatWriteRefusal,
    heat_answer,
    heat_boost_answer,
    write_envelope,
    write_heat,
)
from .house import async_house_answer
from .house_loads import async_loads_answer
from .house_now import async_house_now
from .power_month import PowerMonthRefusal
from .site_view import SITE_VIEW_API_VERSION, site_settings_view

type _Handler = Callable[[HomeAssistant, ConfigEntry, dict[str, Any]], Awaitable[web.Response]]
#: A site action bound to a site: `(hass, site, payload)`, `site` `None` for a charger in no site.
type SiteHandler = Callable[[HomeAssistant, ConfigEntry | None, dict[str, Any]], Awaitable[web.Response]]
type _Read = Callable[[HomeAssistant, ConfigEntry, dict[str, Any]], Awaitable[dict[str, Any]]]

#: HTTP status of a write's refusal by its stable code; any other refusal is a bad request.
_WRITE_STATUS: Final = {ERROR_CONFLICT: 409, ERROR_NOT_PERMITTED_OVER_WEBHOOK: 403}


def _refused(action: str, code: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": code, "action": action}, status=status)


def _speaks(payload: dict[str, Any], version: int) -> bool:
    """Whether the body asks for the command's version (absent: it does)."""
    asked = payload.get("api_version", version)
    return type(asked) is int and asked == version


def _read(action: str, version: int, read: _Read) -> SiteHandler:
    """A read twinning a WebSocket command: `read(hass, site, payload)` is the command's own answer for the
    site, a `PowerMonthRefusal` its refusal."""

    async def handler(hass: HomeAssistant, site: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
        if not _speaks(payload, version):
            return _refused(action, ERROR_UNSUPPORTED_VERSION)
        if site is None:
            return _refused(action, ERROR_NO_SITE)
        try:
            answer = await read(hass, site, payload)
        except PowerMonthRefusal as refusal:
            return _refused(action, refusal.code)
        return web.json_response({"ok": True, "action": action, **answer})

    return handler


async def _house(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> dict[str, Any]:
    return await async_house_answer(hass, site, payload.get("date"))


async def _house_now(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> dict[str, Any]:
    return await async_house_now(hass, site)


async def _house_history(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> dict[str, Any]:
    return await house_history.history_answer(hass, site, payload.get("period"), payload.get("date"))


async def _heat(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> dict[str, Any]:
    # The webhook's holder is no administrator: no entity choices, and `writable` (the configuration) false. The
    # dial and home/away are everyone's, as over the WebSocket.
    return await heat_answer(hass, site, writable=False)


async def _heat_history(hass: HomeAssistant, site: ConfigEntry, payload: dict[str, Any]) -> dict[str, Any]:
    return await heat_history.history_answer(hass, site, payload.get("period"), payload.get("date"))


def site_settings_view_handler(*, price_writable: bool) -> SiteHandler:
    """`get_site_settings_view`: what the webhook's own writes can change is writable (as its dashboard says:
    `update_site_settings`, and `choose_price_source` and `update_grid_tariff` where `price_writable`), load
    balancing is not."""

    async def handler(hass: HomeAssistant, site: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
        action = "get_site_settings_view"
        if not _speaks(payload, SITE_VIEW_API_VERSION):
            return _refused(action, ERROR_UNSUPPORTED_VERSION)
        if site is None or site.state is not ConfigEntryState.LOADED:
            return _refused(action, ERROR_NO_SITE)
        view = site_settings_view(
            hass,
            site,
            writable=True,
            forecast_domains=await async_forecast_capable_domains(hass),
            active_control_writable=False,
            price_writable=price_writable,
        )
        return web.json_response({"ok": True, "action": action, **view})

    return handler


async def _update_heat(hass: HomeAssistant, site: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
    """`update_heat`: the dial, home/away, the cooler night and the zones' offsets, through the command's own judge
    and compare-and-set."""
    action = "update_heat"
    if not _speaks(payload, HEAT_API_VERSION):
        return _refused(action, ERROR_UNSUPPORTED_VERSION)
    if site is None:
        return _refused(action, ERROR_NO_SITE)
    try:
        write_heat(hass, site, payload.get("changes"), payload.get("expected"), admin=False, over_webhook=True)
    except HeatWriteRefusal as refusal:
        return web.json_response(
            {**write_envelope(site, refusal.code, refusal.field_errors), "action": action},
            status=_WRITE_STATUS.get(refusal.code, 400),
        )
    return web.json_response({**write_envelope(site), "action": action})


async def _heat_boost(hass: HomeAssistant, site: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
    """`heat_boost`: the Boost button, as `spotnav/heat_boost` (a household control, like the dial)."""
    action = "heat_boost"
    if not _speaks(payload, HEAT_API_VERSION):
        return _refused(action, ERROR_UNSUPPORTED_VERSION)
    if site is None:
        return _refused(action, ERROR_NO_SITE)
    answer = await heat_boost_answer(hass, site, payload.get("boost_action"))
    status = 200 if answer["ok"] else {ERROR_INVALID_VALUE: 400, "spotnav_rate_limited": 429}.get(answer["error"], 409)
    return web.json_response({**answer, "action": action}, status=status)


async def _battery_control(hass: HomeAssistant, site: ConfigEntry | None, payload: dict[str, Any]) -> web.Response:
    """`battery_control`: the status, after writing the battery mode when `changes` names it."""
    action = "battery_control"
    if not _speaks(payload, BATTERY_CONTROL_API_VERSION):
        return _refused(action, ERROR_UNSUPPORTED_VERSION)
    answer = await async_battery_control_answer(hass, site, payload.get("changes"), admin=False, over_webhook=True)
    status = 200 if answer["ok"] else _WRITE_STATUS.get(answer["error"], 400)
    return web.json_response({**answer, "action": action}, status=status)


def site_handlers(*, price_writable: bool) -> dict[str, SiteHandler]:
    """The site actions bound to a site, for either carrier: the charger webhook (its charger's site) and the site
    webhook (`api/site_entry_webhook.py`, its own site, where the price is read-only: `price_writable` false)."""
    return {
        "get_house": _read("get_house", HOUSE_API_VERSION, _house),
        "get_house_now": _read("get_house_now", HOUSE_NOW_API_VERSION, _house_now),
        "get_house_history": _read("get_house_history", HOUSE_HISTORY_API_VERSION, _house_history),
        "get_house_loads": _read("get_house_loads", HOUSE_LOADS_API_VERSION, async_loads_answer),
        "get_heat": _read("get_heat", HEAT_API_VERSION, _heat),
        "get_heat_history": _read("get_heat_history", HEAT_HISTORY_API_VERSION, _heat_history),
        "get_site_settings_view": site_settings_view_handler(price_writable=price_writable),
        "battery_control": _battery_control,
        "update_heat": _update_heat,
        "heat_boost": _heat_boost,
    }


def _of_charger(handler: SiteHandler) -> _Handler:
    """A site action on the charger webhook: bound to the site that lists the webhook's charger (loaded or not)."""

    async def bound(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
        return await handler(hass, site_of_charger(hass, entry.entry_id), payload)

    return bound


#: The site actions of the charger webhook (`api/webhook.py:ACTIONS` takes them in).
SITE_ACTIONS: Final[dict[str, _Handler]] = {
    action: _of_charger(handler) for action, handler in site_handlers(price_writable=True).items()
}
