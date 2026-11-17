"""`spotnav/get_house_loads` (API v1): the house's known meters over a day, month or year of a site (read-only), for
the pie the house card opens from "House" in its energy flow.

`{api_version: 1, site_id | charger_id, period?: "day" | "month" | "year", date?: "YYYY-MM-DD"}` (absent: today).
Any user who can see the card may read. The answer is `site/house_loads.py:house_loads` (its docstring has the shape).
A command of its own, asked only when the pie is opened, so neither the front's answer (read every five minutes) nor
the history's carries the meters.

The house's figure is the energy flow's house block for the period, as the card shows it: the history's answer for
the period (`api/house_history.history_answer`, its cache), whose day is the front's day flow. The meters are the
Energy dashboard's individual devices (`energy/get_prefs`); their statistics are read in the recorder's executor over
the period's start up to the flow's `through`: by the hour, and for a day read by the quarter hour its 5-minute rows
as well (the running hour). The answer is kept per site and period until the flow's `through` or the meters change.
Refusals as `spotnav/get_house_history`, and `spotnav_read_failed` when the statistics cannot be read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import CONF_CHARGE_CONTROL, CONF_CHARGER_ENTRY_IDS, CONF_POWER_ENTITY
from ..energy_register import _charger_device_id
from ..runtime import controller_for, site_controller_for
from ..site.energy_flow import ROLE_HOUSE
from ..site.energy_statistics import async_energy_prefs, async_statistics
from ..site.history_period import PERIOD_DAY, period_of, utc_bounds
from ..site.house_loads import (
    HOUSE_LOADS_API_VERSION,
    DashboardMeter,
    counted_meters,
    house_loads,
    meter_kwh,
)
from .common import send_unsupported_version
from .house_history import history_answer
from .power_month import PowerMonthRefusal, _site_of_request

_LOGGER = logging.getLogger(__name__)

ERROR_READ_FAILED: Final = "spotnav_read_failed"
_CACHE: Final = "spotnav_house_loads"
#: The answers kept (all sites together); the oldest goes first.
CACHE_SIZE: Final = 24


def dashboard_meters(hass: HomeAssistant, prefs: Any) -> list[DashboardMeter]:
    """The Energy dashboard's individual devices, each with the name it is shown by there (its own, else its entity's
    friendly name, else the statistic id) and the device its statistic's entity is on."""
    devices = prefs.get("device_consumption") if isinstance(prefs, Mapping) else None
    entities = er.async_get(hass)
    out: list[DashboardMeter] = []
    for device in devices if isinstance(devices, list) else []:
        if not isinstance(device, Mapping) or not isinstance(device.get("stat_consumption"), str):
            continue
        stat = device["stat_consumption"]
        entry = entities.async_get(stat)
        state = hass.states.get(stat)
        names = (
            device.get("name"),
            None if state is None else state.attributes.get("friendly_name"),
            None if entry is None else entry.name or entry.original_name,
        )
        name = next((value.strip() for value in names if isinstance(value, str) and value.strip()), stat)
        rate = device.get("stat_rate")
        nested = device.get("included_in_stat")
        out.append(
            DashboardMeter(
                statistic_id=stat,
                name=name,
                rate_id=rate if isinstance(rate, str) else None,
                device_id=None if entry is None else entry.device_id,
                included_in=nested if isinstance(nested, str) else None,
            )
        )
    return out


def charger_sensors(hass: HomeAssistant, controller: Any) -> tuple[set[str], set[str]]:
    """The site's chargers' sensors (energy register, power sensor, charge control) and devices."""
    entities: set[str] = set()
    devices: set[str] = set()
    registry = er.async_get(hass)
    for charger_entry_id in controller.config.get(CONF_CHARGER_ENTRY_IDS) or []:
        charger = controller_for(hass, charger_entry_id)
        register = None if charger is None else charger.energy_register_entity_id
        entry = hass.config_entries.async_get_entry(charger_entry_id)
        data = {} if entry is None else dict(entry.data)
        for entity_id in (register, data.get(CONF_POWER_ENTITY), data.get(CONF_CHARGE_CONTROL)):
            if isinstance(entity_id, str) and entity_id:
                entities.add(entity_id)
                registered = registry.async_get(entity_id)
                if registered is not None and registered.device_id is not None and entity_id == register:
                    devices.add(registered.device_id)
        device_id = _charger_device_id(hass, data) if data else None
        if device_id is not None:
            devices.add(device_id)
    return entities, devices


