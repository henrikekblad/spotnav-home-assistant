"""`spotnav/get_heat_history` (API v1): the heat card's history, one day, month or year of a site's heating.

`{api_version: 1, site_id | charger_id, period: "day" | "month" | "year", date?: "YYYY-MM-DD"}` as
`spotnav/get_house_history` (`api/house_history.py`, which says why a command of its own). Any user who can see the
card may read. The answer is `heat/history.py:heat_history` (its docstring has the shape).

What is read, in the recorder's executor and kept: the heat pump meter's hourly statistics from the degree-day
trend's first month (eleven months before the period's last) to the last completed hour, its 5-minute rows where the
recorder still holds them (the period's days and the week before, for the hot-water split and its runs), the outdoor
sensor's, the chosen indoor sensors' and the immersion heater's hourly means (and the indoor minimums). The prices
are the area's held day documents with the fees as set now (`api/house_history.period_price_slots`). The answer is
kept per site and period until the next hour (a day with today in it: the next 5-minute row) or, for a finished
period, until the next day. Refusals as `spotnav/get_house_history`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback, HomeAssistant

from ..heat.detect import read_heat_pumps
from ..heat.relay_effect import Effect
from ..heat.history import (
    HEAT_HISTORY_API_VERSION,
    OUTDOOR_HOUSE_FORECAST,
    OUTDOOR_SENSOR,
    TREND_MONTHS,
    HeatHistoryInputs,
    heat_history,
)
from ..planning.demand_charge import hour_start
from ..pricing.price_source import site_price_source
from ..runtime import site_controller_for
from ..site.heat_meter import async_heat_hours
from ..site.history_period import (
    PERIOD_DAY,
    PERIOD_MONTH,
    Period,
    local_date,
    midnight_utc,
    period_of,
    requested_period,
    shifted,
    utc_bounds,
)
from .common import send_unsupported_version
from .heat import _name, stored_config
from .house import five_slot, short_term_days
from .house_history import ERROR_INVALID_DATE, period_price_slots
from .power_month import ERROR_NO_SITE, PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)

_CACHE: Final = "spotnav_heat_history"
CACHE_SIZE: Final = 24
#: The days before a shown day whose hot-water runs the run causes compare with.
REFERENCE_DAYS: Final = 6


def trend_start(period: Period) -> date:
    """The first day the degree-day trend needs: eleven months before the period's last month (a day: itself)."""
    if period.kind == PERIOD_DAY:
        return period.first
    last = period_of(PERIOD_MONTH, period.end - timedelta(days=1))
    return shifted(last, -(TREND_MONTHS - 1)).first


def outdoor_source(config: Any, controller: Any, pump: Any) -> tuple[str | None, str] | None:
    """Where the outdoor temperature's past comes from (see `heat/history.py`)."""
    if config.outdoor_sensor is not None:
        return config.outdoor_sensor, OUTDOOR_SENSOR
    forecast = controller.house_forecast
    if forecast.outdoor_source == "sensor" and forecast.outdoor_entity_id:
        return forecast.outdoor_entity_id, OUTDOOR_SENSOR
    pump_sensor = None if pump is None else pump.sources.get("outdoor_temp")
    if pump_sensor and " + " not in pump_sensor:
        return pump_sensor, OUTDOOR_SENSOR
    if any(row[1] is not None for row in forecast.history.rows.values()):
        return forecast.weather_entity_id, OUTDOOR_HOUSE_FORECAST
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _start_of(row: Mapping[str, Any]) -> datetime | None:
    start = row.get("start")
    if isinstance(start, datetime):
        return start
    if isinstance(start, (int, float)) and not isinstance(start, bool):
        return datetime.fromtimestamp(float(start), timezone.utc)
    return None


def hourly_means(rows: Iterable[Mapping[str, Any]], with_min: bool = False) -> dict[datetime, Any]:
    """The rows' `mean` (and `min`) per UTC hour start; rows without them are skipped."""
    out: dict[datetime, Any] = {}
    for row in rows:
        start = _start_of(row)
        mean = _number(row.get("mean"))
        if start is None or mean is None:
            continue
        if with_min:
            low = _number(row.get("min"))
            out[start] = (mean, mean if low is None else low)
        else:
            out[start] = mean
    return out


async def _read_means(
    hass: HomeAssistant, temperature_ids: set[str], power_ids: set[str], start: datetime, end: datetime
) -> dict[str, list[dict[str, Any]]]:
    """The sensors' hourly `mean` and `min` in `[start, end)`, temperatures in °C and powers in kW, in the recorder's
    executor."""
    if not temperature_ids and not power_ids or end <= start:
        return {}
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415 - only when loaded
    from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

    def read() -> dict[str, list[dict[str, Any]]]:
        rows: dict[str, list[dict[str, Any]]] = {}
        if temperature_ids:
            rows.update(
                statistics_during_period(hass, start, end, temperature_ids, "hour", {"temperature": "°C"}, {"mean", "min"})
            )
        if power_ids:
            rows.update(statistics_during_period(hass, start, end, power_ids, "hour", {"power": "kW"}, {"mean"}))
        return rows

    return await get_instance(hass).async_add_executor_job(read)


