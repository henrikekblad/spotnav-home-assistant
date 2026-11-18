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
  `away_until`), the cooler night (`night`) and each stored zone's offset (`zones`, the same entities in the same
  order), judged by the command's own rules (`heat/config.py`) under its compare-and-set.
* `battery_control` with `changes`: `battery_mode` only (`self_consumption`, `balanced`, `max_profit`).

The control opt-ins stay with a signed-in administrator in Home Assistant: `control_enabled` ("Styr värmen") and
the control path, the sensors and which entities are zones, `battery_control_enabled` ("Styr batteriet") and the other battery options are
`spotnav_not_permitted_over_webhook` (HTTP 403) and nothing is written, as load balancing is
(`api/site_settings.py`). Both writes answer their command's envelope (`ok`, `error` inside it): HTTP 200, 400 for a
value refused, 409 for `spotnav_conflict`, 403 as above.

`api_version` is optional in every body (1, each command's own); another is `spotnav_unsupported_api_version`. A
charger in no site, or in one that is not loaded, is `spotnav_no_site`. The dashboard's `charger.capabilities` says
what a charger's webhook answers: `get_house`, `get_heat`, `battery_control`, `get_site_settings_view`
(`api/dashboard.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiohttp import web
from homeassistant.config_entries import ConfigEntry
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
from .heat import ERROR_CONFLICT, HeatWriteRefusal, heat_answer, write_envelope, write_heat
from .house import async_house_answer
from .house_loads import async_loads_answer
from .house_now import async_house_now
from .power_month import PowerMonthRefusal, _site_of_charger
from .site_view import SITE_VIEW_API_VERSION, resolve_site, site_settings_view

type _Handler = Callable[[HomeAssistant, ConfigEntry, dict[str, Any]], Awaitable[web.Response]]
type _Read = Callable[[HomeAssistant, ConfigEntry, dict[str, Any]], Awaitable[dict[str, Any]]]

#: HTTP status of a write's refusal by its stable code; any other refusal is a bad request.
_WRITE_STATUS: Final = {ERROR_CONFLICT: 409, ERROR_NOT_PERMITTED_OVER_WEBHOOK: 403}


def _refused(action: str, code: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": code, "action": action}, status=status)


def _speaks(payload: dict[str, Any], version: int) -> bool:
    """Whether the body asks for the command's version (absent: it does)."""
    asked = payload.get("api_version", version)
    return type(asked) is int and asked == version


def _read(action: str, version: int, read: _Read) -> _Handler:
    """A read twinning a WebSocket command: `read(hass, site, payload)` is the command's own answer for the
    charger's site, a `PowerMonthRefusal` its refusal."""

    async def handler(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
        if not _speaks(payload, version):
            return _refused(action, ERROR_UNSUPPORTED_VERSION)
        try:
            answer = await read(hass, _site_of_charger(hass, entry), payload)
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


async def _site_settings_view(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    """`get_site_settings_view`: what the webhook's own writes can change is writable (as its dashboard says:
    `update_site_settings`, `choose_price_source`, `update_grid_tariff`), load balancing is not."""
    action = "get_site_settings_view"
    if not _speaks(payload, SITE_VIEW_API_VERSION):
        return _refused(action, ERROR_UNSUPPORTED_VERSION)
    site, code = resolve_site(hass, {"charger_id": entry.entry_id})
    if site is None:
        return _refused(action, code or ERROR_NO_SITE)
    view = site_settings_view(
        hass,
        site,
        writable=True,
        forecast_domains=await async_forecast_capable_domains(hass),
        active_control_writable=False,
    )
    return web.json_response({"ok": True, "action": action, **view})


async def _update_heat(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    """`update_heat`: the dial, home/away, the cooler night and the zones' offsets, through the command's own judge
    and compare-and-set."""
    action = "update_heat"
    if not _speaks(payload, HEAT_API_VERSION):
        return _refused(action, ERROR_UNSUPPORTED_VERSION)
    try:
        site = _site_of_charger(hass, entry)
    except PowerMonthRefusal as refusal:
        return _refused(action, refusal.code)
    try:
        write_heat(hass, site, payload.get("changes"), payload.get("expected"), admin=False, over_webhook=True)
    except HeatWriteRefusal as refusal:
        return web.json_response(
            {**write_envelope(site, refusal.code, refusal.field_errors), "action": action},
            status=_WRITE_STATUS.get(refusal.code, 400),
        )
    return web.json_response({**write_envelope(site), "action": action})


async def _battery_control(hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]) -> web.Response:
    """`battery_control`: the status, after writing the battery mode when `changes` names it."""
    action = "battery_control"
    if not _speaks(payload, BATTERY_CONTROL_API_VERSION):
        return _refused(action, ERROR_UNSUPPORTED_VERSION)
    answer = await async_battery_control_answer(
        hass, site_of_charger(hass, entry.entry_id), payload.get("changes"), admin=False, over_webhook=True
    )
    status = 200 if answer["ok"] else _WRITE_STATUS.get(answer["error"], 400)
    return web.json_response({**answer, "action": action}, status=status)


#: The site actions of the charger webhook (`api/webhook.py:ACTIONS` takes them in).
SITE_ACTIONS: Final[dict[str, _Handler]] = {
    "get_house": _read("get_house", HOUSE_API_VERSION, _house),
    "get_house_now": _read("get_house_now", HOUSE_NOW_API_VERSION, _house_now),
    "get_house_history": _read("get_house_history", HOUSE_HISTORY_API_VERSION, _house_history),
    "get_house_loads": _read("get_house_loads", HOUSE_LOADS_API_VERSION, async_loads_answer),
    "get_heat": _read("get_heat", HEAT_API_VERSION, _heat),
    "get_heat_history": _read("get_heat_history", HEAT_HISTORY_API_VERSION, _heat_history),
    "get_site_settings_view": _site_settings_view,
    "battery_control": _battery_control,
    "update_heat": _update_heat,
}