async def _read(hass: HomeAssistant, meters: list[DashboardMeter], start: datetime, through: datetime, five: bool) -> list[tuple[DashboardMeter, float]]:
    ids = [meter.statistic_id for meter in meters]
    hourly = await async_statistics(hass, ids, start, through, "hour")
    short = await async_statistics(hass, ids, start, through, "5minute") if five else {}
    return [
        (meter, meter_kwh(hourly.get(meter.statistic_id) or [], short.get(meter.statistic_id) or [], start, through))
        for meter in meters
    ]


async def loads_answer(hass: HomeAssistant, site: Any, raw_period: Any, raw_date: Any) -> dict[str, Any]:
    """The answer for one site and period, or a `PowerMonthRefusal`."""
    history = await history_answer(hass, site, PERIOD_DAY if raw_period is None else raw_period, raw_date)
    controller = site_controller_for(hass, site.entry_id)
    if controller is None:  # unloaded between the two reads
        raise PowerMonthRefusal("spotnav_no_site", "The site is not loaded")
    tz = controller.demand.tracker.tz
    period = period_of(history["period"], date.fromisoformat(history["date"]))
    flow = history["energy_flow"]
    house = None
    through = None
    five = False
    if flow is not None:
        house = next((float(node["kwh"]) for node in flow["nodes"] if node["role"] == ROLE_HOUSE), 0.0)
        through = datetime.fromisoformat(flow["through"])
        five = int(flow.get("step_min") or 60) < 60
    car_entities, car_devices = charger_sensors(hass, controller)
    meter = controller.house_forecast.heat.meter
    heat = [meter.entity_id] if meter.found and meter.entity_id else []
    meters = counted_meters(
        dashboard_meters(hass, await async_energy_prefs(hass)),
        car_entities=car_entities,
        car_devices=car_devices,
        heat_entities=heat,
    )
    cache: dict[tuple[Any, ...], tuple[Any, dict[str, Any]]] = hass.data.setdefault(_CACHE, {})
    key = (site.entry_id, period.kind, period.first)
    mark = (through, house, tuple(meters))
    held = cache.get(key)
    if held is not None and held[0] == mark:
        return held[1]
    start = utc_bounds(period, tz)[0]
    loads: list[tuple[DashboardMeter, float]] = []
    if meters and through is not None and through > start:
        loads = await _read(hass, meters, start, through, five)
    answer = house_loads(
        site_id=site.entry_id,
        period=period.kind,
        first=period.first.isoformat(),
        end=period.end.isoformat(),
        through=through,
        house_kwh=house,
        loads=loads or [(meter, 0.0) for meter in meters],
    )
    cache.pop(key, None)
    cache[key] = (mark, answer)
    while len(cache) > CACHE_SIZE:
        del cache[next(iter(cache))]
    return answer


@callback
def forget_cached_loads(hass: HomeAssistant, site_entry_id: str) -> None:
    """Drop a site's kept answers (its unload)."""
    cache: dict[tuple[Any, ...], Any] | None = hass.data.get(_CACHE)
    if cache:
        for key in [key for key in cache if key[0] == site_entry_id]:
            del cache[key]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "spotnav/get_house_loads",
        vol.Optional("api_version"): object,
        vol.Optional("charger_id"): object,
        vol.Optional("site_id"): object,
        vol.Optional("period"): object,
        vol.Optional("date"): object,
    }
)
@websocket_api.async_response
async def websocket_get_house_loads(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The house's known meters over one period of a site."""
    version = msg.get("api_version")
    if type(version) is not int or version != HOUSE_LOADS_API_VERSION:
        send_unsupported_version(connection, msg, HOUSE_LOADS_API_VERSION)
        return
    try:
        site = _site_of_request(hass, msg)
        answer = await loads_answer(hass, site, msg.get("period"), msg.get("date"))
    except PowerMonthRefusal as refusal:
        connection.send_error(msg["id"], refusal.code, refusal.message)
        return
    except Exception:  # noqa: BLE001 - no recorder, statistics that fail: the card says it could not be read
        _LOGGER.debug("SpotNav house loads: the meters could not be read", exc_info=True)
        connection.send_error(msg["id"], ERROR_READ_FAILED, "The meters could not be read")
        return
    connection.send_result(msg["id"], answer)


@callback
def async_setup_house_loads_api(hass: HomeAssistant) -> None:
    """Register the command once for the domain."""
    websocket_api.async_register_command(hass, websocket_get_house_loads)