async def history_answer(hass: HomeAssistant, site: Any, raw_period: Any, raw_date: Any) -> dict[str, Any]:
    """The answer for one site and period, or a `PowerMonthRefusal`."""
    controller = site_controller_for(hass, site.entry_id)
    if site.state is not ConfigEntryState.LOADED or controller is None:
        raise PowerMonthRefusal(ERROR_NO_SITE, "The site is not loaded")
    now = controller.demand.now()
    tz = controller.demand.tracker.tz
    today = local_date(now, tz)
    try:
        period = requested_period(raw_period, raw_date, today)
    except ValueError as err:
        raise PowerMonthRefusal(ERROR_INVALID_DATE, "The period is not one the heat card can show") from err
    complete = period.end <= today
    mark: Any = today if complete else (five_slot(now) if period.kind == PERIOD_DAY else hour_start(now, tz))
    cache: dict[tuple[Any, ...], tuple[Any, dict[str, Any]]] = hass.data.setdefault(_CACHE, {})
    config = stored_config(site)
    shadow_mark = getattr(getattr(controller, "heat_shadow", None), "effect", None)
    key = (site.entry_id, period.kind, period.first, repr(config), repr(shadow_mark))
    held = cache.get(key)
    if held is not None and held[0] == mark:
        return held[1]

    start, end = utc_bounds(period, tz)
    last_hour = hour_start(now, tz)
    read_from = midnight_utc(trend_start(period), tz)
    read_to = min(end, last_hour)
    meter = controller.house_forecast.heat.meter
    pumps = read_heat_pumps(hass)
    pump = pumps[0] if pumps else None
    heat_hours: dict[datetime, float] = {}
    five: dict[datetime, float] = {}
    if meter.found:
        try:
            heat_hours = await async_heat_hours(hass, meter, read_from, read_to)
            keep = short_term_days(hass)
            if keep is not None:
                five_from = max(start - timedelta(days=REFERENCE_DAYS), now - timedelta(days=keep))
                five_to = min(end, now)
                if five_to > five_from:
                    five = await async_heat_hours(hass, meter, five_from, five_to, "5minute")
        except Exception:  # noqa: BLE001 - without the meter's rows the rest is shown
            _LOGGER.debug("SpotNav heat history: the heat meter could not be read", exc_info=True)
    outdoor_from = outdoor_source(config, controller, pump)
    immersion_ids = set()
    if pump is not None and pump.sources.get("immersion_power"):
        immersion_ids = set(pump.sources["immersion_power"].split(" + "))
    temperature_ids = set(config.indoor_sensors)
    if outdoor_from is not None and outdoor_from[1] == OUTDOOR_SENSOR and outdoor_from[0]:
        temperature_ids.add(outdoor_from[0])
    try:
        rows = await _read_means(hass, temperature_ids, immersion_ids, read_from, read_to)
    except Exception:  # noqa: BLE001 - without the sensors' rows the rest is shown
        _LOGGER.debug("SpotNav heat history: the sensors' statistics could not be read", exc_info=True)
        rows = {}
    outdoor: dict[datetime, float] = {}
    if outdoor_from is not None and outdoor_from[1] == OUTDOOR_SENSOR:
        outdoor = hourly_means(rows.get(outdoor_from[0] or "", ()))
    elif outdoor_from is not None:
        outdoor = {
            moment: values[1]
            for moment, values in controller.house_forecast.history.rows.items()
            if values[1] is not None and read_from <= moment < read_to
        }
    immersion = None
    if immersion_ids:
        immersion = {}
        for entity_id in immersion_ids:
            for moment, kw in hourly_means(rows.get(entity_id, ())).items():
                immersion[moment] = immersion.get(moment, 0.0) + kw
    indoor = {
        entity_id: (_name(hass, entity_id), hourly_means(rows.get(entity_id, ()), with_min=True))
        for entity_id in config.indoor_sensors
    }
    source = site_price_source(hass, site.entry_id)
    area = None if source is None else source.area
    state = None if meter.entity_id is None else hass.states.get(meter.entity_id)
    shadow = getattr(controller, "heat_shadow", None)
    kept: dict[date, dict[datetime, str]] = {}
    reviews: dict[date, dict[str, Any]] = {}
    effect = Effect()
    if shadow is not None:
        effect = shadow.effect
        for day in period.days():
            actions = shadow.kept_actions(day)
            # A day kept whole only: a part-kept day (SpotNav started mid-day) is planned again whole.
            if len(actions) >= 23:
                kept[day] = {hour: action for hour, (action, _reason) in actions.items()}
            review = shadow.review_of(day)
            if review is not None:
                reviews[day] = review
    answer = heat_history(
        HeatHistoryInputs(
            site_id=site.entry_id,
            now=now,
            tz=tz,
            period=period,
            config=config,
            meter=(meter.entity_id, None if state is None else state.name) if meter.found and meter.entity_id else None,
            heat_hours=heat_hours,
            five=five,
            prices=tuple((slot.start, slot.end, slot.buy) for slot in period_price_slots(hass, site.entry_id, period))
            if heat_hours
            else (),
            money=None if area is None else (area.currency, area.major_unit, area.minor_unit),
            outdoor=outdoor,
            outdoor_source=outdoor_from,
            indoor=indoor,
            immersion=immersion,
            kept=kept,
            effect=effect,
            reviews=reviews,
        )
    )
    cache.pop(key, None)
    cache[key] = (mark, answer)
    while len(cache) > CACHE_SIZE:
        del cache[next(iter(cache))]
    return answer


@callback
def forget_cached_heat_history(hass: HomeAssistant, site_entry_id: str) -> None:
    """Drop a site's kept answers (its unload)."""
    cache: dict[tuple[Any, ...], Any] | None = hass.data.get(_CACHE)
    if cache:
        for key in [key for key in cache if key[0] == site_entry_id]:
            del cache[key]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_heat_history",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("period"): object,
        vol.Optional("date"): object,
    }
)
@websocket_api.async_response
async def websocket_get_heat_history(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The heat card's history of one site and period."""
    version = msg.get("api_version")
    if type(version) is not int or version != HEAT_HISTORY_API_VERSION:
        send_unsupported_version(connection, msg, HEAT_HISTORY_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
        answer = await history_answer(hass, site, msg.get("period"), msg.get("date"))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_heat_history_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_heat_history)
