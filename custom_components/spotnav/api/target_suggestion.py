"""`spotnav/suggest_import_target` (API v1): a suggested import target from Home Assistant's statistics, for the
import target's editor.

`{api_version: 1, charger_id | site_id}`, administrators only (as the target's write). The site's hours of the last
`PERIOD_DAYS` are read from the recorder's hourly statistics (`site/hour_statistics.py`): the grid's mean, less what
the member chargers drew. The answer, kept per site for `CACHE_S` (a read of three months of statistics is not
repeated for every opening of the editor, and requests while one is read share it):

    {api_version, site_id, available, reason, suggestion_kw, typical_kw, highest, car_min_kw, car_kw, counted,
     days, hours, period, months, grid_entities, chargers}

* `available`: whether there is a suggestion; else `reason` is `no_recorder` (the recorder is not running),
  `no_grid_statistics` (the site's grid power has no long-term statistics, or in a unit not known), `too_few_days`
  (fewer than `MIN_DAYS` days) or `no_chargers` (the site has no charger to give a minimum power); every figure is
  then `null` (`days`/`hours` still count what was read).
* `suggestion_kw`: the house's typical daily peak (`typical_kw`, the median of the days' highest hourly means of the
  house alone, within the demand-charge parts' own hours where every part has some: `counted` is `windows`, else
  `all`) plus the car's minimum power on this site (`car_min_kw`), rounded up to the next 0.5 kW.
* `highest`: `{kw, start, date}`, the house's highest hour (local time on the area's clock).
* `car_kw`: what the car gets at the suggestion in a typical charging hour: the suggestion less the house's median
  hourly mean in the evening and night (18-06), never below `car_min_kw`.
* `months`: per month, oldest first, `{month, peak_kw, peak_start, typical_kw, days}`.
* `period`: `{start, end}` read; `grid_entities` the grid's entities; `chargers`: per member charger
  `{charger_id, source, has_statistics}` (`source`: `energy`, `power`, `current` or `null`). A charger without
  statistics is counted as nothing: the grid alone stands for the house then.

Refusals (WebSocket errors; the webhook answers 400 with `error`): `spotnav_unsupported_api_version`,
`spotnav_not_admin`, `spotnav_charger_required`, the charger lookup's codes, `spotnav_unknown_site`,
`spotnav_no_site` and `spotnav_needs_total_grid_power`. Nothing is written; nothing changes what a charger is told.

The charger webhook's `suggest_import_target` answers the same for the paired app's charger (its site), with the
routing `action` beside it (capability `suggest_import_target`).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import callback, HomeAssistant
from homeassistant.util import dt as dt_util

from .. import time_zones
from ..const import CONF_GRID_POWER_SOURCE, CONF_MEASUREMENT_MODE, DOMAIN
from ..pricing.price_source import site_demand_components
from ..runtime import site_controller_for
from ..site.hour_statistics import (
    read_site_hours,
    recorder_ready,
    RecorderReader,
    REASON_NO_GRID_STATISTICS,
    REASON_NO_RECORDER,
    site_series,
    statistics_period,
    StatisticsReader,
    suggest_target,
)
from ..site.solar_capability import solar_capability
from .common import ERROR_NOT_ADMIN, ERROR_UNSUPPORTED_VERSION, is_admin, send_unsupported_version
from .power_month import (
    _site_of_charger,
    _site_of_request,
    ERROR_NEEDS_TOTAL_GRID_POWER,
    ERROR_NO_SITE,
    PowerMonthRefusal,
)

SUGGEST_API_VERSION: Final = 1
WEBHOOK_ACTION: Final = "suggest_import_target"
#: How long one site's answer is kept.
CACHE_S: Final = 600.0
_CACHE_KEY: Final = f"{DOMAIN}_target_suggestion"

#: Every key of the answer, in order.
SUGGEST_KEYS: Final = (
    "api_version",
    "site_id",
    "available",
    "reason",
    "suggestion_kw",
    "typical_kw",
    "highest",
    "car_min_kw",
    "car_kw",
    "counted",
    "days",
    "hours",
    "period",
    "months",
    "grid_entities",
    "chargers",
)


def _kw(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


async def build_suggestion(
    hass: HomeAssistant, site: ConfigEntry, reader: StatisticsReader | None = None
) -> dict[str, Any]:
    """The answer for one site, read now (see the module)."""
    controller = site_controller_for(hass, site.entry_id)
    zone_name = controller.demand_zone() if controller is not None else hass.config.time_zone
    zone = time_zones.require_zone(zone_name)
    now = controller.demand.now() if controller is not None else dt_util.utcnow()
    start, end = statistics_period(now)
    series = site_series(hass, site.data)
    answer: dict[str, Any] = {
        "api_version": SUGGEST_API_VERSION,
        "site_id": site.entry_id,
        "available": False,
        "reason": None,
        "suggestion_kw": None,
        "typical_kw": None,
        "highest": None,
        "car_min_kw": _kw(series.car_min_kw),
        "car_kw": None,
        "counted": "all",
        "days": 0,
        "hours": 0,
        "period": {"start": start.astimezone(zone).isoformat(), "end": end.astimezone(zone).isoformat()},
        "months": [],
        "grid_entities": [term.entity_id for term in series.grid],
        "chargers": [
            {"charger_id": charger.charger_entry_id, "source": charger.source, "has_statistics": False}
            for charger in series.chargers
        ],
    }
    if reader is None:
        if not recorder_ready(hass):
            return {**answer, "reason": REASON_NO_RECORDER}
        reader = RecorderReader(hass)
    read = await read_site_hours(reader, series, start, end)
    without = set(read.chargers_without_statistics)
    answer["chargers"] = [
        {**charger, "has_statistics": charger["charger_id"] not in without} for charger in answer["chargers"]
    ]
    if not read.grid_readable:
        return {**answer, "reason": REASON_NO_GRID_STATISTICS}
    found = suggest_target(
        read.hours,
        tz=zone_name,
        components=site_demand_components(hass, site.entry_id),
        car_min_kw=series.car_min_kw,
    )
    answer.update(counted=found.counted, days=found.days, hours=found.hours)
    if found.reason is not None:
        return {**answer, "reason": found.reason}
    assert found.highest_start is not None and found.highest_kw is not None
    highest_local = found.highest_start.astimezone(zone)
    answer.update(
        available=True,
        suggestion_kw=_kw(found.suggestion_kw, 1),
        typical_kw=_kw(found.typical_kw),
        highest={"kw": _kw(found.highest_kw), "start": highest_local.isoformat(), "date": highest_local.date().isoformat()},
        car_kw=_kw(found.car_kw),
        months=[
            {
                "month": f"{month.month[0]:04d}-{month.month[1]:02d}",
                "peak_kw": _kw(month.peak_kw),
                "peak_start": month.peak_start.astimezone(zone).isoformat(),
                "typical_kw": _kw(month.typical_kw),
                "days": month.days,
            }
            for month in found.months
        ],
    )
    return answer


async def suggestion_answer(hass: HomeAssistant, site: ConfigEntry) -> dict[str, Any]:
    """The answer for one site, from the cache while it is fresh, or a `PowerMonthRefusal`."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    capability = solar_capability(site.data.get(CONF_MEASUREMENT_MODE), site.data.get(CONF_GRID_POWER_SOURCE))
    if not capability.capable:
        raise PowerMonthRefusal(ERROR_NEEDS_TOTAL_GRID_POWER, "The site cannot measure its import")
    cache: dict[str, tuple[float, asyncio.Future[dict[str, Any]]]] = hass.data.setdefault(_CACHE_KEY, {})
    kept = cache.get(site.entry_id)
    now = time.monotonic()
    if kept is not None and now - kept[0] < CACHE_S and not (kept[1].done() and kept[1].exception() is not None):
        return await asyncio.shield(kept[1])
    future: asyncio.Future[dict[str, Any]] = hass.loop.create_future()
    cache[site.entry_id] = (now, future)
    try:
        answer = await build_suggestion(hass, site)
    except Exception as failure:
        future.set_exception(failure)
        future.exception()  # read here: a request that shares it may not come
        cache.pop(site.entry_id, None)
        raise
    future.set_result(answer)
    return answer


def forget_suggestion(hass: HomeAssistant, site_id: str) -> None:
    """Drop one site's kept answer (its chargers or sources changed)."""
    hass.data.get(_CACHE_KEY, {}).pop(site_id, None)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/suggest_import_target",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
    }
)
@websocket_api.async_response
async def websocket_suggest_import_target(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """A suggested import target for the site."""
    version = msg.get("api_version")
    if type(version) is not int or version != SUGGEST_API_VERSION:
        send_unsupported_version(connection, msg, SUGGEST_API_VERSION)
        return
    if not is_admin(connection):
        connection.send_error(msg["id"], ERROR_NOT_ADMIN, "Only an administrator may ask")
        return
    try:
        answer = await suggestion_answer(hass, _site_of_request(hass, msg))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


async def webhook_suggest_import_target(
    hass: HomeAssistant, entry: ConfigEntry, payload: dict[str, Any]
) -> web.Response:
    """`suggest_import_target` over the charger webhook (the paired app): this charger's site, an optional
    `api_version` (1). HTTP 200 with the answer, 400 with the refusal's code."""
    version = payload.get("api_version", SUGGEST_API_VERSION)
    if type(version) is not int or version != SUGGEST_API_VERSION:
        return web.json_response({"ok": False, "error": ERROR_UNSUPPORTED_VERSION, "action": WEBHOOK_ACTION}, status=400)
    try:
        answer = await suggestion_answer(hass, _site_of_charger(hass, entry))
    except PowerMonthRefusal as refusal:
        return web.json_response({"ok": False, "error": refusal.code, "action": WEBHOOK_ACTION}, status=400)
    return web.json_response({"ok": True, "action": WEBHOOK_ACTION, **answer})


@callback
def async_setup_target_suggestion_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_suggest_import_target)
